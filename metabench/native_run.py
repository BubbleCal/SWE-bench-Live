"""Parallel model/effort/issue trials driven by native CLI sessions."""
import concurrent.futures
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from pathlib import Path

from . import usage
from .checkouts import TrialCheckout, trial_id
from .dashboard import write_dashboard
from .native_cli import cli_identity
from .native_round import run_round
from .model_policy import applies as has_model_policy, resolve_agents
from .queue import TestQueue
from .schema import digest, public_task, write_json
from .test_service import TestService
from .vm_pool import SSHVM, VMPool

PROTOCOL = "native-cli-v2-active-budget"
CONTINUE = (
    "Reassess the current patch against the original requirements. "
    "Identify the most consequential correctness assumption that is not yet supported by evidence. "
    "Verify it against the pinned dependency implementation or a minimal executable probe, "
    "and try to construct a counterexample from the public contract. "
    "Derive expected results independently of the implementation being checked; "
    "consider distinct valid input constructions and relevant compositions of operations. "
    "Use public tests to check the result and improve the solution if needed. "
    "Do not treat rerunning unchanged passing checks as new evidence. "
    "If the relevant assumptions are already supported, finish without inventing a defect "
    "or repeating checks unnecessarily. Work within this round's budget, then finish this turn."
)


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    os.replace(temporary, path)


def run_matrix(suite, repo, environments, matrix, out, *, vm_count=None, grading_vm_count=None, parallel=None,
               resume=False, driver_factory=SSHVM, agent_runner=run_round):
    out = Path(out).resolve()
    agents, vms = matrix["agents"], matrix["vms"]
    if not isinstance(agents, list) or not isinstance(vms, list):
        raise ValueError("agents and vms must be lists")
    matrix = {"max_rounds": 4, "round_timeout_seconds": 900, "budget_mode": "active",
              "queue_timeout_seconds": 7200, **matrix}
    if matrix["budget_mode"] not in ("active", "wall"):
        raise ValueError("budget_mode must be active or wall")
    protocol = PROTOCOL if matrix["budget_mode"] == "active" else "native-cli-v2-wall-budget"
    rounds = matrix["max_rounds"]
    repeats = matrix.get("repeats", 1)
    count = vm_count if vm_count is not None else matrix.get("vm_count", len(vms))
    if not agents or not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 1 or not isinstance(repeats, int) or repeats < 1:
        raise ValueError("agents, max_rounds and repeats must be nonempty/positive")
    workers = parallel if parallel is not None else matrix.get("parallel_agents", len(agents) * len(suite["tasks"]) * repeats)
    per_vm_limit = matrix.get("max_parallel_agents_per_vm")
    if per_vm_limit is not None and (isinstance(per_vm_limit, bool) or not isinstance(per_vm_limit, int) or per_vm_limit < 1):
        raise ValueError("max_parallel_agents_per_vm must be positive")
    for name in ("round_timeout_seconds", "test_timeout_seconds", "queue_timeout_seconds"):
        value = matrix.get(name)
        if name == "test_timeout_seconds" and name in matrix and value is None:
            raise ValueError("test_timeout_seconds must be a positive duration")
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0):
            raise ValueError(name + " must be a positive duration or null")
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= len(vms):
        raise ValueError("vm_count must select from configured VMs")
    grading_vms = matrix.get("grading_vms", [])
    if not isinstance(grading_vms, list):
        raise ValueError("grading_vms must be a list")
    grading_count = grading_vm_count if grading_vm_count is not None else matrix.get("grading_vm_count", len(grading_vms))
    if (isinstance(grading_count, bool) or not isinstance(grading_count, int)
            or (not grading_vms and grading_count != 0)
            or (grading_vms and not 1 <= grading_count <= len(grading_vms))):
        raise ValueError("grading_vm_count must select from configured grading VMs")
    chosen = [*vms[:count], *grading_vms[:grading_count]]
    if len({v["id"] for v in chosen}) != len(chosen):
        raise ValueError("public and grading VMs must have distinct identities")
    # Reject aliases of the same declared SSH host/instance. The VM's kernel
    # lock remains the physical execution authority across all controllers.
    for key in ("host", "instance_id"):
        values = [v[key].rsplit("@", 1)[-1].lower() for v in chosen if v.get(key)]
        if len(values) != len(set(values)):
            raise ValueError("public and grading VMs must have distinct " + key + " values")
    if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
        raise ValueError("parallel_agents must be positive")
    for task in suite["tasks"]:
        env = environments["by_task"][task["instance_id"]] if "by_task" in environments else environments
        if digest(env) != task["validation"]["environment_hash"] or task["validation"]["environment"]["backend"] != "docker":
            raise ValueError("native trials require the validated pinned Docker environment")
    for spec in agents:
        if spec.get("provider") not in ("codex", "claude", "dsh") or not isinstance(spec.get("model"), str) or not spec["model"] or not isinstance(spec.get("reasoning"), str) or not spec["reasoning"]:
            raise ValueError("each agent must explicitly select provider, model and reasoning")
    if resume:
        saved_manifest = out / "run.json"
        if not saved_manifest.exists():
            raise ValueError("no recorded run exists to resume")
        frozen_agents = json.loads(saved_manifest.read_text())["matrix"]["agents"]
        if len(agents) != len(frozen_agents):
            raise ValueError("resume agent configuration changed")
        for requested, frozen in zip(agents, frozen_agents):
            if ({k: v for k, v in requested.items() if k != "model_policy"}
                    != {k: v for k, v in frozen.items() if k != "model_policy"}
                    or ("model_policy" in requested and requested["model_policy"] != frozen.get("model_policy"))):
                raise ValueError("resume agent configuration changed")
            if has_model_policy(frozen) and "model_policy" not in frozen:
                raise ValueError("cannot add the GPT context policy to an already-started legacy run")
        agents = resolve_agents(frozen_agents, refresh=False)
    else:
        agents = resolve_agents(agents)
    matrix = {**matrix, "agents": agents}
    identities = {}
    for spec in agents:
        key = json.dumps(spec.get("command", [spec["provider"]]))
        if key not in identities:
            identities[key] = cli_identity(spec)
    from .performance import PERFORMANCE_POLICY_ID
    from .scoring import EVALUATION_POLICY_ID
    configuration = {"protocol": protocol, "performance_policy_id": PERFORMANCE_POLICY_ID, "evaluation_policy_id": EVALUATION_POLICY_ID,
                     "suite_id": suite["suite_id"], "matrix": matrix,
                     "vm_count": count, "grading_vm_count": grading_count,
                     "worker_pools": {"public": [v["id"] for v in vms[:count]],
                                      "grading": [v["id"] for v in grading_vms[:grading_count]] if grading_count else [v["id"] for v in vms[:count]],
                                      "separate": bool(grading_count)},
                     "checkpoint_execution_policy": "isolated-cached-benchmarks-v1",
                     "parallel_agents": workers, "environments": environments,
                     "native_cli": identities, "evaluation_schedule": "checkpoint-pipeline-v1",
                     "trial_order": "task-then-configuration",
                     "continuation_prompt": CONTINUE,
                     "execution_source_hash": digest({name: Path(__file__).with_name(name).read_text() for name in (
                         "native_run.py", "native_cli.py", "dsh_cli.py", "native_round.py", "model_policy.py", "checkouts.py", "queue.py", "test_service.py", "test_mcp.py",
                         "vm_pool.py", "vm_worker.py", "checkpoint_eval.py", "benchmark_build.py", "performance.py", "scoring.py", "verification.py", "evaluate.py", "process.py", "usage.py", "schema.py")})}
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
        for task in suite["tasks"]:
            for spec in agents:
                config_id = digest({"experiment": experiment, "agent": spec})
                for repeat in range(repeats):
                    identity = trial_id({"experiment": experiment, "run_id": manifest["run_id"], "agent": spec}, task, repeat)
                    vm = vms[len(jobs) % count]["id"]
                    grading_vm = grading_vms[len(jobs) % grading_count]["id"] if grading_count else vm
                    label = re.sub(r"[^a-zA-Z0-9_.-]", "-", f"{spec['model']}-{spec['reasoning']}-{task['instance_id']}")[:100]
                    folder = out / "trials" / (label + "-" + identity[:8])
                    common = {"suite_id": suite["suite_id"], "protocol_id": protocol,
                              "performance_policy_id": PERFORMANCE_POLICY_ID, "evaluation_policy_id": EVALUATION_POLICY_ID, "config_id": config_id,
                              "model": spec["model"], "reasoning": spec["reasoning"], "provider": spec["provider"],
                              "task_id": task["instance_id"], "repeat": repeat, "trial_id": identity, "vm_id": vm,
                              "public_vm_id": vm, "grading_vm_id": grading_vm,
                              "artifact_directory": str(folder), "score_dimensions":
                              ["functional", "performance"] if task.get("supplemental") else sorted(task["weights"])}
                    if "model_policy" in spec:
                        common["model_policy"] = spec["model_policy"]
                    jobs.append((spec, task, folder, common))
                    for step in range(1, rounds + 1):
                        saved = folder / f"score-{step}.json"
                        rows[identity, step] = json.loads(saved.read_text()) if saved.exists() else {
                            **common, "step": step, "status": "queued", "score": None, "scores": {}, "score_eligible": False}
        if len({j[3]["trial_id"] for j in jobs}) != len(jobs):
            raise ValueError("duplicate model/effort/issue configuration")
        pool = VMPool(queue, vms, out / "control", vm_count=count, driver_factory=driver_factory,
                      kinds={"public"} if grading_count else None)
        grading_pool = VMPool(queue, grading_vms, out / "grading-control", vm_count=grading_count,
                              driver_factory=driver_factory, kinds={"verify"}) if grading_count else pool
        drivers = {**pool.drivers, **grading_pool.drivers}
        task_environments = list(environments["by_task"].values()) if "by_task" in environments else [environments]
        manifest["vm_preflight"] = {name: driver.prepare(task_environments) for name, driver in drivers.items()}
        # Share immutable base bytes, never a trial's mutable Git object database.
        # Large transfers finish before any native model's budget starts.
        manifest["status"] = "Preparing base assets"
        atomic_json(manifest_path, manifest)
        base_assets = out / "base-assets"
        base_assets.mkdir(exist_ok=True)
        required = {name: set() for name in drivers}
        for spec, task, folder, common in jobs:
            checkout = TrialCheckout(repo, task["base_commit"], folder / "checkout")
            canonical = base_assets / (task["base_commit"] + ".tar")
            if not canonical.exists():
                supplied = matrix.get("base_archives", {}).get(task["base_commit"])
                if supplied:
                    source = Path(supplied["path"])
                    if hashlib.sha256(source.read_bytes()).hexdigest() != supplied["sha256"]:
                        raise ValueError("prebuilt base archive checksum mismatch")
                    with tarfile.open(source) as archive:
                        metadata = json.load(archive.extractfile("base.json"))
                    expected = json.loads(checkout.manifest.read_text())
                    if metadata.get("commit") != checkout.base or metadata.get("tree") != expected["source_tree"]:
                        raise ValueError("prebuilt archive belongs to a different Git base")
                    shutil.copyfile(source, canonical)
                else:
                    checkout.base_archive(canonical)
            base = folder / "base.tar"
            if not base.exists():
                shutil.copyfile(canonical, base)
            required[common["vm_id"]].add(base)
            required[common["grading_vm_id"]].add(base)
        def stage(name):
            return [drivers[name].stage_base(base) for base in sorted(required[name])]
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(drivers)) as preparation:
                manifest["base_asset_preflight"] = dict(zip(drivers, preparation.map(stage, drivers)))
        except Exception as error:
            manifest.update(status="Preflight failed", failures=[{"stage": "base-assets", "error": str(error)}])
            atomic_json(manifest_path, manifest)
            raise
        manifest.setdefault("models_started_at", time.time())
        manifest["status"] = "Running"
        atomic_json(manifest_path, manifest)
        pool.start()
        if grading_pool is not pool:
            grading_pool.start()
        service = TestService(queue, pool)
        graders = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(jobs)))
        grading_futures = {}

        def publish():
            ordered = sorted(rows.values(), key=lambda r: (r["model"], r["reasoning"], r["task_id"], r["repeat"], r["step"]))
            temporary = out / "results.jsonl.tmp"
            temporary.write_text("".join(json.dumps(r, allow_nan=False) + "\n" for r in ordered))
            os.replace(temporary, out / "results.jsonl")
            topology = f"{count} public + {grading_count} grading VMs" if grading_count else f"{count} VMs"
            metadata = {"title": "Native CLI benchmark", "subtitle": f"{len(jobs)} independent trials · {topology} · {workers} parallel agents",
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
            active_seconds = queue_seconds = infrastructure_seconds = 0.0
            def public_wait(start, end):
                # A failed worker cannot provide test feedback. Abort the native
                # turn instead of repeatedly returning errors while queued jobs
                # keep extending its active-time allowance.
                error = pool.errors.get(common["vm_id"])
                if error:
                    raise RuntimeError("public test VM unavailable: " + error)
                return queue.queued_seconds(identity, start, end)

            def submit_grade(row):
                # The payload owns an immutable patch string. Grading never reads
                # the live checkout that the next native turn is already editing.
                if rows[identity, row["step"]].get("evaluation_complete"):
                    return
                payload = {"base_archive": str(base), "environment": env, "task": task,
                           "patch": (folder / f"step-{row['step']}.patch").read_text()}
                job = queue.enqueue(identity, common["grading_vm_id"], "verify", payload,
                    job_id=digest({"trial": identity, "patch": row["patch_hash"], "task": task, "kind": "verify"})[:32])
                with row_lock:
                    future = graders.submit(grade, folder, [row], {row["patch_hash"]: job})
                    grading_futures[future] = common

            for step in range(1, rounds + 1):
                checkpoint = folder / f"checkpoint-{step}.json"
                if checkpoint.exists():
                    row = json.loads(checkpoint.read_text())
                    session, cumulative, seconds = row.get("session_id"), row["usage"], row["seconds"]
                    active_seconds = row.get("active_seconds", seconds)
                    queue_seconds = row.get("queue_wait_seconds", 0.0)
                    infrastructure_seconds = row.get("infrastructure_wait_seconds", 0.0)
                    previous_usage = row.get("native_turn", {}).get("provider", {}).get("session_usage")
                    observed = {k: row.get("observed_usage_lower_bound", {}).get(k, row["usage"].get(k) or 0) for k in observed}
                    submit_grade(row)
                    if row["status"] not in ("submitted", "round_timeout"):
                        service.seal(identity)
                        return
                    continue
                if stop.is_set():
                    break
                prompt = (json.dumps(public_task(task)) + "\n\nSolve this task using your native tools. Do not commit or change Git metadata. "
                          "Use the bench.run_tests MCP tool for all compilation and public tests on the VM; do not compile on this host. "
                          "Only use this worktree, the task statement, and the source or documentation for its pinned dependencies. "
                          "Do not inspect other trial directories or seek historical solutions. "
                          "The test VM has its own persistent build cache. Hidden scoring is not feedback.\n"
                          "Test VM context (not the local working directory):\n" + env.get("agent_instructions", "")) if step == 1 else CONTINUE
                limit = matrix["round_timeout_seconds"]
                budget_text = ("active budget, excluding public-test queue and VM-lock waits; "
                               "reasoning, editing and actual test execution count toward the budget. "
                               if matrix["budget_mode"] == "active" else "wall-clock budget, including VM queue waits. ")
                prompt += (f"\nConversation round {step}/{rounds}. "
                           + (f"This round has a {limit:g}-second " + budget_text if limit is not None else "")
                           + f"A public test command may request at most {matrix.get('test_timeout_seconds', 1800):g} seconds of VM execution.")
                turn_dir = folder / f"turn-{step}"
                if turn_dir.exists() and agent_runner is not run_round:
                    raise RuntimeError(f"{identity} turn {step} was interrupted before its checkpoint; inspect saved events before retrying")
                turn = agent_runner(spec, checkout.root, prompt, turn_dir, session_id=session, mcp_path=mcp_path,
                                    timeout=matrix.get("round_timeout_seconds"), stop=stop, previous_usage=previous_usage,
                                    queue_wait=public_wait,
                                    budget_mode=matrix["budget_mode"],
                                    queue_timeout=matrix["queue_timeout_seconds"] if matrix["budget_mode"] == "active" else None)
                session = turn.get("session_id")
                previous_usage = turn.get("provider", {}).get("session_usage")
                cumulative = usage.add(cumulative, turn["usage"])
                known = turn.get("observed_usage_lower_bound", turn["usage"])
                if spec["provider"] == "codex" and previous_usage is not None:
                    observed = {k: max(observed[k], previous_usage.get(k) or 0) for k in observed}
                else:
                    observed = {k: observed[k] + (known.get(k) or 0) for k in observed}
                seconds += turn["seconds"]
                active_seconds += turn.get("active_seconds", turn["seconds"])
                queue_seconds += turn.get("queue_wait_seconds", 0.0)
                infrastructure_seconds += turn.get("infrastructure_wait_seconds", 0.0)
                patch = service.snapshot(identity)
                (folder / f"step-{step}.patch").write_text(patch)
                row = {**common, "step": step, "session_id": session, "status": turn["status"],
                       "usage": dict(cumulative), "step_usage": turn["usage"], "seconds": seconds,
                       "wall_seconds": seconds, "active_seconds": active_seconds,
                       "queue_wait_seconds": queue_seconds, "budget_mode": matrix["budget_mode"],
                       "infrastructure_wait_seconds": infrastructure_seconds,
                       "native_turn": turn, "patch_hash": digest(patch), "score": None, "scores": {},
                       "observed_usage_lower_bound": {**observed, "total_tokens": observed["input_tokens"] + observed["output_tokens"]},
                       "score_eligible": False, "agent_steps": step}
                atomic_json(checkpoint, row)
                print(f"TRIAL {identity[:8]} {spec['model']}/{spec['reasoning']} {task['instance_id']} round={step} {turn['status']}", flush=True)
                with row_lock:
                    rows[identity, step] = row
                    publish()
                submit_grade(row)
                if turn["status"] not in ("submitted", "round_timeout") or not session:
                    break
            service.seal(identity)

        def grade(folder, checkpoints, grade_jobs):
            for row in checkpoints:
                identity = row["trial_id"]
                job = grading_pool.await_job(grade_jobs[row["patch_hash"]])
                result = job["result"]
                bad = result.get("status") == "infrastructure_error" or result.get("supplemental_status") == "needs_review" or any(
                    c["status"] not in ("passed", "test_failed", "measured") for c in result.get("checks", {}).values())
                graded = {**row, **result, "evaluation_complete": True, "evaluation_job_id": job["id"],
                          "initial_vm_id": row["vm_id"], "vm_id": job["vm_id"],
                          "evaluation_status": "needs_review" if bad else "scored",
                          "score_eligible": not bad and row["status"] in ("submitted", "round_timeout") and result.get("environment", {}).get("score_eligible", False),
                          "evaluation_queue_wait_seconds": job["started"] - job["created"]}
                if bad:
                    graded["score"] = None
                atomic_json(folder / f"score-{row['step']}.json", graded)
                print(f"GRADED {identity[:8]} round={row['step']} score={graded.get('score')} vm={graded['vm_id']}", flush=True)
                with row_lock:
                    rows[identity, row["step"]] = graded
                    publish()

        with row_lock:
            publish()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        failures = []
        futures = {}
        pending = list(jobs)
        active_vms = {}
        def completed_trials():
            while pending or futures:
                while len(futures) < workers:
                    index = next((i for i, job in enumerate(pending)
                                  if per_vm_limit is None or active_vms.get(job[3]["vm_id"], 0) < per_vm_limit), None)
                    if index is None:
                        break
                    job = pending.pop(index)
                    vm = job[3]["vm_id"]
                    active_vms[vm] = active_vms.get(vm, 0) + 1
                    futures[executor.submit(trial, *job)] = job
                finished, _ = concurrent.futures.wait(futures, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in finished:
                    job = futures.pop(future)
                    active_vms[job[3]["vm_id"]] -= 1
                    yield future, job
        try:
            for future, job in completed_trials():
                try:
                    future.result()
                except Exception as error:
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
            # Native timeouts may leave an already-requested public job running.
            # Collect it once before releasing either worker pool.
            with queue.connect() as db:
                trailing = [dict(row) for row in db.execute("SELECT id,kind FROM jobs WHERE status!='completed'")]
            for job in trailing:
                try:
                    (grading_pool if job["kind"] == "verify" else pool).await_job(job["id"])
                except Exception as error:
                    failures.append({"job_id": job["id"], "stage": "queue-drain", "error": str(error)})
            manifest["completed_at"] = time.time()
            manifest["status"] = "Complete" if not failures and all(
                r.get("evaluation_complete") and r.get("score") is not None
                and r["status"] in ("submitted", "round_timeout") for r in rows.values()) else "Partial · needs attention"
        except KeyboardInterrupt:
            stop.set()
            pool.stop.set()
            grading_pool.stop.set()
            manifest["interrupted_at"] = time.time()
            manifest["status"] = "Interrupted"
            for future in futures:
                future.cancel()
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            graders.shutdown(wait=True, cancel_futures=True)
            service.close()
            pool.close()
            if grading_pool is not pool:
                grading_pool.close()
            manifest["failures"] = failures
            atomic_json(manifest_path, manifest)
            with row_lock:
                publish()
        return list(rows.values())
