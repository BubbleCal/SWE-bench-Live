"""Frozen per-checkpoint performance and downstream compatibility evaluation.

Called only by a VM worker while it holds the machine-wide execution lock.
All supplied Rust runs inside the offline verifier containers.
"""
import json
import math
import re
import shlex
import statistics

from .schema import digest


def checked(workspace, command, *, source=None, timeout=900):
    result = workspace.command(command, timeout, source)
    if result["returncode"]:
        raise RuntimeError(result["output"][-8000:])
    return result


def install(workspace, spec, kind):
    directory = "rust/" + spec["crate"]
    if kind == "benchmark":
        path = directory + "/benches/metabench_perf.rs"
        checked(workspace, "test ! -e " + path)
        checked(workspace, "mkdir -p " + directory + "/benches")
        checked(workspace, "cat > " + path, source=spec["benchmark_source"])
        checked(workspace, "cat >> " + directory + "/Cargo.toml",
                source='\n[[bench]]\nname = "metabench_perf"\nharness = false\n')
    else:
        path = directory + "/tests/metabench_future.rs"
        checked(workspace, "test ! -e " + path)
        checked(workspace, "mkdir -p " + directory + "/tests")
        checked(workspace, "cat > " + path, source=spec["future_source"])


def build(workspace, spec):
    install(workspace, spec, "benchmark")
    result = workspace.command(f"cargo bench --locked --profile release-with-debug -p {spec['crate']} "
                               "--bench metabench_perf --no-run --message-format=json", 900)
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


def future(workspace, spec):
    if spec.get("future_patch"):
        applied = workspace.command("git apply --whitespace=nowarn -", 60, spec["future_patch"])
        if applied["returncode"]:
            return {"status": "text_conflict_needs_review", "score": None, "patch_execution": applied}
    if spec.get("future_append"):
        from .verification import install_in_workspace
        install_in_workspace(workspace, spec["future_append"])
    if spec.get("future_lib_filters"):
        selection = "--lib -- " + " ".join(spec["future_lib_filters"]) + " --test-threads=1"
    else:
        install(workspace, spec, "future")
        selection = "--test metabench_future -- --test-threads=1"
    result = workspace.command(f"cargo test --locked --profile release-with-debug -p {spec['crate']} " + selection, 900)
    match = re.search(r"test result: (ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored;", result["output"])
    passed, failed, ignored = (map(int, match.groups()[1:]) if match else (0, 0, 0))
    valid = (match is not None and passed + failed == spec["future_test_count"] and ignored == 0
             and not result.get("timed_out") and ((failed == 0 and result["returncode"] == 0)
                                                  or (failed > 0 and result["returncode"] == 101)))
    return {"status": ("passed" if failed == 0 and result["returncode"] == 0 else "behavior_failed") if valid else "build_or_execution_failed",
            "score": 100 * passed / spec["future_test_count"] if valid else None,
            "passed_tests": passed, "failed_tests": failed, "expected_tests": spec["future_test_count"], "execution": result}


def performance_scores(pairs, repeats, tolerance):
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
        ratios = [a / b for a, b in zip(reference, candidate)]
        metrics.append({"name": name, "unit": unit, "reference_samples": reference, "candidate_samples": candidate,
                        "reference_median": statistics.median(reference), "candidate_median": statistics.median(candidate),
                        "paired_reference_over_candidate": ratios, "speedup": statistics.median(ratios),
                        "score": 100 * min(1, (1 + tolerance) * statistics.median(ratios))})
    return metrics


def evaluate(root, request, candidate, functional):
    # Import lazily because vm_worker owns workspace construction and the lock.
    from .vm_worker import CachedWorkspace, execute
    spec = request["task"]["supplemental"]
    reference_request = {**request, "trial_id": digest({"trial": request["trial_id"], "reference": True})[:24]}
    reference = CachedWorkspace(root, reference_request, "verify")
    evidence = {"protocol": "paired-checkpoint-v1", "spec_hash": digest(spec), "pairs": []}
    try:
        checked(reference, "true")
        if reference.restore_source_ownership()["returncode"]:
            raise RuntimeError("reference ownership recovery failed")
        reference.prepare([request["task"]["patch"]])
        evidence["reference_future"] = future(reference, spec)
        if evidence["reference_future"]["status"] != "passed":
            raise RuntimeError("historical reference fails frozen compatibility control")
        # Historical compatibility patches must never contaminate the timed
        # reference build. Restore the task's own revision before measuring it.
        if spec.get("future_patch") or spec.get("future_append"):
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
        evidence["future"] = future(candidate, spec)
        metrics = performance_scores(evidence["pairs"], spec["repeats"], spec["tolerance"])
        performance = statistics.mean(m["score"] for m in metrics) if metrics else None
        future_score = evidence["future"]["score"]
        performance_status = "excluded_incorrect_solution" if not functional["critical_pass"] else ("measured" if metrics else "measurement_failed")
        scores = {"functional": functional["score"], "performance": performance, "future_evolution": future_score}
        total = 0.0 if not functional["critical_pass"] else (
            .6 * scores["functional"] + .2 * performance + .2 * future_score
            if performance is not None and future_score is not None else None)
        return {**functional, "functional_score": functional["score"], "functional_scores": functional["scores"],
                "scores": scores, "score": total, "performance_metrics": metrics or [], "performance_status": performance_status,
                "future_evolution_status": evidence["future"]["status"], "supplemental_evaluation": evidence,
                "supplemental_status": "needs_review" if future_score is None or performance_status == "measurement_failed" else "scored"}
    finally:
        ownership = reference.restore_source_ownership()
        stopped = execute(["docker", "stop", "--time", "2", reference.container], timeout=30)
        if ownership["returncode"] or stopped["returncode"]:
            raise RuntimeError("reference container cleanup failed")
