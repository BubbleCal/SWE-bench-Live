"""Report actual checkpoint scores, never an oracle-selected best checkpoint."""

import collections
import statistics


def report(rows):
    suites = {r["suite_id"] for r in rows}
    if len(suites) != 1:
        raise ValueError("compare only runs from the same frozen suite")
    grouped = collections.defaultdict(list)
    for row in rows:
        grouped[(row["model"], str(row["reasoning"]), row.get("step"), row["config_id"])].append(row)
    lines = ["# Meta-bench results", "", "Suite: `" + next(iter(suites)) + "`", "",
             "Scores are current checkpoints, not best-of-trajectory selections. Errors remain in the coverage denominator.", "",
             "| Model | Reasoning | Step | Mean score | Scored / attempted | Critical pass | Eligible |",
             "| --- | --- | ---: | ---: | ---: | ---: | --- |"]
    for (model, reasoning, step, _), group in sorted(grouped.items(), key=lambda x: str(x[0])):
        scored = [r for r in group if r.get("score") is not None]
        # Macro-average tasks after averaging independent repeats within each task.
        by_task = collections.defaultdict(list)
        for row in scored:
            by_task[row["task_id"]].append(row["score"])
        mean = statistics.mean(statistics.mean(v) for v in by_task.values()) if scored else None
        display = f"{mean:.2f}" if mean is not None and len(scored) == len(group) else "incomplete"
        critical = sum(bool(r.get("critical_pass")) for r in scored)
        eligible = "yes" if len(scored) == len(group) and all(r.get("score_eligible") and r["status"] != "agent_error" for r in scored) and scored else "no"
        escape = lambda s: str(s).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {escape(model)} | {escape(reasoning)} | {step if step is not None else 'N/A'} | {display} | {len(scored)} / {len(group)} | {critical} / {len(group)} | {eligible} |")
    lines += ["", "Trusted-local results validate the harness only; they are not isolated model benchmark results.",
              "No ranking or confidence claim is made for this pilot. Missing usage metadata remains null."]
    return "\n".join(lines) + "\n"
