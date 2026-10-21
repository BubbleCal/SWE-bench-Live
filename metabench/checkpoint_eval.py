"""Frozen per-checkpoint functional and performance evaluation.

Called only by a VM worker while it holds the machine-wide execution lock.
All supplied Rust runs inside the offline verifier containers.
"""
import json
import math
import shlex
import statistics

from .schema import digest
from .performance import metric_score, PERFORMANCE_POLICY_ID
from .scoring import EVALUATION_POLICY_ID, combined_score
from .benchmark_build import build


def sample(workspace, binary):
    result = workspace.command("taskset -c 2 " + shlex.quote(binary), 120)
    metrics = []
    try:
        metrics = [json.loads(line[len("MB_METRIC "):]) for line in result["output"].splitlines()
                   if line.startswith("MB_METRIC ")]
        valid = (result["returncode"] == 0 and bool(metrics)
                 and len({m["name"] for m in metrics}) == len(metrics)
                 and all(isinstance(m["value"], (int, float)) and math.isfinite(m["value"]) and m["value"] > 0 for m in metrics))
    except (ValueError, KeyError, TypeError):
        valid = False
    return {"valid": valid, "metrics": metrics if valid else [], "execution": result}


def performance_scores(pairs, repeats, tolerance):
    """Score median paired latency ratios; tolerance is a legacy, unused input."""
    if len(pairs) != repeats or any(not p[side]["valid"] for p in pairs for side in ("reference", "candidate")):
        return None
    names = [(m["name"], m["unit"]) for m in pairs[0]["reference"]["metrics"]]
    if any([(m["name"], m["unit"]) for m in p[side]["metrics"]] != names
           for p in pairs for side in ("reference", "candidate")):
        return None
    metrics = []
    for i, (name, unit) in enumerate(names):
        reference = [p["reference"]["metrics"][i]["value"] for p in pairs]
        candidate = [p["candidate"]["metrics"][i]["value"] for p in pairs]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0
               for v in reference + candidate):
            return None
        ratios = [a / b for a, b in zip(reference, candidate)]
        if any(not math.isfinite(v) or v <= 0 for v in ratios):
            return None
        metrics.append({"name": name, "unit": unit, "reference_samples": reference, "candidate_samples": candidate,
                        "reference_median": statistics.median(reference), "candidate_median": statistics.median(candidate),
                        "paired_reference_over_candidate": ratios, "speedup": statistics.median(ratios),
                        "score": metric_score(statistics.median(ratios)),
                        "performance_policy_id": PERFORMANCE_POLICY_ID})
    return metrics


def evaluate(root, request, candidate, functional):
    # Import lazily because vm_worker owns workspace construction and the lock.
    from .vm_worker import CachedWorkspace, execute
    spec = request["task"]["supplemental"]
    evidence = {"protocol": "paired-checkpoint-v2-functional-performance", "evaluation_policy_id": EVALUATION_POLICY_ID, "performance_policy_id": PERFORMANCE_POLICY_ID,
                "spec_hash": digest(spec), "pairs": []}
    # Frozen task validation already established a runnable reference. A failing
    # candidate has no performance score; do not compile either benchmark for it.
    # Keep original check statuses so an infrastructure failure remains reviewable.
    if not functional["critical_pass"]:
        return scored_result(functional, evidence, spec)
    reference_request = {**request, "trial_id": digest({
        "role": "reference-performance-v1", "base_sha256": request["base_sha256"],
        "environment": request["environment"], "patch": request["task"]["patch"], "spec": spec})[:24]}
    workspaces = []
    try:
        reference = CachedWorkspace(root, reference_request, "reference")
        workspaces.append(reference)
        if reference.restore_source_ownership()["returncode"]:
            raise RuntimeError("reference ownership recovery failed")
        reference.prepare([request["task"]["patch"]], benchmark=spec)
        refbin, evidence["reference_build"] = build(reference, spec)
        if not refbin:
            raise RuntimeError("historical reference performance build failed")
        # Never strip the hidden tests out of the functional verification tree.
        # Separate seeds, source trees and container layers retain both builds.
        candidate = CachedWorkspace(root, request, "performance")
        workspaces.append(candidate)
        if candidate.restore_source_ownership()["returncode"]:
            raise RuntimeError("candidate ownership recovery failed")
        candidate.prepare([request["patch"]], benchmark=spec)
        binary, evidence["candidate_build"] = build(candidate, spec)
        if binary:
            for repeat in range(spec["repeats"]):
                pair = {}
                for side in (("reference", "candidate") if repeat % 2 == 0 else ("candidate", "reference")):
                    pair[side] = sample(reference if side == "reference" else candidate, refbin if side == "reference" else binary)
                evidence["pairs"].append(pair)
                if not all(value["valid"] for value in pair.values()):
                    break
        return scored_result(functional, evidence, spec)
    finally:
        failures = []
        for workspace in workspaces:
            ownership = workspace.restore_source_ownership()
            stopped = execute(["docker", "stop", "--time", "2", workspace.container], timeout=30)
            if ownership["returncode"] or stopped["returncode"]:
                failures.append(workspace.container)
        if failures:
            raise RuntimeError("performance container cleanup failed: " + ", ".join(failures))


def scored_result(functional, evidence, spec):
    metrics = performance_scores(evidence["pairs"], spec["repeats"], spec.get("tolerance"))
    performance = statistics.mean(m["score"] for m in metrics) if metrics else None
    status = "excluded_incorrect_solution" if not functional["critical_pass"] else ("measured" if metrics else "measurement_failed")
    scores = {"functional": functional["score"], "performance": performance}
    bad_checks = any(c.get("status") not in ("passed", "test_failed", "measured")
                     for c in functional.get("checks", {}).values())
    return {**functional, "functional_score": functional["score"], "functional_scores": functional["scores"],
            "performance_policy_id": PERFORMANCE_POLICY_ID, "evaluation_policy_id": EVALUATION_POLICY_ID,
            "scores": scores, "score": combined_score(scores["functional"], performance, functional["critical_pass"]),
            "performance_metrics": metrics or [], "performance_status": status,
            "supplemental_evaluation": evidence,
            "supplemental_status": "needs_review" if bad_checks or status == "measurement_failed" else "scored"}
