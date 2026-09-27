"""Run reproducible trajectories; evaluate only after generation has ended."""

import platform
import math
import time
from pathlib import Path

from .agent import CommandModel, trajectory
from ..evaluate import evaluate
from ..runtime import Workspace
from ..schema import digest, write_json


def task_environment(environment, task_id):
    return environment["by_task"][task_id] if "by_task" in environment else environment


def run(suite, repo, environment, command, model, reasoning, budget, out, *, repeats=1, trusted_local=False):
    from ..charts import plotting_backend
    from ..report import write_report

    if repeats < 1 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in budget.values()):
        raise ValueError("all budgets and repeats must be positive")
    if any(not isinstance(budget[k], int) for k in ("max_steps", "max_calls_per_step", "max_tool_output")):
        raise ValueError("step, call, and output counts must be integers")
    if not command:
        raise ValueError("model adapter command is required")
    if out.exists():
        raise ValueError("output exists; use a new run directory to preserve prior evidence")
    for task in suite["tasks"]:
        if task["validation"]["environment_hash"] != digest(task_environment(environment, task["instance_id"])):
            raise ValueError("environment differs from the one used to validate the task")
        if task["validation"]["environment"]["backend"] != ("trusted-local" if trusted_local else "docker"):
            raise ValueError("validation and execution backends must match")
    plotting_backend()  # Fail before model calls or output creation if reports are unavailable.
    out.mkdir(parents=True)
    configuration = {"suite_id": suite["suite_id"], "model": model, "reasoning": reasoning,
                     "adapter_command": command, "budget": budget, "environment": environment,
                     "repeats": repeats, "trusted_local": trusted_local,
                     "harness_version": "0.1.0", "host_platform": platform.platform(),
                     "harness_hash": digest({p.name: p.read_bytes().hex() for p in Path(__file__).parent.glob("*.py") if not p.name.startswith(".")}),
                     "adapter_file_hashes": {arg: digest(Path(arg).read_bytes().hex()) for arg in command if Path(arg).is_file()}}
    config_id = digest(configuration)
    write_json(out / "run.json", {**configuration, "config_id": config_id, "started_at": time.time()})
    rows = []
    adapter = CommandModel(command, model, reasoning)
    for task in suite["tasks"]:
        runtime_environment = task_environment(environment, task["instance_id"])
        for repeat in range(repeats):
            folder = out / task["instance_id"] / str(repeat)
            folder.mkdir(parents=True)
            try:
                with Workspace(repo, task, runtime_environment, trusted_local=trusted_local) as workspace:
                    for setup in runtime_environment.get("agent_setup", []):
                        workspace.must(setup, runtime_environment.get("setup_timeout", 1800))
                    snapshots = trajectory(workspace, task, adapter, budget, folder)
                    runtime_identity = workspace.identity
            except Exception as error:
                row = {"suite_id": suite["suite_id"], "config_id": config_id,
                       "task_id": task["instance_id"], "repeat": repeat, "model": model,
                       "score_dimensions": sorted(task["weights"]),
                       "reasoning": reasoning, "step": None, "score": None,
                       "status": "infrastructure_error", "error": str(error)}
                rows.extend({**row, "step": step} for step in range(1, budget["max_steps"] + 1))
                write_json(folder / "error.json", row)
                continue
            for snapshot in snapshots:
                patch = snapshot.pop("patch")
                identity = {"suite_id": suite["suite_id"], "config_id": config_id,
                            "task_id": task["instance_id"], "repeat": repeat,
                            "score_dimensions": sorted(task["weights"]),
                            "model": model, "reasoning": reasoning,
                            "patch_hash": digest(patch), **snapshot,
                            "score_eligible": runtime_identity["score_eligible"]}
                try:
                    result = evaluate(repo, task, patch, runtime_environment, trusted_local=trusted_local)
                    row = {**identity, **result}
                except Exception as error:
                    row = {**identity, "status": "evaluation_error", "score": None, "error": str(error)}
                write_json(folder / f"score-{snapshot['step']}.json", row)
                rows.append(row)
            for step in range(len(snapshots) + 1, budget["max_steps"] + 1):
                rows.append({"suite_id": suite["suite_id"], "config_id": config_id,
                             "task_id": task["instance_id"], "repeat": repeat, "model": model,
                             "score_dimensions": sorted(task["weights"]),
                             "reasoning": reasoning, "step": step, "score": None,
                             "status": "not_run_after_agent_error"})
    # JSONL is a direct (model, reasoning, step, score) query surface with provenance.
    import json
    (out / "results.jsonl").write_text("".join(json.dumps(row, allow_nan=False) + "\n" for row in rows))
    write_report(rows, out / "report.md")
    return rows
