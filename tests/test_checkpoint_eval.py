import tempfile
import unittest
from unittest.mock import patch

from metabench.checkpoint_eval import evaluate, performance_scores
from tests.test_benchmark_build import BuildWorkspace


class CheckpointEvaluationTest(unittest.TestCase):
    def test_failed_functional_check_skips_all_performance_work(self):
        functional = {"score": 0, "scores": {"correctness": 0}, "critical_pass": False,
                      "checks": {"regression": {"passed": False, "status": "test_failed"}}}
        task = {"patch": "reference", "supplemental": {
            "crate": "fixture", "benchmark_source": "// bench", "repeats": 7}}
        with patch("metabench.vm_worker.CachedWorkspace",
                   side_effect=AssertionError("failed candidates must not start performance work")) as workspace:
            result = evaluate("/fixture", {"trial_id": "trial", "task": task, "patch": "candidate"},
                              object(), functional)
        workspace.assert_not_called()
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["performance_status"], "excluded_incorrect_solution")
        self.assertEqual(result["supplemental_evaluation"]["pairs"], [])
        self.assertEqual(result["checks"], functional["checks"])

    def test_paired_scores_reject_missing_or_misaligned_metrics(self):
        def sample(value, name="latency"):
            return {"valid": True, "metrics": [{"name": name, "unit": "ns/op", "value": value}]}

        pairs = [{"reference": sample(10), "candidate": sample(20)} for _ in range(7)]
        result = performance_scores(pairs, 7, .05)
        self.assertAlmostEqual(result[0]["score"], 100 / 3)
        self.assertEqual(result[0]["speedup"], .5)
        self.assertIsNone(performance_scores(pairs[:-1], 7, .05))
        pairs[2]["candidate"] = sample(20, "different workload")
        self.assertIsNone(performance_scores(pairs, 7, .05))

    def test_legacy_evolution_payload_is_not_executed_or_scored(self):
        task = {"patch": "reference", "supplemental": {
            "crate": "fixture", "benchmark_source": "// bench", "repeats": 7,
            "features": ["safe-feature"], "future_source": "// must not execute", "future_patch": "bad patch"}}
        functional = {"score": 100, "scores": {"correctness": 100}, "critical_pass": True, "checks": {}}
        with tempfile.TemporaryDirectory() as directory:
            workspaces = {}
            def workspace(root, request, lane):
                key = request['trial_id'], lane
                if key not in workspaces:
                    workspaces[key] = BuildWorkspace(root, request, lane)
                return workspaces[key]
            request = {"trial_id": "trial", "task": task, "patch": "candidate",
                       "base_sha256": "base archive", "environment": {"image": "sha256:fixture"}}
            with patch("metabench.vm_worker.CachedWorkspace", side_effect=workspace), \
                    patch("metabench.vm_worker.execute", return_value={"returncode": 0}):
                result = evaluate(directory, request, object(), functional)
                second = evaluate(directory, request, object(), functional)
                # A separate trial can share only the trusted reference binary.
                third = evaluate(directory, {**request, "trial_id": "another"}, object(), functional)
            self.assertEqual({w.lane for w in workspaces.values()}, {"reference", "performance"})
            self.assertEqual(len(workspaces), 3)
            self.assertTrue(second["supplemental_evaluation"]["reference_build"]["cache_hit"])
            self.assertTrue(second["supplemental_evaluation"]["candidate_build"]["cache_hit"])
            self.assertTrue(third["supplemental_evaluation"]["reference_build"]["cache_hit"])
            self.assertFalse(third["supplemental_evaluation"]["candidate_build"]["cache_hit"])
            reference = next(w for w in workspaces.values() if w.lane == "reference")
            self.assertEqual(reference.compilations, 1)
            self.assertEqual(reference.samples, 21, "cached binaries must not reuse old timing samples")
            for value in (result, second, third):
                self.assertEqual(len(value["supplemental_evaluation"]["pairs"]), 7)
                self.assertEqual(value["score"], 87.5)
            commands = [c for w in workspaces.values() for c in w.commands]
        self.assertEqual(result["scores"], {"functional": 100, "performance": 50})
        self.assertEqual(result["score"], 87.5)
        self.assertNotIn("future_evolution_status", result)
        self.assertFalse(any("cargo test" in c or "git apply" in c or "metabench_future" in c for c in commands))
        self.assertTrue(any("--features safe-feature" in c for c in commands))

    def test_infrastructure_check_failure_stays_reviewable_without_performance_work(self):
        task = {"supplemental": {"repeats": 7}}
        functional = {"score": 0, "scores": {"correctness": 0}, "critical_pass": False,
                      "checks": {"test": {"passed": False, "status": "execution_failed"}}}
        with patch("metabench.vm_worker.CachedWorkspace") as workspace:
            result = evaluate("/fixture", {"task": task}, object(), functional)
        workspace.assert_not_called()
        self.assertEqual(result["supplemental_status"], "needs_review")


if __name__ == "__main__":
    unittest.main()
