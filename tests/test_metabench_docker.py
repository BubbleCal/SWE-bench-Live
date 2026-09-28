"""Real Docker integration; set METABENCH_TEST_IMAGE to a pinned prepared image."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from metabench.evaluate import evaluate
from metabench.runtime import Workspace, git


@unittest.skipUnless(os.environ.get("METABENCH_TEST_IMAGE"), "set METABENCH_TEST_IMAGE for Docker integration")
class DockerIntegrationTest(unittest.TestCase):
    def test_isolated_snapshot_and_evaluator(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            git(repo, "config", "user.email", "test@example.com")
            git(repo, "config", "user.name", "test")
            (repo / "pkg").mkdir()
            (repo / "pkg/calc.py").write_text("answer = 0\n")
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "base")
            base = git(repo, "rev-parse", "HEAD")
            (repo / "pkg/calc.py").write_text("answer = 42\n")
            patch = subprocess.check_output(["git", "-C", str(repo), "diff"], text=True)
            task = {"instance_id": "docker-control", "repo": str(repo), "base_commit": base,
                    "problem_statement": "Set answer to 42.", "patch": patch,
                    "checks": [{"id": "answer", "dimension": "correctness", "critical": True,
                                "command": "python -c 'from pkg.calc import answer; assert answer==42; print(\"OK\")'",
                                "success_pattern": "^OK$", "failure_pattern": "AssertionError"}],
                    "weights": {"correctness": 1}}
            env = {"image": os.environ["METABENCH_TEST_IMAGE"]}
            with Workspace(repo, task, env) as workspace:
                network = subprocess.check_output(["docker", "inspect", workspace.container, "--format", "{{.HostConfig.NetworkMode}}"], text=True).strip()
                self.assertEqual(network, "none")
                self.assertEqual(workspace.must("git rev-list --all --count")["output"].strip(), "1")
                # Timed-out exec descendants must not keep modifying the workspace.
                result = workspace.command("sleep 2; touch leaked", timeout=0.2)
                self.assertNotEqual(result["returncode"], 0)
                workspace.must("sleep 2; test ! -f leaked")
            baseline = evaluate(repo, task, "", env)
            reference = evaluate(repo, task, patch, env)
            self.assertEqual(baseline["score"], 0)
            self.assertEqual(reference["score"], 100)
            self.assertTrue(reference["environment"]["score_eligible"])


if __name__ == "__main__":
    unittest.main()
