import argparse
import json
import sys
from pathlib import Path

from .schema import read_json, write_json, freeze, load_suite


def main():
    parser = argparse.ArgumentParser(description="Build and run repository-specific SWE-bench-Live evaluations")
    sub = parser.add_subparsers(dest="operation", required=True)
    mine = sub.add_parser("mine", help="use a curation agent to turn local Git history into candidate tasks")
    mine.add_argument("--repo", required=True)
    mine.add_argument("--revision", required=True, help="Git revision range, e.g. base..head")
    mine.add_argument("--adapter", required=True, help="JSON file containing an argv array")
    mine.add_argument("--limit", type=int, default=20)
    mine.add_argument("--out", type=Path, required=True)
    validate = sub.add_parser("validate", help="reproduce the base failure and reference success")
    validate.add_argument("--task", type=Path, required=True)
    validate.add_argument("--repo", required=True)
    validate.add_argument("--environment", type=Path, required=True)
    validate.add_argument("--out", type=Path, required=True)
    validate.add_argument("--repeats", type=int, default=3)
    validate.add_argument("--trusted-local", action="store_true", help="execute trusted code on this host; results are not benchmark-eligible")
    frozen = sub.add_parser("freeze", help="freeze validated tasks with a content hash")
    frozen.add_argument("tasks", nargs="+", type=Path)
    frozen.add_argument("--out", type=Path, required=True)
    run = sub.add_parser("legacy-run", help="reproduce the retired command-loop protocol")
    for flag in ("suite", "environment", "adapter", "budget", "out"):
        run.add_argument("--" + flag, type=Path, required=True)
    run.add_argument("--repo", required=True)
    run.add_argument("--model", required=True)
    run.add_argument("--reasoning", required=True)
    run.add_argument("--repeats", type=int, default=1)
    run.add_argument("--trusted-local", action="store_true")
    native = sub.add_parser("run", help="run parallel native CLI model/effort/issue trials")
    for flag in ("suite", "environment", "matrix", "out"):
        native.add_argument("--" + flag, type=Path, required=True)
    native.add_argument("--repo", required=True)
    native.add_argument("--vm-count", type=int)
    native.add_argument("--parallel-agents", type=int)
    native.add_argument("--resume", action="store_true")
    report = sub.add_parser("report", help="render interactive HTML, or Markdown with image exports")
    report.add_argument("results", nargs="+", type=Path)
    report.add_argument("--out", type=Path, required=True)
    dashboard = sub.add_parser("dashboard", help="serve live, filterable results on localhost")
    dashboard.add_argument("results", nargs="+", type=Path)
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument("--metadata", type=Path, help="optional JSON with title, phase, note and progress")
    live = sub.add_parser("grade-live", help="strictly grade a SWE-bench-Live status map")
    live.add_argument("--task", type=Path, required=True)
    live.add_argument("--status", type=Path, required=True)
    live.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.operation == "mine":
            from .mine import mine
            tasks = mine(args.repo, args.revision, read_json(args.adapter), limit=args.limit)
            args.out.mkdir(parents=True, exist_ok=False)
            for task in tasks:
                write_json(args.out / (task["instance_id"] + ".json"), task)
            print(f"Created {len(tasks)} candidate tasks; validate before freezing.")
        elif args.operation == "validate":
            from .evaluate import validate
            if args.out.exists():
                raise ValueError("validation output already exists")
            result = validate(args.repo, read_json(args.task), read_json(args.environment), args.out,
                              trusted_local=args.trusted_local, repeats=args.repeats)
            print(json.dumps(result["validation"], indent=2))
            return 0 if result["validation"]["passed"] else 1
        elif args.operation == "freeze":
            write_json(args.out, freeze([read_json(path) for path in args.tasks]))
        elif args.operation == "legacy-run":
            from .legacy.run import run
            rows = run(load_suite(args.suite), args.repo, read_json(args.environment), read_json(args.adapter),
                       args.model, args.reasoning, read_json(args.budget), args.out,
                       repeats=args.repeats, trusted_local=args.trusted_local)
            return 1 if any(r.get("score") is None or r["status"] == "agent_error" for r in rows) else 0
        elif args.operation == "run":
            from .native_run import run_matrix
            rows = run_matrix(load_suite(args.suite), args.repo, read_json(args.environment), read_json(args.matrix),
                              args.out, vm_count=args.vm_count, parallel=args.parallel_agents, resume=args.resume)
            return 0 if read_json(args.out / "run.json").get("status") == "Complete" else 1
        elif args.operation == "report":
            from .report import write_report
            rows = [json.loads(line) for path in args.results for line in path.read_text().splitlines() if line.strip()]
            write_report(rows, args.out)
        elif args.operation == "dashboard":
            from .dashboard import serve_dashboard
            serve_dashboard(args.results, port=args.port, metadata_path=args.metadata)
        elif args.operation == "grade-live":
            from .evaluate import grade_live_status
            write_json(args.out, grade_live_status(read_json(args.task), read_json(args.status)))
    except (ValueError, KeyError, OSError, RuntimeError) as error:
        parser.exit(2, f"metabench: {error}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
