import copy
import unittest
from metabench.scoring import combined_score, without_evolution, EVALUATION_POLICY_ID
from metabench.schema import validate_task


class ScoringTest(unittest.TestCase):
    def test_normalized_weights_and_functional_gate(self):
        self.assertEqual(combined_score(100, 50, True), 87.5)
        self.assertEqual(combined_score(0, None, False), 0)
        self.assertIsNone(combined_score(100, None, True))

    def test_historical_projection_retains_provenance_without_mutating_receipts(self):
        row = {"scores": {"functional": 100, "performance": 50, "future_evolution": 100},
               "score": 90, "critical_pass": True, "score_dimensions": ["functional", "performance", "future_evolution"],
               "future_evolution_status": "passed", "usage": {"total_tokens": 123}}
        original = copy.deepcopy(row)
        projected = without_evolution([row])[0]
        self.assertEqual(row, original)
        self.assertEqual(projected["scores"], {"functional": 100, "performance": 50})
        self.assertEqual(projected["score"], 87.5)
        self.assertEqual(projected["evaluation_policy_id"], EVALUATION_POLICY_ID)
        self.assertEqual(projected["evaluation_rescoring"]["source_total_score"], 90)
        self.assertEqual(projected["usage"], row["usage"])
        self.assertNotIn("future_evolution_status", projected)
        self.assertEqual(without_evolution([projected]), [projected])

    def test_performance_specs_do_not_require_evolution_and_reject_unsafe_features(self):
        task = {"instance_id": "fixture", "repo": "fixture", "base_commit": "a" * 40,
                "problem_statement": "fixture", "patch": "fixture",
                "checks": [{"id": "fixture", "dimension": "correctness", "critical": True,
                            "command": "fixture", "success_pattern": "VERIFIED"}], "weights": {"correctness": 100},
                "supplemental": {"crate": "fixture", "benchmark_source": "fn main() {}", "repeats": 7}}
        self.assertEqual(validate_task(task), task)
        task["supplemental"]["features"] = ["datafusion; echo unsafe"]
        with self.assertRaisesRegex(ValueError, "safe Cargo feature"):
            validate_task(task)


if __name__ == "__main__":
    unittest.main()
