"""Parallel model/effort/issue trials driven by native CLI sessions."""
import concurrent.futures
import fcntl
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import usage
from .checkouts import TrialCheckout, trial_id
from .dashboard import write_dashboard
from .native_cli import cli_identity, run_turn
from .queue import TestQueue
from .schema import digest, public_task, write_json
from .test_service import TestService
from .vm_pool import SSHVM, VMPool

PROTOCOL = "native-cli-v1"
CONTINUE = "Review your current solution against the original requirements. Use public tests to check it, improve it if needed, and finish this turn."


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    os.replace(temporary, path)


def run_matrix(suite, repo, environments, matrix, out, *, vm_count=None, parallel=None,
               resume=False, driver_factory=SSHVM, agent_runner=run_turn):
    out = Path(out).resolve()
    agents, vms = matrix["agents"], matrix["vms"]
    if not isinstance(agents, list) or not isinstance(vms, list):
        raise ValueError("agents and vms must be lists")
    matrix = {"max_rounds": 4, "round_timeout_seconds": 900, **matrix}
    rounds = matrix["max_rounds"]
    repeats = matrix.get("repeats", 1)
    count = vm_count if vm_count is not None else matrix.get("vm_count", len(vms))
    if not agents or not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 1 or not isinstance(repeats, int) or repeats < 1:
        raise ValueError("agents, max_rounds and repeats must be nonempty/positive")
    workers = parallel if parallel is not None else matrix.get("parallel_agents", len(agents) * len(suite["tasks"]) * repeats)
    for name in ("round_timeout_seconds", "test_timeout_seconds"):
        value = matrix.get(name)
        if name == "test_timeout_seconds" and name in matrix and value is None:
            raise ValueError("test_timeout_seconds must be a positive duration")
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0):
            raise ValueError(name + " must be a positive duration or null")
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= len(vms):
        raise ValueError("vm_count must select from configured VMs")
    if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
        raise ValueError("parallel_agents must be positive")
    for task in suite["tasks"]:
        env = environments["by_task"][task["instance_id"]] if "by_task" in environments else environments
        if digest(env) != task["validation"]["environment_hash"] or task["validation"]["environment"]["backend"] != "docker":
            raise ValueError("native trials require the validated pinned Docker environment")
    for spec in agents:
        if spec.get("provider") not in ("codex", "claude") or not isinstance(spec.get("model"), str) or not spec["model"] or not isinstance(spec.get("reasoning"), str) or not spec["reasoning"]:
            raise ValueError("each agent must explicitly select provider, model and reasoning")
    identities = {}
    for spec in agents:
        key = json.dumps(spec.get("command", [spec["provider"]]))
        if key not in identities:
            identities[key] = cli_identity(spec)
    configuration = {"protocol": PROTOCOL, "suite_id": suite["suite_id"], "matrix": matrix,
                     "vm_count": count, "parallel_agents": workers, "environments": environments,
                     "native_cli": identities,
                     "execution_source_hash": digest({name: Path(__file__).with_name(name).read_text() for name in (
                         "native_run.py", "native_cli.py", "checkouts.py", "queue.py", "test_service.py", "test_mcp.py",
                         "vm_pool.py", "vm_worker.py", "verification.py", "evaluate.py", "process.py", "usage.py", "schema.py")})}
    experiment = digest(configuration)
    if out.exists() and not resume:
        raise ValueError("output exists; use --resume for this exact matrix")
    out.mkdir(parents=True, exist_ok=True)
    with (out / "controller.lock").open("a+") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another controller is already using this run") from None
        manifest_path = out / "run.json"
        if resume and not manifest_path.exists():
            raise ValueError("no recorded run exists to resume")
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            if manifest["experiment_id"] != experiment:
                raise ValueError("resume configuration changed")
            manifest.setdefault("resumes", []).append({"at": time.time(), "previous_status": manifest.get("status")})
            manifest.pop("completed_at", None)
            manifest.pop("interrupted_at", None)
        else:
            manifest = {**configuration, "experiment_id": experiment, "run_id": uuid.uuid4().hex, "started_at": time.time()}
            atomic_json(manifest_path, manifest)
        manifest["status"] = "Running"
        atomic_json(manifest_path, manifest)
        queue = TestQueue(out / "queue.sqlite")
        stop = threading.Event()
        row_lock = threading.Lock()
        jobs, rows = [], {}
        for spec in agents:
            config_id = digest({"experiment": experiment, "agent": spec})
            for task in suite["tasks"]:
                for repeat in range(repeats):
                    identity = trial_id({"experiment": experiment, "run_id": manifest["run_id"], "agent": spec}, task, repeat)
                    vm = vms[len(jobs) % count]["id"]
                    label = re.sub(r"[^a-zA-Z0-9_.-]", "-", f"{spec['model']}-{spec['reasoning']}-{task['instance_id']}")[:100]
                    folder = out / "trials" / (label + "-" + identity[:8])
                    common = {"suite_id": suite["suite_id"], "protocol_id": PROTOCOL, "config_id": config_id,
                              "model": spec["model"], "reasoning": spec["reasoning"], "provider": spec["provider"],
                              "task_id": task["instance_id"], "repeat": repeat, "trial_id": identity, "vm_id": vm,
                              "artifact_directory": str(folder), "score_dimensions": sorted(task["weights"])}
                    jobs.append((spec, task, folder, common))
                    for step in range(1, rounds + 1):
                        saved = folder / f"score-{step}.json"
                        rows[identity, step] = json.loads(saved.read_text()) if saved.exists() else {
                            **common, "step": step, "status": "queued", "score": None, "scores": {}, "score_eligible": False}
        if len({j[3]["trial_id"] for j in jobs}) != len(jobs):
            raise ValueError("duplicate model/effort/issue configuration")
        pool = VMPool(queue, vms, out / "control", vm_count=count, driver_factory=driver_factory)
        task_environments = list(environments["by_task"].values()) if "by_task" in environments else [environments]
        manifest["vm_preflight"] = {name: driver.prepare(task_environments) for name, driver in pool.drivers.items()}
        atomic_json(manifest_path, manifest)
        pool.start()
        service = TestService(queue, pool)
        graders = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(jobs)))
        grading_futures = {}

        def publish():
            ordered = sorted(rows.values(), key=lambda r: (r["model"], r["reasoning"], r["task_id"], r["repeat"], r["step"]))
            temporary = out / "results.jsonl.tmp"
            temporary.write_text("".join(json.dumps(r, allow_nan=False) + "\n" for r in ordered))
            os.replace(temporary, out / "results.jsonl")
            metadata = {"title": "Native CLI benchmark", "subtitle": f"{len(jobs)} independent trials · {count} VMs · {workers} parallel agents",
                        "phase": manifest.get("status", "Running"),
                        "updated_at": time.time(), "note": "Native CLI turns, not individual tool calls. VM tests are serialized per machine. Missing grades stay pending. This protocol is separate from legacy command-loop runs."}
            metadata["progress"] = {"planned": len(ordered), "generated": sum("patch_hash" in r for r in ordered),
                                    "functional": sum(bool(r.get("evaluation_complete")) and r.get("score") is not None for r in ordered)}
            write_dashboard(ordered, out / "report.html", metadata)
            atomic_json(out / "status.json", metadata)

        def trial(spec, task, folder, common):
            identity = common["trial_id"]
            folder.mkdir(parents=True, exist_ok=True)
            if all((folder / f"score-{step}.json").exists() and rows[identity, step].get("evaluation_complete") for step in range(1, rounds + 1)):
                return
            checkout = TrialCheckout(repo, task["base_commit"], folder / "checkout")
            base = folder / "base.tar"
            if not base.exists():
                checkout.base_archive(base)
            env = environments["by_task"][task["instance_id"]] if "by_task" in environments else environments
            token = service.register(identity, checkout, base, env, common["vm_id"], test_timeout=matrix.get("test_timeout_seconds", 1800))
            token_file = folder / "test-capability"
            token_file.write_text(token)
            token_file.chmod(0o600)
            mcp_path = folder / "mcp.json"
            write_json(mcp_path, {"mcpServers": {"bench": {"command": sys.executable,
                "timeout": 86400000,
                "args": [str(Path(__file__).with_name("test_mcp.py")), "--url", service.url, "--token-file", str(token_file)]}}})
            session = None
            previous_usage = None
            cumulative = usage.empty()
            observed = {k: 0 for k in usage.FIELDS if k != "cost_usd"}
            seconds = 0.0
            checkpoints = []
            for step in range(1, rounds + 1):
                checkpoint = folder / f"checkpoint-{step}.json"
                if checkpoint.exists():
                    row = json.loads(checkpoint.read_text())
                    session, cumulative, seconds = row.get("session_id"), row["usage"], row["seconds"]
                    previous_usage = row.get("native_turn", {}).get("provider", {}).get("session_usage")
                    observed = {k: row.get("observed_usage_lower_bound", {}).get(k, row["usage"].get(k) or 0) for k in observed}
                    checkpoints.append(row)
                    continue
                if stop.is_set():
                    break
                prompt = (json.dumps(public_task(task)) + "\n\nSolve this task using your native tools. Do not commit or change Git metadata. "
                          "Use the bench.run_tests MCP tool for all compilation and public tests on the VM; do not compile on this host. "
                          "Only use this worktree and the task statement. Do not inspect other trial directories or seek historical solutions. "
                          "The test VM has its own persistent build cache. Hidden scoring is not feedback.\n"
                          "Test VM context (not the local working directory):\n" + env.get("agent_instructions", "")) if step == 1 else CONTINUE
                limit = matrix["round_timeout_seconds"]
                prompt += (f"\nConversation round {step}/{rounds}. "
                           + (f"This round has a {limit:g}-second wall-clock budget, including VM queue waits. " if limit is not None else "")
                           + f"A public test command may request at most {matrix.get('test_timeout_seconds', 1800):g} seconds of VM execution.")
                turn_dir = folder / f"turn-{step}"
                if turn_dir.exists():
                    raise RuntimeError(f"{identity} turn {step} was interrupted before its checkpoint; inspect saved events before retrying")
                turn = agent_runner(spec, checkout.root, prompt, turn_dir, session_id=session, mcp_path=mcp_path,
                                    timeout=matrix.get("round_timeout_seconds"), stop=stop, previous_usage=previous_usage)
                session = turn.get("session_id")
                previous_usage = turn.get("provider", {}).get("session_usage")
                cumulative = usage.add(cumulative, turn["usage"])
                known = turn.get("observed_usage_lower_bound", turn["usage"])
                if spec["provider"] == "codex" and previous_usage is not None:
                    observed = {k: max(observed[k], previous_usage.get(k) or 0) for k in observed}
                else:
                    observed = {k: observed[k] + (known.get(k) or 0) for k in observed}
                seconds += turn["seconds"]
                patch = service.snapshot(identity)
                (folder / f"step-{step}.patch").write_text(patch)
                row = {**common, "step": step, "session_id": session, "status": turn["status"],
                       "usage": dict(cumulative), "step_usage": turn["usage"], "seconds": seconds,
                       "native_turn": turn, "patch_hash": digest(patch), "score": None, "scores": {},
                       "observed_usage_lower_bound": {**observed, "total_tokens": observed["input_tokens"] + observed["output_tokens"]},
                       "score_eligible": False, "agent_steps": step}
                atomic_json(checkpoint, row)
                checkpoints.append(row)
                print(f"TRIAL {identity[:8]} {spec['model']}/{spec['reasoning']} {task['instance_id']} round={step} {turn['status']}", flush=True)
                with row_lock:
                    rows[identity, step] = row
                    publish()
                if turn["status"] not in ("submitted", "round_timeout") or not session:
                    break
            service.seal(identity)
            if stop.is_set():
                return
            grade_jobs = {}
            for row in checkpoints:
                patch_hash = row["patch_hash"]
                if patch_hash not in grade_jobs:
                    payload = {"base_archive": str(base), "environment": env, "task": task,
                               "patch": (folder / f"step-{row['step']}.patch").read_text()}
                    grade_jobs[patch_hash] = queue.enqueue(identity, common["vm_id"], "verify", payload,
                        job_id=digest({"trial": identity, "patch": patch_hash, "task": task, "kind": "verify"})[:32])
            with row_lock:
                future = graders.submit(grade, folder, checkpoints, grade_jobs)
                grading_futures[future] = common

        def grade(folder, checkpoints, grade_jobs):
            for row in checkpoints:
                identity = row["trial_id"]
                job = pool.await_job(grade_jobs[row["patch_hash"]])
                result = job["result"]
                bad = result.get("status") == "infrastructure_error" or any(
                    c["status"] not in ("passed", "test_failed", "measured") for c in result.get("checks", {}).values())
                graded = {**row, **result, "evaluation_complete": True, "evaluation_job_id": job["id"],
                          "evaluation_status": "needs_review" if bad else "scored",
                          "score_eligible": not bad and row["status"] in ("submitted", "round_timeout") and result.get("environment", {}).get("score_eligible", False),
                          "evaluation_queue_wait_seconds": job["started"] - job["created"]}
                if bad:
                    graded["score"] = None
                atomic_json(folder / f"score-{row['step']}.json", graded)
                print(f"GRADED {identity[:8]} round={row['step']} score={graded.get('score')} vm={row['vm_id']}", flush=True)
                with row_lock:
                    rows[identity, row["step"]] = graded
                    publish()

        with row_lock:
            publish()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        failures = []
        futures = {executor.submit(trial, *job): job for job in jobs}
        try:
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception as error:
                    job = futures[future]
                    failure = {"trial_id": job[3]["trial_id"], "error": str(error)}
                    failures.append(failure)
                    atomic_json(job[2] / "error.json", failure)
                    with row_lock:
                        for row in rows.values():
                            if row["trial_id"] == job[3]["trial_id"] and row["status"] == "queued":
                                row.update(status="infrastructure_error", error=str(error))
                        publish()
            for future in concurrent.futures.as_completed(grading_futures):
                try:
                    future.result()
                except Exception as error:
                    common = grading_futures[future]
                    failure = {"trial_id": common["trial_id"], "error": str(error), "stage": "evaluation"}
                    failures.append(failure)
                    atomic_json(Path(common["artifact_directory"]) / "evaluation-error.json", failure)
            manifest["completed_at"] = time.time()
            manifest["status"] = "Complete" if not failures and all(
                r.get("evaluation_complete") and r.get("score") is not None
                and r["status"] in ("submitted", "round_timeout") for r in rows.values()) else "Partial · needs attention"
        except KeyboardInterrupt:
            stop.set()
            pool.stop.set()
            manifest["interrupted_at"] = time.time()
            manifest["status"] = "Interrupted"
            for future in futures:
                future.cancel()
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            graders.shutdown(wait=True, cancel_futures=True)
            service.close()
            pool.close()
            manifest["failures"] = failures
            atomic_json(manifest_path, manifest)
            with row_lock:
                publish()
        return list(rows.values())
