# Development

Use the project-local `.venv` (plain venv/pip). Do not install dependencies globally.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[reports]'
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/metabench --help
```

Core tests do not require SWE-bench-Live, model credentials or cloud VMs.
Initialize source dependencies only when exercising the upstream adapter:

```sh
git submodule update --init --recursive
.venv/bin/metabench upstream doctor
.venv/bin/python -m pip install -e '.[live]'
.venv/bin/metabench upstream evaluate -- --help
METABENCH_TEST_LIVE=1 .venv/bin/python -m unittest discover -s tests -p 'test_upstream_live.py' -v
```

The adapter tests exercise revision checks, isolation, argument/exit-code forwarding
and caller-relative output paths with a local fixture. The `doctor` command checks
the actual pinned source checkouts. Optional live tests collect saved fixture reports
through the real evaluator/validator without Docker or model calls. Running upstream evaluation additionally needs
Docker and the corresponding task images.

```sh
docker build -t metabench-python -f examples/metabench/python.Dockerfile .
METABENCH_TEST_IMAGE=$(docker image inspect metabench-python --format '{{.Id}}') \
  .venv/bin/python -m unittest discover -s tests -p '*docker.py' -v
```

Dependencies under `dependencies/` are external source, not our implementation.
Never patch them for meta-bench behavior. Update their commits, packaged source lock,
optional dependency metadata and validation together when upgrading.
`metabench` is the only namespace in the built wheel. Archived benchmark artifacts
under `.metabench/` retain their original code and protocol fingerprints.

The local Git history and remote retain the original fork for traceability. This
migration does not create or publish a new GitHub repository.
