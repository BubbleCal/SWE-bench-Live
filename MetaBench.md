# Repository meta-benchmark

This fork adds a repository-specific evaluation workflow to SWE-bench-Live. It preserves
the upstream curation, RepoLaunch environment construction, and evaluation commands.
The native runner, VM queue, evaluator and interactive HTML report use the Python
standard library. PNG/SVG exports use the optional `reports` extra (Matplotlib).
The one-shot curation adapter can use RepoLaunch's existing LiteLLM dependency.

## Scope of this version

- Mine a local Git revision range through a JSON curation-agent adapter.
- Validate each task against an unchanged base and a reference solution, repeatedly.
- Freeze task statements, patches, checks, dimension weights, and environment identity.
- Run native Codex or Claude Code CLI sessions. A trial is one model / effort / issue / repeat.
- Give every trial a private Git worktree and session; run trials concurrently.
- Queue public tests and hidden verification on a configurable pool of existing VMs,
  with one execution lock per physical machine and persistent per-trial build caches.
- Evaluate immutable submission patches separately, after the complete trajectory.
- Record `(model, reasoning, step, score)` with configuration, per-dimension evidence,
  cumulative model calls, reported token usage, cost, elapsed time, and patch identity.
- Score correctness, regressions, explicit compatibility checks, and measured performance.
- Automatically produce one score-vs-round figure per dimension, comparing model/reasoning configurations.
- Strictly regrade SWE-bench-Live status maps, counting missing/skipped expected tests as failures.

The Lance pilot uses [PR #8873](https://github.com/lance-format/lance/pull/8873).
Its tests are embedded in the same Rust file as the solution. The example separates them
at the inline test-module boundary and removes an implementation-specific panic-message
assertion. It is a reviewed historical pilot, not a representative Lance benchmark suite.

Future-requirement replay, subjective code review scores, automatic task-family sampling,
confidence intervals, model rankings, and token/cost enforcement are not implemented yet.
No placeholder score is assigned to an unsupported dimension. The native runner limits user-level conversation rounds, with an optional round
timeout. CLI tool iteration and context management remain native. Queue wait and VM
execution times are recorded separately; token counters remain observations.

## Setup

Use Python 3.12+, Git and SSH on the controller (macOS/Linux). Test VMs need Linux,
Python 3.12+, Git, Docker and the pinned images. From the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install 'matplotlib>=3.10,<4'
.venv/bin/python -m metabench --help
.venv/bin/python -m unittest discover -s tests -v
```

The direct Matplotlib installation above is the lightweight reporting dependency declared
in `pyproject.toml`'s `reports` extra. The existing upstream
`.venv/bin/python -m pip install -e '.[reports]'` workflow also installs the `metabench`
entry point and upstream dependencies. Curation and validation can run without Matplotlib;
native `run` writes standalone HTML without needing Matplotlib. Install and authenticate
the local Codex / Claude Code CLIs for live trials. The `launch` submodule and its
provider dependencies are only needed by the optional curation adapter.

## Prepare a task

`mine` delegates interpretation of history and test extraction to an adapter:

```sh
.venv/bin/python -m metabench mine \
  --repo /path/to/repo --revision BASE..HEAD \
  --adapter curation-adapter.json --out .metabench/candidates
```

The adapter file contains an argv array, for example
`["/absolute/path/to/.venv/bin/python", "/absolute/path/to/curator.py"]`.
The trusted curator receives JSON on stdin containing the historical message, full diff,
base and reference commits, and repository path. It emits `problem_statement`, `patch`,
`test_patch`, `checks`, and `weights`. Its stderr is for diagnostics. The curator may see
the answer; the tested model never receives that payload.

`mine` emits candidates, not validated tasks. It currently handles non-merge commits in
a local revision range; PR/issue discovery and environment preparation remain available
through upstream `curation/` and RepoLaunch. Review requirements and test coverage before
accepting generated tasks. Candidate difficulty is not inferred from patch size.
The example `examples/metabench/repolaunch_curator.py --model PROVIDER/MODEL --reasoning SETTING`
is a one-shot history-to-task adapter using the same existing provider dependency. A
repository-exploring agent can implement the same JSON contract.

For the reviewed Lance pilot:

```sh
.venv/bin/python examples/metabench/lance_task.py \
  --repo /path/to/lance --out .metabench/lance-task.json
```

The clone must contain commit `f7b4c594284537d915c93bf5d7708e3bb61be600` and its parent.
Both the reference and test patches are private evaluator inputs. Do not put task bundles
inside an agent-accessible workspace or publish them as supposedly uncontaminated tasks.

## Task and environment contracts

Task field names `instance_id`, `repo`, `base_commit`, `problem_statement`, `patch`, and
`test_patch` follow SWE-bench-Live. New tasks also specify `checks` and `weights`:

```json
{
  "checks": [{
    "id": "regression-case",
    "dimension": "correctness",
    "critical": true,
    "command": "python -m unittest tests.test_regression -v",
    "success_pattern": "(?s)Ran 1 test.*OK",
    "failure_pattern": "FAILED \\(failures=1\\)",
    "timeout": 120
  }],
  "weights": {"correctness": 1}
}
```

A command's zero exit status alone does not prove tests ran: a success pattern is required.
Write patterns that establish the expected test identities/counts. A failure pattern
must distinguish the intended failing test from a compiler/import error or missing tool.
Validation will not accept an arbitrary failing command as a reproduced bug.

If a model request reaches the round deadline, the runner snapshots the current candidate
and proceeds to the next allowed round. The timed-out request's unreported usage stays
unknown; later rounds still retain their own incremental usage. A deadline does not
silently terminate the remaining submission rounds.

The environment file identifies an already prepared **toolchain-only** Docker image by
digest or local image ID, plus resource limits and optional build commands:

```json
{
  "image": "sha256:REPLACE_WITH_ACTUAL_IMAGE_ID",
  "cpus": 4,
  "memory": "8g",
  "setup": ["your offline build command"],
  "agent_setup": [],
  "setup_timeout": 1800,
  "env": {}
}
```

Pin compilers and install/fetch dependencies when preparing the image. It must contain
`/bin/sh`, Git, `timeout` from coreutils, and all offline build dependencies. `/repo` must
be empty; the runner copies only the selected Git archive into that directory. No host
directories, credentials, or Docker socket are mounted into the task container. Only
explicit `env` entries are forwarded; they must not contain secrets.

Upstream RepoLaunch can discover build/test recipes and prepare environments. Audit its
output before using it as a toolchain image: copying a checkout and then removing its
`.git` directory is insufficient if answers remain elsewhere in the image. This runner
does not certify arbitrary third-party images as free of historical code or secrets.

## Validate, freeze, and run

```sh
.venv/bin/python -m metabench validate \
  --repo /path/to/lance --task .metabench/lance-task.json \
  --environment environment.json --out .metabench/validation --repeats 3

.venv/bin/python -m metabench freeze \
  .metabench/validation/task.json --out .metabench/suite.json

.venv/bin/python -m metabench run \
  --suite .metabench/suite.json --repo /path/to/lance \
  --environment environment.json --matrix my-native-matrix.json \
  --vm-count 2 --parallel-agents 30 --out .metabench/run-001

.venv/bin/python -m metabench report \
  .metabench/run-001/results.jsonl --out .metabench/report.html

# Re-read the configured result files every 5 seconds in a local browser.
.venv/bin/python -m metabench dashboard \
  .metabench/run-001/results.jsonl .metabench/run-002/results.jsonl --port 8765
```

`report --out report.html` creates a standalone interactive report without external
libraries or network dependencies. It has one chart per dimension, model/effort
checkboxes, task selection, checkpoint details, coverage and cumulative token usage.
`dashboard` serves the same view at `http://127.0.0.1:8765/`, reloading source files
on refresh; it retains the last valid browser snapshot if a file is partially written
or unavailable. Selections persist locally. Downloading a snapshot produces an
offline HTML file; it does not continue polling the local server.

Native `run` updates `results.jsonl`, `status.json` and `report.html` as checkpoints
and grades arrive. Explicit Markdown reports retain PNG/SVG exports and link to the
interactive HTML. To compare configurations, pass their JSONL files together;
configuration hashes and frozen suite checks still apply. The live viewer binds
only to loopback and exposes no file browser. Optional `--metadata status.json`
accepts `title`, `subtitle`, `phase`, `note`, `updated_at` and a `progress` object
with `planned`, `generated`, `functional`, `performance`, and `evolution` counters.
Result producers must publish their updates; the viewer does not run or grade models.

Validation requires repeatable target failures on the base, passing reference checks,
and usable environments. Changed task content or environment configuration requires new
validation. A suite has a deterministic content hash; modifying its contents invalidates it.
Private validation artifacts are operator-owned, not cryptographic attestations against
a malicious benchmark publisher.

`--trusted-local` exists for controlled local debugging and reference/no-op validation.
It executes repository commands on the host and **is not a security sandbox**. Such results
are always marked `score_eligible: false`; use this only with trusted code, never as an
isolated adversarial model benchmark. Docker is required for ordinary model runs.

## Native agents and VM scheduling

Copy [native-matrix.json](examples/metabench/native-matrix.json), then edit the VM
inventory and agent configurations. `--vm-count N` selects the first N configured VMs;
it does not provision cloud instances. Every selected VM needs SSH, Python 3.12+,
Git, Docker, and the pinned environment images. Missing images fail preflight before
model calls. CLI versions, invoked-file hashes and execution-source hashes are recorded.

The six example model/effort configurations over five issues create **30 independent
trials**, not six sequential issue runners. Concurrency defaults to every trial; use
`--parallel-agents` to impose a provider/resource limit. Completed agent trajectories
release their agent slots immediately while grading continues in the separate VM queue.
Each trial has stable VM affinity so its compiler cache stays warm. Different VMs can
execute tests concurrently; tests assigned to the same VM are FIFO and exclusive.

Local layout:

```text
run/
  run.json, results.jsonl, status.json, report.html
  queue.sqlite
  trials/<model>-<effort>-<issue>-<id>/
    checkout/objects.git           # private, depth-one base objects
    checkout/worktree/             # exact requested base commit
    turn-1/events.jsonl, result.json, invocation.json
    checkpoint-1.json, step-1.patch, score-1.json
```

The controller calls `codex exec` or `claude -p` once per conversation round. The CLI
reads/edits code and performs its own tool calls; no command-action JSON loop runs in
metabench. Later rounds resume the exact saved session ID, never `--last` or `--continue`.
A fixed self-review prompt starts each additional round without revealing hidden scores.
See the official [Codex command reference](https://learn.chatgpt.com/docs/developer-commands#codex-exec)
and [Claude Code programmatic execution](https://code.claude.com/docs/en/headless).

The configured `bench.run_tests` MCP tool snapshots current changes and queues a public
test on the VM. Each trial has a capability limited to its own public test results.
Hidden verification starts after that trial's complete trajectory; its results have no
agent-facing endpoint. Public and verification lanes have separate Git databases and
container filesystems, preventing hidden test objects from entering a reused public cache.

VM layout and locking:

```text
/tmp/metabench-vm.lock              # global across run roots/controllers
<vm-root>/trials/<trial-id>/
  public.seed/, public/            # public-test worktree
  verify.seed/, verify/            # held-out verifier worktree
<vm-root>/queue/<job-id>/           # request, worker log, durable result
```

VM workers detach from SSH before taking the kernel lock. That lock covers source
source synchronization, setup, compilation, tests and process cleanup. Disconnecting a local
controller does not release the remote lock. A new worker fences surviving owned
containers before running tests. Containers are stopped between jobs but retained;
compiler output such as `/build-cache/target` survives while source is updated to the
base plus the immutable candidate patch. An alternate Git index applies only content
changes, preserving timestamps on unchanged sources and avoiding needless rebuilds. There is no shared mutable cache between trials
or between public and private lanes. VM filesystem ownership is restored before releasing
the lock. The container has no network, no Docker socket, and only its own source and
read-only Git metadata bind mounts.

`--resume` accepts the exact same frozen configuration, reconciles existing remote jobs,
and skips completed checkpoints. Running jobs never expire based only on elapsed time:
a missing remote receipt requires inspection rather than potentially overlapping a live
test. An orphaned CLI turn directory is also retained for inspection rather than silently
billing a duplicate turn. Keep VM directories and stopped containers to reuse caches;
cloud stop/start preserves them when backed by persistent disks.

Codex pre-approves only `bench.run_tests` through its per-tool MCP setting. Claude uses
restricted mode with explicit native coding tools and the same scoped MCP tool; safe mode
would disable even the explicitly configured server. Both retain their own agent loops.

Native CLIs run on the controller host with saved local authentication and their native
permission mechanisms. Git worktrees isolate repository state, **not hostile processes
on the same host**. Use trusted repositories/agents; VM test isolation does not turn a
local native CLI into an adversarial sandbox. Pin an executable path when CLI auto-updates
would otherwise change a run. Subagents are disabled so the selected model handles the
trial; ordinary native file and shell tools remain available.

Token accounting distinguishes provider scopes: Codex's resumed session counters are
differenced against the previous checkpoint; Claude's per-invocation `result.usage` is
used instead of its cumulative `modelUsage`/cost fields or incomplete streaming counters.
Cache reads/writes are included once in input. Missing interrupted-request usage remains
unknown. CLI price estimates are not subscription invoices.

The retired protocol is retained under `metabench.legacy` and `legacy-run` only for
reproducing historical results. New runs emit `protocol_id: native-cli-v1`; reports reject
mixing them with legacy command-loop measurements.

## Scoring and reporting

Checks are weighted within each dimension; configured dimension weights combine them.
A failed critical check zeros the overall score while retaining the ungated progress score
and individual dimensions. Compatibility is tested through explicit executable checks.
No patch-similarity or line-count score is used.

Performance checks emit one positive scalar per command execution. Declare `direction`
(`lower` or `higher`), fixed `good`/`bad` thresholds, and `repeats`. The median is mapped
linearly to [0,100], clipped to that range. Raw samples are retained. Define units,
warmup/cache state, workload, concurrency, and a correctness condition in each task's
provenance. This is a primitive for measured evaluation, not an automatic speedup claim.

Reference code is not assumed to be the only correct solution. Tests should accept
equivalent implementations. Historical public tasks may have appeared in pretraining;
the tool makes no contamination-free claim.

Report means first average repeats within each task, then average tasks. Established
check failures score zero. Unclassified build, infrastructure and evaluator failures remain visible and make
the corresponding aggregate incomplete. Runs from different suites cannot be combined.
The report does not select the highest-scoring historical checkpoint, pool different
configuration hashes, or assert statistically significant model rankings.

### Interactive reports: one dimension per chart

Native `run` generates interactive HTML. Each recorded score dimension has its own chart.
`report --out comparison.html` produces a standalone snapshot; explicit Markdown output
also exports separate PNG/SVG figures.
The x-axis is the submission round; the y-axis is score (0–100). Each figure contains
all model/reasoning configurations, with consistent colors, line styles and markers.
HTML supports model/effort filters, individual tasks, round details and token totals.
The live `dashboard` command refreshes producer updates while retaining selections.
Static image exports include a zoom inset for close scores; points are never jittered.

For `--out comparison.md`, the report links to:

- `comparison.charts/01-<dimension>.png` and `.svg`, one pair per dimension;
- `comparison.charts/chart-data.json`, containing plotted values, task coverage,
  configuration IDs and aggregation rules.

Dimensions are discovered from `scores` and `score_dimensions` in the result rows;
they are not hardcoded to the pilot's three dimensions. `score_dimensions` records
task applicability even when generation or evaluation fails. Imported evaluators may
provide additional names such as `functional` or `future_evolution` in `scores`.
The report uses those scores as provided and does not invent or recalculate dimensions.

Missing rounds, unreported scores, agent/evaluator errors and ineligible results are
**gaps**, not zeroes or carried-forward values. Missing checkpoints for known task/repeat
trajectories make the affected aggregate incomplete. A dimension never declared or
observed for a task is not applicable to that task. Duplicate checkpoints and mixed
suites are rejected. Separate configuration hashes remain separate curves, including
when model and reasoning labels match.

An evaluator can deliberately omit a dimension by writing a null score and
`dimension_statuses: {"performance": "excluded"}`. The existing pilot's
`performance_status: "excluded_incorrect_solution"` is also recognized. A task with
an excluded repeat is omitted as a whole, so its successful repeats cannot selectively
improve the mean. Other missing results still leave a gap. Figures list scored/applicable
task counts for every round; changing task coverage can change an average without
changing a solution's performance. Exclusions never alter the recorded total score.

The low-level Python `report(rows)` function still returns tables as text. Use
`write_report(rows, path)` for the complete default report with figures, or the CLI.
Regeneration retires only images listed in the previous generated chart manifest;
other files in the chart directory are preserved.

## Reuse upstream evaluation results

Run upstream `evaluation.evaluation` as documented in [evaluation/README.md](evaluation/README.md).
Then use the exact instance and its `status.json` for strict grading:

```sh
.venv/bin/python -m metabench grade-live \
  --task instance.json --status path/to/status.json --out .metabench/strict-score.json
```

Both `FAIL_TO_PASS` and `PASS_TO_PASS` must all be observed passing. A skipped or absent
expected test cannot be accepted merely because it was absent from the failure list.

## Docker integration tests

```sh
docker build -t metabench-python -f examples/metabench/python.Dockerfile .
METABENCH_TEST_IMAGE=$(docker image inspect metabench-python --format '{{.Id}}') \
  .venv/bin/python -m unittest discover -s tests -p '*docker.py' -v
```

The test verifies a clean Git snapshot, disabled networking, subprocess timeout cleanup,
and separate base/reference grading. The example Dockerfile is for harness tests, not
the Lance Rust toolchain; freeze the resulting image ID before using it in a task.
