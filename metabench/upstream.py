"""Adapter for the unmodified, pinned SWE-bench-Live source dependency.

Upstream exposes scripts and generic ``evaluation``/``launch`` namespaces, not
a standalone library distribution. Keep those imports in a child process so
they cannot shadow the controller's packages. Its working directory belongs
to the caller; output is never redirected into the dependency checkout.
"""

import argparse
import json
import os
import subprocess
import sys
from importlib.resources import files
from pathlib import Path


def dependency_lock():
    return json.loads(files("metabench").joinpath("upstream-lock.json").read_text())


class SWEBenchLive:
    """Version-checked source dependency with an isolated script interface."""

    OPERATIONS = {
        "evaluate": ("-m", "evaluation.evaluation"),
        "validate": ("-m", "evaluation.validation"),
        "crawl": ("curation/crawl_repo.py",),
        "filter": ("curation/filter_repo.py",),
    }

    def __init__(self, source=None):
        self.lock = dependency_lock()
        self.source = Path(source).resolve() if source else (
            Path(__file__).resolve().parent.parent / self.lock["path"]
        )

    @staticmethod
    def _check_checkout(path, revision):
        if not (path / ".git").exists():
            raise ValueError(
                f"Missing source dependency: {path}. "
                "Run git submodule update --init --recursive, "
                "or pass --source with the pinned checkout."
            )
        def git(*args):
            result = subprocess.run(["git", "-C", str(path), *args],
                                    capture_output=True, text=True)
            if result.returncode:
                raise ValueError(f"Cannot inspect source dependency: {path}")
            return result.stdout.strip()
        actual = git("rev-parse", "HEAD")
        if actual != revision:
            raise ValueError(f"Dependency revision mismatch at {path}: expected {revision}, got {actual}")
        # Include untracked files: an added Python module can change import behavior.
        if git("status", "--porcelain", "--untracked-files=all"):
            raise ValueError(f"Source dependency has local changes: {path}; use an unmodified checkout")
        return actual

    def inspect(self):
        revision = self._check_checkout(self.source, self.lock["revision"])
        launch = self.source / self.lock["repolaunch"]["path"]
        launch_revision = self._check_checkout(launch, self.lock["repolaunch"]["revision"])
        return {"repository": self.lock["repository"], "revision": revision,
                "source": str(self.source), "repolaunch_revision": launch_revision}

    def run(self, operation, arguments=(), *, cwd=None, python=None):
        """Forward argv and the exit code; do not import upstream into this process.

        File arguments are interpreted relative to ``cwd`` (the caller's current
        directory by default). Install the optional ``live`` extra in the
        selected Python environment before running upstream scripts.
        """
        if operation not in self.OPERATIONS:
            raise ValueError(f"Unknown upstream operation: {operation}")
        self.inspect()
        working = Path(cwd or Path.cwd()).resolve()
        if working == self.source or self.source in working.parents:
            raise ValueError("Run upstream commands outside the dependency checkout")
        entry = self.OPERATIONS[operation]
        if entry[0] != "-m":
            entry = (str(self.source / entry[0]),)
        environment = dict(os.environ)
        # Do not inherit another checkout's PYTHONPATH. Installed dependencies
        # remain available through the selected interpreter's site-packages.
        environment["PYTHONPATH"] = os.pathsep.join(
            (str(self.source), str(self.source / self.lock["repolaunch"]["path"]))
        )
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run([str(python or sys.executable), *entry, *arguments],
                              cwd=working, env=environment).returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description="Use the pinned SWE-bench-Live dependency")
    parser.add_argument("--source", type=Path, help="explicit pinned source checkout (for installed wheels)")
    parser.add_argument("operation", choices=["doctor", *SWEBenchLive.OPERATIONS])
    parser.add_argument("arguments", nargs=argparse.REMAINDER, help="upstream argv after --")
    args = parser.parse_args(argv)
    try:
        dependency = SWEBenchLive(args.source)
        if args.operation == "doctor":
            if args.arguments:
                raise ValueError("doctor takes no upstream arguments")
            print(json.dumps(dependency.inspect(), indent=2))
            return 0
        # argparse already consumes the adapter separator. Any remaining --
        # belongs to upstream (notably Python Fire's own help separator).
        return dependency.run(args.operation, args.arguments)
    except (ValueError, OSError) as error:
        parser.exit(2, f"metabench upstream: {error}\n")


if __name__ == "__main__":
    sys.exit(main())
