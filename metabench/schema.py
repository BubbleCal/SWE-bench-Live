"""Small, dependency-free task format; private fields never enter agent prompts."""

import hashlib
import json
import math
import re
from pathlib import Path

from .verification import validate_append

VERSION = 1
DIMENSIONS = {"correctness", "regression", "compatibility", "performance"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def validate_task(task):
    for field in ("instance_id", "repo", "base_commit", "problem_statement", "patch"):
        if not isinstance(task.get(field), str) or not task[field].strip():
            raise ValueError(f"task requires a nonempty {field}")
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", task["instance_id"]):
        raise ValueError("instance_id must be a safe filename")
    if not re.fullmatch(r"[0-9a-f]{40,64}", task["base_commit"]):
        raise ValueError("base_commit must be a full commit hash")
    if not isinstance(task.get("test_patch", ""), str):
        raise ValueError("test_patch must be a string")
    validate_append(task.get("verification_append", {}))
    if task.get("verification_append") and task.get("test_patch", "").strip():
        raise ValueError("use either test_patch or verification_append")
    checks = task.get("checks", [])
    if not checks:
        raise ValueError("task requires executable checks")
    ids = set()
    for check in checks:
        if check.get("id") in ids or not re.fullmatch(r"[a-zA-Z0-9_.-]+", check.get("id", "")):
            raise ValueError("check IDs must be unique safe filenames")
        ids.add(check["id"])
        if check.get("dimension") not in DIMENSIONS:
            raise ValueError("unknown check dimension")
        if not isinstance(check.get("command"), str) or not check["command"].strip():
            raise ValueError("check requires a command")
        weight = check.get("weight", 1)
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not math.isfinite(weight) or weight <= 0:
            raise ValueError("check weight must be finite and positive")
        if check["dimension"] == "performance":
            # The command emits a positive scalar. Thresholds are part of the frozen task.
            if check.get("direction") not in ("lower", "higher"):
                raise ValueError("performance requires direction lower/higher")
            bad, good = check.get("bad"), check.get("good")
            if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x <= 0 for x in (bad, good)):
                raise ValueError("performance requires positive finite bad/good thresholds")
            if (check["direction"] == "lower" and good >= bad) or (check["direction"] == "higher" and good <= bad):
                raise ValueError("performance thresholds have the wrong order")
        elif not check.get("success_pattern"):
            raise ValueError("functional checks require a success_pattern to detect zero/missing tests")
        if check.get("success_pattern"):
            re.compile(check["success_pattern"])
        if check.get("failure_pattern"):
            re.compile(check["failure_pattern"])
    if not any(c.get("critical", False) for c in checks):
        raise ValueError("at least one check must be critical")
    weights = task.get("weights", {})
    active = {c["dimension"] for c in checks}
    if set(weights) != active:
        raise ValueError("weights must name exactly the applicable dimensions")
    if any(isinstance(w, bool) or not isinstance(w, (int, float)) or not math.isfinite(w) or w <= 0 for w in weights.values()):
        raise ValueError("dimension weights must be finite and positive")
    return task


def task_fingerprint(task):
    return digest({k: v for k, v in task.items() if k != "validation"})


def freeze(tasks):
    seen = set()
    for task in tasks:
        validate_task(task)
        if task["instance_id"] in seen:
            raise ValueError("duplicate instance_id")
        seen.add(task["instance_id"])
        validation = task.get("validation", {})
        if validation.get("task_hash") != task_fingerprint(task) or not validation.get("passed"):
            raise ValueError(f"{task['instance_id']}: validate this exact task before freezing")
    if not tasks:
        raise ValueError("cannot freeze an empty suite")
    payload = {"schema_version": VERSION, "tasks": sorted(tasks, key=lambda t: t["instance_id"])}
    return {**payload, "suite_id": digest(payload)}


def load_suite(path):
    suite = read_json(path)
    payload = {k: v for k, v in suite.items() if k != "suite_id"}
    if payload.get("schema_version") != VERSION or digest(payload) != suite.get("suite_id"):
        raise ValueError("suite version/hash mismatch")
    checked = freeze(payload["tasks"])
    if checked["suite_id"] != suite["suite_id"]:
        raise ValueError("suite is not canonical")
    return suite


def public_task(task):
    # Allowlist, not a list of known secrets to remove. New private fields stay private.
    return {k: task[k] for k in ("instance_id", "problem_statement")}
