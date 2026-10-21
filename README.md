# Meta-bench

Generate and freeze benchmark tasks from a repository's history, then compare
native coding agents on functionality and performance.
Each model / effort / issue has a private worktree and a persistent CLI session.
Every round is scored through a VM queue while the next round begins.
GPT benchmarks resolve the model's maximum context from the authenticated native
CLI catalog and explicitly use Standard service with Fast mode off. Settings are
recorded with the run and retained when resuming it.

Meta-bench is an independent Python project. [SWE-bench-Live](https://github.com/microsoft/SWE-bench-Live)
is an unmodified, commit-pinned **source dependency**, isolated behind
[`metabench.upstream`](metabench/upstream.py). It is not copied into our package
or patched in place. The native benchmark and local dashboard work without it.

## Install

Python 3.12+, Git and SSH are required on the controller. Test workers use Linux
and Docker; model execution uses your authenticated Codex or Claude Code CLI.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/metabench --help
```

The core uses the standard library. Optional dependencies:

```sh
.venv/bin/python -m pip install -e '.[reports]'   # PNG/SVG charts
.venv/bin/python -m pip install -e '.[curation]'  # LiteLLM curation adapter
```

See [MetaBench.md](MetaBench.md) for task preparation, native model runs,
VM concurrency, token accounting and result visualization.

## SWE-bench-Live integration

```sh
git submodule update --init --recursive
.venv/bin/python -m pip install -e '.[live]'
.venv/bin/metabench upstream doctor
.venv/bin/metabench upstream evaluate -- --help
```

Only the optional upstream commands need the `live` extra. The nested RepoLaunch
runtime is pinned independently. The source lock is
[`metabench/upstream-lock.json`](metabench/upstream-lock.json); Git submodules
record the same revisions. We retain the revision used by the existing benchmark.
Upstream does not expose a stable standalone library: its distribution is named
`swebench`, and its scripts assume separate `evaluation` and `launch` namespaces.
The adapter executes those entry points in a child process without importing them
into the meta-bench controller or changing the dependency source.

Run commands from **outside** the dependency directory. Relative input/output
paths refer to your current directory, not the dependency checkout. For example:

```sh
.venv/bin/metabench upstream evaluate -- \
  --dataset /absolute/path/tasks.jsonl --platform linux \
  --patch_dir /absolute/path/predictions.json --output_dir .metabench/live-evaluation \
  --workers 1 --overwrite 0
.venv/bin/metabench grade-live \
  --task instance.json --status .metabench/live-evaluation/status.json \
  --out .metabench/strict-score.json
```

`evaluate`, `validate`, `crawl` and `filter` forward upstream arguments and exit
codes. Run `metabench upstream --help` for the adapter options and use upstream's
[development guide](dependencies/swe-bench-live/Development.md) and
[evaluation guide](dependencies/swe-bench-live/evaluation/README.md) for its contracts.
Both source revisions and clean checkouts are checked before invoking upstream.
For Fire's own help flags use `metabench upstream filter -- -- --help`.
Use Python 3.12 for the optional `live` environment, matching RepoLaunch's pinned dependencies.

A wheel contains only `metabench`, not the source dependency. Wheel users must
supply a matching initialized checkout with `metabench upstream --source /path/to/SWE-bench-Live ...`.
To upgrade, update the root submodule Git link and source lock together, match the
nested RepoLaunch revision in the `live` extra, and rerun integration checks.
All custom behavior belongs in our adapter or our modules.

## Local results

Native runs support [separate public and grading worker pools](examples/metabench/native-split-pools.json).
The evaluator retains independent functional/performance workspaces, verifies cached
benchmark binaries before reuse, and skips performance work for failing candidates.
Paired timing samples remain fresh. See [worker configuration and cache behavior](MetaBench.md#native-agents-and-vm-scheduling).

Serve any saved JSONL report without an upstream checkout:

```sh
.venv/bin/metabench dashboard results.jsonl --port 8772
```

Performance uses a symmetric relative score: reference parity=50, 2x faster=66.67,
2x slower=33.33. Use `metabench rescore-performance results.jsonl --out rescored.jsonl`
to apply it to archived paired timings, preserving original receipts.

The active Lance task set is `benchmarks/lance/current.json`: **004, 017, 018,
024, 025, 026 and 029**. The six-configuration, four-round results show functional,
material performance, or first-round completion differences on these seven tasks.
Task 025 is retained for first-round compilation/completion differences even though
all configurations pass from round two. Tasks 022 and 028 have negligible observed
separation, and 027 has weak separation; they join the previous exclusions.

The canonical seven-task suite, matching environments and selection evidence are
under `.metabench/lance-selected7-20261008/`. Task definitions and their existing
base/reference validation receipts are unchanged. The current dashboard filters
the completed ten-task comparison to **168 checkpoints** (7 tasks × 6 configurations
× 4 rounds) and recomputes aggregates over those tasks. Original comparison suite
and configuration IDs remain attached to the score rows; this selection does not
claim a new model run under the seven-task suite ID.
The current dashboard shows functional and performance only; the total is 75% functional
and 25% performance, gated to zero by a critical functional failure.

The previous 480 checkpoint receipts and original three-dimension protocol remain
archived under `.metabench/gpt61-sol-20260930/results/` and are not rewritten.
The complete 240-point ten-task snapshot from before this selection is preserved in
`.metabench/lance-selected7-20261008/previous-dashboard/`. Earlier snapshots under
`.metabench/lance-next5-20261001/previous-dashboard/`, the audited new9 receipts under
`.metabench/lance-new9-20261001/`, and the Sol/Astra/Opus native-run receipts remain intact.
Missing model results must remain unscored, never inferred from reference controls.

## Development

See [Development.md](Development.md). Source and retained upstream notices are
licensed under [MIT](LICENSE).
