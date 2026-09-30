"""Versioned performance scoring and offline rescoring of measured receipts."""

import copy
import hashlib
import json
import math
import os
import statistics
import tempfile
from pathlib import Path

from .scoring import EVALUATION_POLICY_ID, without_evolution

PERFORMANCE_POLICY_ID = "relative-latency-v2"
LEGACY_POLICY_ID = "paired-latency-v1-tolerance-cap"


def metric_score(speedup):
    """Symmetric, monotonic score: parity=50, 2x faster=66.67, 2x slower=33.33.

    speedup is reference latency / candidate latency. No tolerance bonus or
    finite-speedup ceiling is applied; 0 and 100 are limiting values.
    """
    if isinstance(speedup, bool) or not isinstance(speedup, (int, float)) or not math.isfinite(speedup) or speedup <= 0:
        raise ValueError("speedup must be positive and finite")
    return 100 / (1 + 1 / speedup) if speedup >= 1 else 100 * speedup / (1 + speedup)


def rescore_rows(rows):
    """Recompute scores from paired timings, preserving original decisions/usage.

    Historical trials and their generation protocol are not changed. Preserve
    their previous scores as provenance and fail rather than invent timings.
    Only paired-checkpoint functional/performance receipts are accepted; legacy evolution stays in provenance.
    """
    from .checkpoint_eval import performance_scores
    rescored = []
    for original in rows:
        row = copy.deepcopy(original)
        if row.get("performance_policy_id") == PERFORMANCE_POLICY_ID:
            rescored.append(row)
            continue
        status = row.get("performance_status")
        if status not in ("measured", "excluded_incorrect_solution", "measurement_failed"):
            raise ValueError("rescore-performance requires paired-checkpoint performance receipts")
        if not isinstance(row.get("critical_pass"), bool):
            raise ValueError("missing critical functional decision")
        scores = row.get("scores", {})
        if not {"functional", "performance"}.issubset(scores):
            raise ValueError("missing checkpoint score dimensions")
        row["performance_rescoring"] = {
            "source_policy_id": row.get("performance_policy_id", LEGACY_POLICY_ID),
            "source_scores": copy.deepcopy(scores), "source_total_score": row.get("score"),
            "source_metric_scores": [{"name": m["name"], "score": m["score"]}
                                     for m in row.get("performance_metrics", [])],
        }
        # Capture the original evaluation policy and totals before changing metrics.
        row = without_evolution([row])[0]
        performance = None
        if status == "measured":
            if not row["critical_pass"]:
                raise ValueError("measured performance requires critical functional success")
            metrics = row.get("performance_metrics", [])
            if not metrics:
                raise ValueError("missing paired timing samples")
            repeats = len(metrics[0].get("reference_samples", []))
            if repeats < 1 or len({m["name"] for m in metrics}) != len(metrics):
                raise ValueError("invalid paired timing samples")
            if any(len(m.get(side + "_samples", [])) != repeats
                   for m in metrics for side in ("reference", "candidate")):
                raise ValueError("misaligned paired timing samples")
            pairs = [{side: {"valid": True, "metrics": [
                {"name": m["name"], "unit": m["unit"], "value": m[side + "_samples"][i]}
                for m in metrics]} for side in ("reference", "candidate")} for i in range(repeats)]
            metrics = performance_scores(pairs, repeats, None)
            if metrics is None:
                raise ValueError("invalid paired timing samples")
            row["performance_metrics"] = metrics
            performance = statistics.mean(m["score"] for m in metrics)
        elif status == "excluded_incorrect_solution" and row["critical_pass"]:
            raise ValueError("performance exclusion conflicts with functional success")
        row["performance_policy_id"] = PERFORMANCE_POLICY_ID
        row["scores"]["performance"] = performance
        rescored.append(row)
    return without_evolution(rescored)


def rescore_file(source, out):
    """Write a separate atomic result file; never overwrite the source receipts."""
    source, out = Path(source), Path(out)
    if source.resolve() == out.resolve():
        raise ValueError("write rescored results to a different file; preserve original receipts")
    raw = source.read_bytes()
    rows = rescore_rows([json.loads(line) for line in raw.splitlines() if line.strip()])
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=out.parent, prefix=".rescore-", delete=False) as file:
        temporary = Path(file.name)
        for row in rows:
            file.write(json.dumps(row, allow_nan=False) + "\n")
    try:
        os.replace(temporary, out)
    finally:
        temporary.unlink(missing_ok=True)
    from .schema import write_json
    write_json(out.with_suffix(out.suffix + ".policy.json"), {
        "performance_policy_id": PERFORMANCE_POLICY_ID,
        "evaluation_policy_id": EVALUATION_POLICY_ID,
        "formula": "100 * median(reference_latency / candidate_latency) / (1 + median(reference_latency / candidate_latency))",
        "source_sha256": hashlib.sha256(raw).hexdigest(), "rows": len(rows),
        "measurements_reused": True, "model_calls": 0,
    })
    return rows
