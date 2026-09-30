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


def checked(workspace, command, *, source=None, timeout=900):
    result = workspace.command(command, timeout, source)
    if result["returncode"]:
        raise RuntimeError(result["output"][-8000:])
    return result


def install(workspace, spec):
    directory = "rust/" + spec["crate"]
    path = directory + "/benches/metabench_perf.rs"
    checked(workspace, "test ! -e " + path)
    checked(workspace, "mkdir -p " + directory + "/benches")
    checked(workspace, "cat > " + path, source=spec["benchmark_source"])
    checked(workspace, "cat >> " + directory + "/Cargo.toml",
            source='\n[[bench]]\nname = "metabench_perf"\nharness = false\n')


def build(workspace, spec):
    install(workspace, spec)
    features = " --features " + ",".join(spec["features"]) if spec.get("features") else ""
    result = workspace.command(f"cargo bench --locked --profile release-with-debug -p {spec['crate']} "
                               f"--bench metabench_perf --no-run --message-format=json{features}", 900)
    artifacts = []
    for line in result["output"].splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("reason") == "compiler-artifact" and event.get("target", {}).get("name") == "metabench_perf" and event.get("executable"):
            artifacts.append(event["executable"])
    if result["returncode"] or len(artifacts) != 1:
        return None, result
    return artifacts[0], result


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
    reference_request = {**request, "trial_id": digest({"trial": request["trial_id"], "reference": True})[:24]}
    reference = CachedWorkspace(root, reference_request, "verify")
    evidence = {"protocol": "paired-checkpoint-v2-functional-performance", "evaluation_policy_id": EVALUATION_POLICY_ID, "performance_policy_id": PERFORMANCE_POLICY_ID,
                "spec_hash": digest(spec), "pairs": []}
    try:
        checked(reference, "true")
        if reference.restore_source_ownership()["returncode"]:
            raise RuntimeError("reference ownership recovery failed")
        reference.prepare([request["task"]["patch"]])
        refbin, evidence["reference_build"] = build(reference, spec)
        if not refbin:
            raise RuntimeError("historical reference performance build failed")
        if candidate.restore_source_ownership()["returncode"]:
            raise RuntimeError("candidate ownership recovery failed")
        candidate.prepare([request["patch"]])
        if functional["critical_pass"]:
            binary, evidence["candidate_build"] = build(candidate, spec)
            if binary:
                for repeat in range(spec["repeats"]):
                    pair = {}
                    for side in (("reference", "candidate") if repeat % 2 == 0 else ("candidate", "reference")):
                        pair[side] = sample(reference if side == "reference" else candidate, refbin if side == "reference" else binary)
                    evidence["pairs"].append(pair)
                    if not all(value["valid"] for value in pair.values()):
                        break
        metrics = performance_scores(evidence["pairs"], spec["repeats"], spec.get("tolerance"))
        performance = statistics.mean(m["score"] for m in metrics) if metrics else None
        performance_status = "excluded_incorrect_solution" if not functional["critical_pass"] else ("measured" if metrics else "measurement_failed")
        scores = {"functional": functional["score"], "performance": performance}
        total = combined_score(scores["functional"], performance, functional["critical_pass"])
        return {**functional, "functional_score": functional["score"], "functional_scores": functional["scores"],
                "performance_policy_id": PERFORMANCE_POLICY_ID, "evaluation_policy_id": EVALUATION_POLICY_ID,
                "scores": scores, "score": total, "performance_metrics": metrics or [], "performance_status": performance_status,
                "supplemental_evaluation": evidence,
                "supplemental_status": "needs_review" if performance_status == "measurement_failed" else "scored"}
    finally:
        ownership = reference.restore_source_ownership()
        stopped = execute(["docker", "stop", "--time", "2", reference.container], timeout=30)
        if ownership["returncode"] or stopped["returncode"]:
            raise RuntimeError("reference container cleanup failed")
