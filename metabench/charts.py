"""One score dimension per figure, with auditable checkpoint aggregation."""

import collections
import math
import re
import statistics


ERROR_STATUSES = {"agent_error", "infrastructure_error", "evaluation_error", "not_run_after_agent_error"}
DIMENSION_ORDER = ("functional", "correctness", "regression", "compatibility", "performance", "future_evolution")
EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
TITLES = {"functional": "Functional score", "future_evolution": "Future evolution compatibility"}


def plotting_backend():
    # Lazy import keeps curation/evaluation and table-only Python callers lightweight.
    try:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
    except ImportError as error:
        raise RuntimeError("charts require the reports extra: install matplotlib>=3.10,<4 in the project environment") from error
    return Figure, FigureCanvasAgg


def _series_order(key):
    model, effort, config = key
    rank = EFFORT_ORDER.index(effort) if effort in EFFORT_ORDER else len(EFFORT_ORDER)
    return model, rank, effort, config


def _score(value):
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                              or not math.isfinite(value) or not 0 <= value <= 100):
        raise ValueError("dimension scores must be finite numbers from 0 to 100, or null")
    return value


def chart_data(rows):
    """Macro-average tasks, preserving missing rounds and intentional exclusions.

    Applicability comes from score_dimensions (when emitted by the runner) and
    score keys observed for that task. Expected repeats come from that task's
    observed trajectory IDs. An absent checkpoint is never forward-filled.
    """
    suites = {r["suite_id"] for r in rows}
    if len(suites) != 1:
        raise ValueError("compare only runs from the same frozen suite")
    dimensions = set()
    applicable = collections.defaultdict(set)
    groups = collections.defaultdict(dict)
    trajectories = collections.defaultdict(lambda: collections.defaultdict(set))
    configurations = {}
    tasks = set()
    steps = set()
    for row in rows:
        key = row["model"], str(row["reasoning"]), row["config_id"]
        if key[2] in configurations and configurations[key[2]] != key[:2]:
            raise ValueError("a config_id cannot identify different model/reasoning settings")
        configurations[key[2]] = key[:2]
        task = row["task_id"]
        tasks.add(task)
        scores = row.get("scores", {})
        declared_names = row.get("score_dimensions", [])
        if not isinstance(scores, dict) or not isinstance(declared_names, list):
            raise ValueError("scores must be an object and score_dimensions must be a list")
        if any(not isinstance(name, str) or not name.strip() for name in declared_names):
            raise ValueError("dimension names must be nonempty strings")
        declared = set(declared_names) | set(scores)
        if any(not isinstance(name, str) or not name.strip() for name in declared):
            raise ValueError("dimension names must be nonempty strings")
        dimensions.update(declared)
        applicable[task].update(declared)
        for value in row.get("scores", {}).values():
            _score(value)
        step, repeat = row.get("step"), row.get("repeat", 0)
        if isinstance(repeat, bool) or not isinstance(repeat, int) or repeat < 0:
            raise ValueError("repeat must be a nonnegative integer")
        trajectories[key][task].add(repeat)
        if step is None:
            continue
        if isinstance(step, bool) or not isinstance(step, int) or step < 1:
            raise ValueError("step must be a positive integer or null")
        slot = task, repeat, step
        if slot in groups[key]:
            raise ValueError("duplicate checkpoint; do not include a results file twice")
        groups[key][slot] = row
        steps.add(step)
    rounds = list(range(1, max(steps) + 1)) if steps else []
    keys = sorted(trajectories, key=_series_order)
    models = {key[0] for key in keys}
    series = []
    for model, effort, config in keys:
        label = effort if len(models) == 1 else f"{model} / {effort}"
        peers = [k[2] for k in keys if k[:2] == (model, effort)]
        if len(peers) > 1:
            width = 8
            while len({p[:width] for p in peers}) < len(peers):
                width += 1
            label += f" [{config[:width]}]"
        series.append({"model": model, "reasoning": effort, "config_id": config, "label": label})
    ordered = sorted(dimensions, key=lambda name: (DIMENSION_ORDER.index(name) if name in DIMENSION_ORDER else len(DIMENSION_ORDER), name))
    charts = []
    for index, dimension in enumerate(ordered, 1):
        points = {}
        for key in keys:
            points[key[2]] = []
            for step in rounds:
                values, missing, excluded = [], [], []
                active = sorted(task for task in tasks if dimension in applicable[task])
                for task in active:
                    repeats = trajectories[key].get(task, {0})
                    samples, omissions, errors = [], 0, 0
                    for repeat in repeats:
                        row = groups[key].get((task, repeat, step))
                        if row is None or not row.get("score_eligible") or row.get("status") in ERROR_STATUSES:
                            errors += 1
                            continue
                        value = row.get("scores", {}).get(dimension)
                        intentional = row.get("dimension_statuses", {}).get(dimension) == "excluded"
                        intentional |= dimension == "performance" and row.get("performance_status") == "excluded_incorrect_solution"
                        if intentional:
                            if value is not None:
                                raise ValueError("an excluded dimension must have a null or absent score")
                            omissions += 1
                        elif value is None:
                            errors += 1
                        else:
                            samples.append(value)
                    if errors:
                        missing.append(task)
                    elif omissions:
                        # Never improve the mean by keeping only successful repeats of a task.
                        excluded.append(task)
                    else:
                        values.append(statistics.mean(samples))
                value = statistics.mean(values) if values and not missing else None
                points[key[2]].append({"step": step, "score": value, "scored_tasks": len(values),
                                       "applicable_tasks": len(active), "excluded_tasks": excluded,
                                       "missing_tasks": missing, "not_applicable_tasks": sorted(tasks - set(active))})
        slug = re.sub(r"[^a-z0-9_-]+", "-", dimension.lower()).strip("-")[:48] or "score"
        stem = f"{index:02d}-{slug}"
        charts.append({"dimension": dimension, "title": TITLES.get(dimension, dimension.replace("_", " ").title() + " score"),
                       "png": stem + ".png", "svg": stem + ".svg", "points": points})
    return {"generator": "metabench.dimension-charts", "schema_version": 1,
            "suite_id": next(iter(suites)), "steps": rounds, "series": series, "charts": charts,
            "aggregation": "mean repeats within each task, then mean tasks; missing/ineligible checkpoints are gaps; intentionally excluded tasks require every remaining task to be complete"}


def render_charts(data, directory):
    """Render the same series styles on every separate 0–100 score figure."""
    Figure, FigureCanvasAgg = plotting_backend()
    colors = ("#2563eb", "#15956b", "#e47822", "#9b51e0", "#db2777", "#0891b2", "#737373")
    markers = ("o", "s", "^", "D", "v", "P", "X")
    lines = ("-", (0, (7, 4)), (0, (1.5, 2.5)), "-.")
    styles = [dict(color=colors[i % len(colors)], marker=markers[i % len(markers)],
                   markersize=(12, 8, 5.5)[i % 3], markerfacecolor="white" if i % 3 != 2 else colors[i % len(colors)],
                   markeredgewidth=1.8, linewidth=(3, 2.1, 1.7)[i % 3], linestyle=lines[i % len(lines)])
              for i in range(len(data["series"]))]
    for chart in data["charts"]:
        coverage = []
        for series in data["series"]:
            points = chart["points"][series["config_id"]]
            counts = ", ".join(f"{p['scored_tasks']}/{p['applicable_tasks']}" for p in points)
            coverage.append(f"{series['label']}: {counts or 'no rounds'}")
        note = ["Scored/applicable tasks by round (repeat-averaged):", *coverage,
                "Missing/ineligible results are gaps; excluded tasks are not zero-filled.",
                "Changing task coverage can change the mean without changing code performance."]
        if any(p["score"] is not None and p["excluded_tasks"] for points in chart["points"].values() for p in points):
            note.append("This dimension averages only tasks with complete, non-excluded repeats.")
        footer = 0.16 + 0.024 * len(note)
        fig = Figure(figsize=(10, 6.3 + 0.22 * len(data["series"])), facecolor="white")
        FigureCanvasAgg(fig)
        ax = fig.add_subplot()
        fig.subplots_adjust(left=.10, right=.97, top=.77, bottom=min(footer, .52))
        fig.text(.10, .94, chart["title"], fontsize=21, color="#142334")
        models = sorted({s["model"] for s in data["series"]})
        fig.text(.10, .895, " / ".join(models), fontsize=11, color="#657285")

        def draw(target, small=False):
            for series, style in zip(data["series"], styles):
                points = chart["points"][series["config_id"]]
                target.plot([p["step"] for p in points],
                            [p["score"] if p["score"] is not None else math.nan for p in points],
                            label=series["label"], **{**style, "markersize": style["markersize"] * (.65 if small else 1)})

        draw(ax)
        end = max(data["steps"], default=1)
        ticks = data["steps"][::max(1, len(data["steps"]) // 15)]
        ax.set(xlim=(.8, end + .2), ylim=(0, 105), xticks=ticks, yticks=range(0, 101, 20),
               xlabel="Submission round", ylabel="Score")
        ax.grid(axis="y", color="#e4eaf0")
        ax.spines[["top", "right"]].set_visible(False)
        fig.legend(*ax.get_legend_handles_labels(), loc="upper left", bbox_to_anchor=(.09, .868),
                   ncol=min(3, max(1, len(data["series"]))), frameon=False, fontsize=10, handlelength=3)
        values = [p["score"] for points in chart["points"].values() for p in points if p["score"] is not None]
        if values and min(values) > 80 and 0 < max(values) - min(values) < 5:
            zoom = ax.inset_axes([.21, .16, .75, .53])
            zoom.set_facecolor("#f8fafc")
            draw(zoom, small=True)
            padding = max(.15, (max(values) - min(values)) * .2)
            zoom.set(xlim=(.8, end + .2), ylim=(max(0, min(values) - padding), min(101, max(values) + padding)), xticks=ticks)
            zoom.tick_params(labelsize=8)
            zoom.set_title("Detail (expanded score scale)", loc="left", fontsize=9)
            zoom.grid(color="#e4eaf0", linewidth=.6)
        if not values:
            ax.text(.5, .5, "No eligible complete checkpoints", transform=ax.transAxes, ha="center", color="#657285")
        fig.text(.10, .035, "\n".join(note), fontsize=9, color="#657285", va="bottom", linespacing=1.5)
        for extension in ("png", "svg"):
            fig.savefig(directory / chart[extension], dpi=170)
