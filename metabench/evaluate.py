"""Evidence-based grading and preflight validation of historical tasks."""

import math
import re
import statistics
import time

from .runtime import Workspace, RuntimeErrorWithLog
from .schema import task_fingerprint, validate_task, write_json, digest


def summarize(task, results):
    dimensions = {}
    for dimension in task["weights"]:
        checks = [c for c in task["checks"] if c["dimension"] == dimension]
        dimensions[dimension] = sum(results[c["id"]]["score"] * c.get("weight", 1) for c in checks) / sum(c.get("weight", 1) for c in checks)
    critical_pass = all(results[c["id"]]["passed"] for c in task["checks"] if c.get("critical"))
    progress = sum(dimensions[d] * w for d, w in task["weights"].items()) / sum(task["weights"].values())
    return {"scores": dimensions, "critical_pass": critical_pass,
            "progress_score": progress, "score": progress if critical_pass else 0.0,
            "checks": results}


def evaluate(repo, task, patch, environment, *, trusted_local=False):
    validate_task(task)
    results = {}
    with Workspace(repo, task, environment, trusted_local=trusted_local) as workspace:
        identity = workspace.identity
        try:
            workspace.apply(patch)
            workspace.apply(task.get("test_patch", ""))
        except RuntimeErrorWithLog as error:
            # A candidate which conflicts with the fixed tests is a reviewable failure,
            # not grounds for silently adapting the tests to that candidate.
            for check in task["checks"]:
                results[check["id"]] = {"passed": False, "score": 0.0, "status": "patch_failed", "output": str(error)}
            return {**summarize(task, results), "environment": identity}
        for command in environment.get("setup", []):
            setup = workspace.command(command, environment.get("setup_timeout", 1800))
            if setup["returncode"]:
                for check in task["checks"]:
                    results[check["id"]] = {"passed": False, "score": 0.0, "status": "build_failed", **setup}
                return {**summarize(task, results), "environment": identity}
        for check in task["checks"]:
            samples = []
            executions = []
            performance = check["dimension"] == "performance"
            count = check.get("repeats", 5) if performance else 1
            if not isinstance(count, int) or count < 1:
                raise ValueError("check repeats must be positive")
            for _ in range(count):
                execution = workspace.command(check["command"], check.get("timeout", 300))
                executions.append(execution)
                if execution["returncode"] or execution["timed_out"]:
                    break
                if performance:
                    try:
                        value = float(execution["output"].strip())
                        if not math.isfinite(value) or value <= 0:
                            break
                        samples.append(value)
                    except ValueError:
                        break
            if performance:
                valid = len(samples) == count
                value = statistics.median(samples) if valid else None
                score = 100 * max(0, min(1, (value - check["bad"]) / (check["good"] - check["bad"]))) if valid else 0.0
                passed = valid and (value <= check["good"] if check["direction"] == "lower" else value >= check["good"])
                results[check["id"]] = {"passed": passed, "score": score, "status": "measured" if valid else "measurement_failed", "samples": samples, "median": value, "executions": executions}
            else:
                execution = executions[0]
                passed = execution["returncode"] == 0 and not execution["timed_out"] and bool(re.search(check["success_pattern"], execution["output"], re.MULTILINE))
                expected_failure = bool(check.get("failure_pattern")) and bool(re.search(check["failure_pattern"], execution["output"], re.MULTILINE)) and not execution["timed_out"]
                status = "passed" if passed else ("test_failed" if expected_failure else "execution_failed")
                results[check["id"]] = {"passed": passed, "score": 100.0 if passed else 0.0, "status": status, "executions": executions}
        return {**summarize(task, results), "environment": identity}


def validate(repo, task, environment, out, *, trusted_local=False, repeats=3):
    validate_task(task)
    if repeats < 2:
        raise ValueError("validation requires at least two repetitions")
    references, baselines = [], []
    for repeat in range(repeats):
        baseline = evaluate(repo, task, "", environment, trusted_local=trusted_local)
        reference = evaluate(repo, task, task["patch"], environment, trusted_local=trusted_local)
        baselines.append(baseline)
        references.append(reference)
        write_json(out / f"base-{repeat}.json", baseline)
        write_json(out / f"reference-{repeat}.json", reference)
    # Always distinguish the target behavior, including performance-only tasks.
    targets = [c["id"] for c in task["checks"] if c["dimension"] in ("correctness", "performance")]
    distinguishes = any(all(not b["checks"][name]["passed"] and b["checks"][name]["status"] in ("test_failed", "measured") for b in baselines) for name in targets)
    reference_pass = all(all(c["passed"] for c in r["checks"].values()) for r in references)
    stable_baseline = all([c["passed"] for c in b["checks"].values()] == [c["passed"] for c in baselines[0]["checks"].values()] for b in baselines)
    # A broken build/patch application on the base is not a reproduced bug.
    baseline_runnable = all(all(c["status"] not in ("build_failed", "patch_failed", "execution_failed", "measurement_failed") for c in b["checks"].values()) for b in baselines)
    passed = distinguishes and reference_pass and stable_baseline and baseline_runnable
    task = {**task, "validation": {"task_hash": task_fingerprint(task), "environment_hash": digest(environment),
                                   "passed": passed, "repeats": repeats,
                                   "baseline_distinguished": distinguishes,
                                   "reference_pass": reference_pass,
                                   "stable_baseline": stable_baseline,
                                   "environment": references[0]["environment"],
                                   "timestamp": time.time()}}
    write_json(out / "task.json", task)
    return task


def grade_live_status(instance, status):
    """Grade upstream status maps strictly: skipped/missing regressions cannot pass."""
    groups = {"correctness": instance["FAIL_TO_PASS"], "regression": instance["PASS_TO_PASS"]}
    if not groups["correctness"]:
        raise ValueError("FAIL_TO_PASS must not be empty")
    scores = {}
    failures = {}
    for dimension, tests in groups.items():
        unique = set(tests)
        failures[dimension] = sorted(t for t in unique if status.get(t, "missing").lower() != "pass")
        scores[dimension] = 100 * (len(unique) - len(failures[dimension])) / len(unique) if unique else None
    passed = not any(failures.values())
    return {"scores": scores, "critical_pass": passed, "score": 100.0 if passed else 0.0, "failures": failures}
