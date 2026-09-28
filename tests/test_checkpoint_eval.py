import unittest

from metabench.checkpoint_eval import future, performance_scores


class CheckpointEvaluationTest(unittest.TestCase):
    def test_paired_scores_reject_missing_or_misaligned_metrics(self):
        def sample(value, name="latency"):
            return {"valid": True, "metrics": [{"name": name, "unit": "ns/op", "value": value}]}
        pairs = [{"reference": sample(10), "candidate": sample(20)} for _ in range(7)]
        result = performance_scores(pairs, 7, .05)
        self.assertEqual(result[0]["score"], 52.5)
        self.assertEqual(result[0]["speedup"], .5)
        self.assertIsNone(performance_scores(pairs[:-1], 7, .05))
        pairs[2]["candidate"] = sample(20, "different workload")
        self.assertIsNone(performance_scores(pairs, 7, .05))

    def test_missing_tests_and_build_errors_are_not_compatibility_success(self):
        class Workspace:
            def __init__(self, output, code):
                self.output, self.code = output, code
            def command(self, command, timeout, source=None):
                if command.startswith("cargo test"):
                    return {"returncode": self.code, "output": self.output, "timed_out": False}
                return {"returncode": 0, "output": ""}
        spec = {"crate": "fixture", "future_source": "// frozen", "future_test_count": 2}
        for output, code in [("error: compiler failed", 101),
                             ("test result: ok. 0 passed; 0 failed; 0 ignored;", 0),
                             ("test result: ok. 1 passed; 0 failed; 0 ignored;", 0),
                             ("test result: ok. 2 passed; 0 failed; 0 ignored;", 101)]:
            with self.subTest(output=output, code=code):
                self.assertIsNone(future(Workspace(output, code), spec)["score"])
        result = future(Workspace("test result: FAILED. 1 passed; 1 failed; 0 ignored;", 101), spec)
        self.assertEqual(result["score"], 50)
        self.assertEqual(result["status"], "behavior_failed")


if __name__ == "__main__":
    unittest.main()
