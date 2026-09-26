# Repository meta-benchmark

This fork adds a repository-specific evaluation workflow to SWE-bench-Live. It preserves
the upstream curation, RepoLaunch environment construction, and evaluation commands.
The new `metabench` package has no third-party runtime dependency; its optional model
adapter uses RepoLaunch's existing LiteLLM dependency.

## Scope of this version

- Mine a local Git revision range through a JSON curation-agent adapter.
- Validate each task against an unchanged base and a reference solution, repeatedly.
- Freeze task statements, patches, checks, dimension weights, and environment identity.
- Run a model-driven shell agent for a fixed number of submission rounds, preserving
  repository state and conversation history between rounds.
- Evaluate immutable submission patches separately, after the complete trajectory.
- Record `(model, reasoning, step, score)` with configuration, per-dimension evidence,
  cumulative model calls, reported token usage, cost, elapsed time, and patch identity.
- Score correctness, regressions, explicit compatibility checks, and measured performance.
- Strictly regrade SWE-bench-Live status maps, counting missing/skipped expected tests as failures.

The Lance pilot uses [PR #8873](https://github.com/lance-format/lance/pull/8873).
Its tests are embedded in the same Rust file as the solution. The example separates them
at the inline test-module boundary and removes an implementation-specific panic-message
assertion. It is a reviewed historical pilot, not a representative Lance benchmark suite.

Future-requirement replay, subjective code review scores, automatic task-family sampling,
confidence intervals, model rankings, and token/cost enforcement are not implemented yet.
No placeholder score is assigned to an unsupported dimension. The current runner enforces
model-call and wall-clock budgets; token usage and cost are observations when available.

## Setup

Use Python 3.12 or later, Git, and Docker on Linux. From the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m metabench --help
.venv/bin/python -m unittest discover -s tests -v
```

No dependency installation is needed for the core commands. The existing upstream
`pip install -e .` workflow also installs the `metabench` entry point. For live model
access, initialize the `launch` submodule and install its dependencies using the
upstream [development instructions](Development.md), in the project environment.

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
  --environment environment.json --adapter model-adapter.json \
  --model PROVIDER/MODEL --reasoning YOUR_SETTING \
  --budget examples/metabench/budget.json --repeats 3 --out .metabench/run-001

.venv/bin/python -m metabench report \
  .metabench/run-001/results.jsonl --out .metabench/report.md
```

Validation requires repeatable target failures on the base, passing reference checks,
and usable environments. Changed task content or environment configuration requires new
validation. A suite has a deterministic content hash; modifying its contents invalidates it.
Private validation artifacts are operator-owned, not cryptographic attestations against
a malicious benchmark publisher.

`--trusted-local` exists for controlled local debugging and reference/no-op validation.
It executes repository commands on the host and **is not a security sandbox**. Such results
are always marked `score_eligible: false`; use this only with trusted code, never as an
isolated adversarial model benchmark. Docker is required for ordinary model runs.

## Model adapter protocol

The trusted adapter reads one JSON request per invocation:

```json
{"model":"provider/model","reasoning":"high","messages":[{"role":"user","content":"..."}]}
```

It returns one action and optional usage:

```json
{"action":{"command":"git diff"},"usage":{"input_tokens":100,"output_tokens":20,"cost_usd":0.001}}
```

or `{"action":{"submit":true}}`. Unknown usage remains `null`, not zero. The provided
`examples/metabench/repolaunch_model.py` adapter uses the LiteLLM completion interface
already used by RepoLaunch; API credentials stay in the trusted adapter process.
Providers that need a different endpoint can supply another JSON adapter. Adapter
configuration, model version, and reasoning setting are part of the run identity.

`examples/metabench/codex_model.py` is an alternative that uses saved local Codex CLI
authentication. It disables native CLI tools, plugins, memories and inherited app routing;
the CLI returns one structured decision for the metabench container to execute. Unexpected
native tool events invalidate the invocation. Set `METABENCH_PROVIDER_LOG_DIR` to preserve
the raw provider event streams. This is a fixed Codex CLI inference wrapper, not a claim
of equivalence to direct API requests or the full Codex coding-agent product.

The runner stores per-call usage, each round's incremental `step_usage`, and cumulative
`usage`. Counters include input, cached input, cache-write input, output and reasoning
output tokens where provided. Cached input is a subset of input, and reasoning output is
a subset of output: `total_tokens = input_tokens + output_tokens`, without adding either
subset again. Unknown counters, including unobserved timed-out requests and unavailable
ChatGPT-account billing, remain null rather than being estimated as zero. CLI usage
semantics follow the [official JSON event format](https://learn.chatgpt.com/docs/non-interactive-mode).

An environment configuration may contain `by_task`, mapping each instance ID to its own
validated environment. This permits separate immutable images containing each task's
original-base build cache. Each task still validates against the exact selected environment
hash; caches from reference solutions or other future repo revisions must not enter images.

One `step` is one submission round. `agent_steps` counts cumulative model calls, including
submission calls. Each round has independent call/time limits. The model gets its prior
conversation, current workspace, and a fixed request to review the original requirements.
It can run public tests. Hidden scores, failures, test patches, and reference code are
evaluated only after generation and are never used as continuation feedback. This version
does not simulate reviewer-specific advice or choose a best patch using hidden scores.

The command adapter is trusted infrastructure, not an untrusted subprocess sandbox. It
must not retrieve answers, access private evaluator files, or add undocumented tools.
Candidate shell commands execute only in the task container in normal Docker mode.

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

Report means first average repeats within each task, then average tasks. Failed builds
and submissions score zero. Infrastructure/evaluator failures remain visible and make
the corresponding aggregate incomplete. Runs from different suites cannot be combined.
The report does not select the highest-scoring historical checkpoint, pool different
configuration hashes, or assert statistically significant model rankings.

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
