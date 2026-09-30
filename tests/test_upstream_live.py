"""Optional smoke tests against the real pinned dependency, without Docker.

These collect saved fixture reports; they do not claim to benchmark an agent.
Run with METABENCH_TEST_LIVE=1 in an environment with meta-bench[live].
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from metabench.upstream import SWEBenchLive
from metabench.evaluate import grade_live_status


@unittest.skipUnless(os.environ.get("METABENCH_TEST_LIVE") == "1", "set METABENCH_TEST_LIVE with the live extra")
class LiveDependencyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dependency = SWEBenchLive(Path(__file__).resolve().parents[1] / "dependencies/swe-bench-live")
        self.task = {"instance_id": "upstream-fixture", "patch": "fixture nonempty patch",
                     "FAIL_TO_PASS": ["fixed"], "PASS_TO_PASS": ["existing"]}
        (self.root / "tasks.jsonl").write_text(json.dumps(self.task) + "\n")

    def test_real_evaluator_collects_saved_reports_outside_dependency(self):
        folder = self.root / "output" / self.task["instance_id"]
        folder.mkdir(parents=True)
        (folder / "report.json").write_text(json.dumps({"instance_id": self.task["instance_id"], "resolved": True}))
        code = self.dependency.run("evaluate", ["--dataset", "tasks.jsonl", "--patch_dir", "gold",
            "--platform", "linux", "--workers", "1", "--output_dir", "output",
            "--overwrite", "0", "--collect-only"], cwd=self.root)
        self.assertEqual(code, 0)
        result = json.loads((self.root / "output/results.json").read_text())
        self.assertEqual(result["success_ids"], [self.task["instance_id"]])
        self.assertEqual(result["error"], 0)
        self.dependency.inspect()
        # Importing upstream's resolved flag never relaxes our strict checks.
        self.assertFalse(grade_live_status(self.task, {"fixed": "pass"})["critical_pass"])

    def test_real_validator_collects_saved_status(self):
        folder = self.root / "output" / self.task["instance_id"]
        folder.mkdir(parents=True)
        (folder / "status.json").write_text(json.dumps(self.task))
        code = self.dependency.run("validate", ["--input_dir", "tasks.jsonl", "--platform", "linux",
            "--workers", "1", "--output_dir", "output", "--overwrite", "0", "--collect_only", "True"],
            cwd=self.root)
        self.assertEqual(code, 0)
        collected = [json.loads(line) for line in (self.root / "output/validated_instances.jsonl").read_text().splitlines()]
        self.assertEqual([task["instance_id"] for task in collected], [self.task["instance_id"]])
        self.dependency.inspect()


if __name__ == "__main__":
    unittest.main()
