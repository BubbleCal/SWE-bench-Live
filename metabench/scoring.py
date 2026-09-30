"""Current evaluation dimensions; historical three-dimension receipts stay archived."""
import copy

EVALUATION_POLICY_ID = "functional-performance-v1"


def combined_score(functional, performance, critical_pass):
    # Preserve the previous functional:performance weight ratio (60:20).
    if not critical_pass:
        return 0.0
    return .75 * functional + .25 * performance if performance is not None else None


def without_evolution(rows):
    """Project historical receipts into the current two-dimension report policy."""
    result = []
    for original in rows:
        row = copy.deepcopy(original)
        scores = row.get("scores", {})
        if "functional" not in scores or "performance" not in scores:
            result.append(row)
            continue
        if row.get("evaluation_policy_id") != EVALUATION_POLICY_ID:
            row["evaluation_rescoring"] = {
                "source_policy_id": row.get("evaluation_policy_id", "functional-performance-evolution-v1"),
                "source_scores": copy.deepcopy(scores), "source_total_score": row.get("score")}
        scores.pop("future_evolution", None)
        row.pop("future_evolution_status", None)
        for key in ("score_dimensions",):
            if key in row:
                row[key] = [d for d in row[key] if d != "future_evolution"]
        row["evaluation_policy_id"] = EVALUATION_POLICY_ID
        if "critical_pass" in row:
            row["score"] = combined_score(scores["functional"], scores["performance"], row["critical_pass"])
        result.append(row)
    return result
