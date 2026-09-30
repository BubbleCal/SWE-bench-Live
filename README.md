# Meta-bench

Generate and freeze benchmark tasks from a repository's history, then compare
native coding agents on functionality, performance and future compatibility.
Each model / effort / issue has a private worktree and a persistent CLI session.
Every round is scored through a VM queue while the next round begins.

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

Serve any saved JSONL report without an upstream checkout:

```sh
.venv/bin/metabench dashboard results.jsonl --port 8772
```

Performance uses a symmetric relative score: reference parity=50, 2x faster=66.67,
2x slower=33.33. Use `metabench rescore-performance results.jsonl --out rescored.jsonl`
to apply it to archived paired timings, preserving original receipts.

The existing combined Lance report remains in `.metabench/gpt61-sol-20260930/results/`.
Its 480 scored checkpoints and frozen protocols are retained. The current local
view uses `combined-results.jsonl` and `metadata.json` in that directory.

## Development

See [Development.md](Development.md). Source and retained upstream notices are
licensed under [MIT](LICENSE).
