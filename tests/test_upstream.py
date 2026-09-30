import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from metabench.upstream import SWEBenchLive, dependency_lock, main


class UpstreamTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        launch = self.source / "launch"
        launch.mkdir()
        (launch / "provider_marker.py").write_text("VALUE = 'nested-runtime'\n")
        launch_revision = self.commit_repo(launch)
        (self.source / "evaluation").mkdir()
        (self.source / "evaluation/__init__.py").write_text("")
        (self.source / "evaluation/evaluation.py").write_text(
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "from provider_marker import VALUE\n"
            "Path('output.json').write_text(json.dumps({'argv':sys.argv[1:],"
            "'cwd':os.getcwd(),'provider':VALUE}))\n"
            "raise SystemExit(7)\n"
        )
        revision = self.commit_repo(self.source)
        lock = {"repository": "fixture", "path": "unused", "revision": revision,
                "repolaunch": {"path": "launch", "revision": launch_revision}}
        self.lock_patch = patch("metabench.upstream.dependency_lock", return_value=lock)
        self.lock_patch.start()
        self.addCleanup(self.lock_patch.stop)
        self.dependency = SWEBenchLive(self.source)

    @staticmethod
    def commit_repo(root):
        for command in (("init", "-q"), ("config", "user.name", "test"),
                        ("config", "user.email", "test@example.com"), ("add", "-A"),
                        ("-c", "core.hooksPath=/dev/null", "commit", "-qm", "fixture")):
            subprocess.run(["git", "-C", str(root), *command], check=True, capture_output=True)
        return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()

    def test_child_imports_nested_runtime_and_keeps_caller_paths_and_exit_code(self):
        working = self.root / "caller"
        working.mkdir()
        before = set(sys.modules)
        with patch.dict(os.environ, {"PYTHONPATH": "/unrelated/checkout"}):
            code = self.dependency.run("evaluate", ["--dataset", "tasks with spaces.jsonl"], cwd=working)
        self.assertEqual(code, 7)
        self.assertEqual(json.loads((working / "output.json").read_text()),
                         {"argv": ["--dataset", "tasks with spaces.jsonl"],
                          "cwd": str(working.resolve()), "provider": "nested-runtime"})
        self.assertFalse((self.source / "output.json").exists())
        self.assertEqual(set(sys.modules) - before & {"evaluation", "provider_marker"}, set())
        self.dependency.inspect()  # no pycache or output dirtied the source

    def test_missing_dependency_is_actionable(self):
        with self.assertRaisesRegex(ValueError, "submodule update"):
            SWEBenchLive(self.root / "missing").inspect()

    def test_changed_dependency_revision_is_rejected(self):
        self.dependency.lock["revision"] = "0" * 40
        with self.assertRaisesRegex(ValueError, "revision mismatch"):
            self.dependency.inspect()

    def test_modified_or_added_source_is_rejected(self):
        path = self.source / "evaluation/evaluation.py"
        original = path.read_text()
        path.write_text(original + "# changed\n")
        with self.assertRaisesRegex(ValueError, "local changes"):
            self.dependency.inspect()
        path.write_text(original)
        (self.source / "injected.py").write_text("pass\n")
        with self.assertRaisesRegex(ValueError, "local changes"):
            self.dependency.inspect()

    def test_changed_nested_runtime_is_rejected(self):
        (self.source / "launch/provider_marker.py").write_text("VALUE = 'changed'\n")
        with self.assertRaisesRegex(ValueError, "local changes"):
            self.dependency.inspect()

    def test_missing_nested_runtime_is_actionable(self):
        self.dependency.lock["repolaunch"]["path"] = "absent"
        with self.assertRaisesRegex(ValueError, "Missing source dependency"):
            self.dependency.inspect()

    def test_dependency_cannot_be_the_output_working_directory(self):
        with self.assertRaisesRegex(ValueError, "outside the dependency"):
            self.dependency.run("evaluate", cwd=self.source / "evaluation")

    def test_cli_preserves_an_upstream_argument_separator(self):
        with patch.object(SWEBenchLive, "run", return_value=7) as run:
            self.assertEqual(main(["--source", str(self.source), "filter", "--", "--", "--help"]), 7)
        run.assert_called_once_with("filter", ["--", "--help"])


class DependencyMetadataTest(unittest.TestCase):
    def test_distribution_and_pins_are_consistent(self):
        root = Path(__file__).resolve().parents[1]
        metadata = tomllib.loads((root / "pyproject.toml").read_text())
        lock = dependency_lock()
        self.assertEqual(metadata["project"]["name"], "meta-bench")
        self.assertEqual(metadata["project"]["dependencies"], [])
        self.assertEqual(metadata["tool"]["setuptools"]["packages"]["find"]["include"],
                         ["metabench", "metabench.*"])
        self.assertIn("launch @ git+" + lock["repolaunch"]["repository"] + "@" + lock["repolaunch"]["revision"],
                      metadata["project"]["optional-dependencies"]["live"])
        link = subprocess.check_output(["git", "-C", str(root), "ls-files", "--stage", lock["path"]], text=True)
        self.assertEqual(link.split()[:2], ["160000", lock["revision"]])


if __name__ == "__main__":
    unittest.main()
