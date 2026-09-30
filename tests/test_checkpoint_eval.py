import json
import unittest
from unittest.mock import patch

from metabench.checkpoint_eval import evaluate, performance_scores


class CheckpointEvaluationTest(unittest.TestCase):
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
        class Workspace:
            container = "fixture"

            def __init__(self, *args):
                self.commands = []

            def command(self, command, timeout=900, input=None):
                self.commands.append(command)
                output = ""
                if "--message-format=json" in command:
                    output = json.dumps({"reason": "compiler-artifact",
                                         "target": {"name": "metabench_perf"},
                                         "executable": "/fixture/bench"})
                elif command.startswith("taskset"):
                    output = "MB_METRIC " + json.dumps({"name": "latency", "unit": "ns/op", "value": 100})
                return {"returncode": 0, "output": output}

            def prepare(self, *args):
                pass

            def restore_source_ownership(self):
                return {"returncode": 0}

        candidate, reference = Workspace(), Workspace()
        task = {"patch": "reference", "supplemental": {
            "crate": "fixture", "benchmark_source": "// bench", "repeats": 7,
            "features": ["safe-feature"], "future_source": "// must not execute", "future_patch": "bad patch"}}
        functional = {"score": 100, "scores": {"correctness": 100}, "critical_pass": True, "checks": {}}
        with patch("metabench.vm_worker.CachedWorkspace", return_value=reference), \
                patch("metabench.vm_worker.execute", return_value={"returncode": 0}):
            result = evaluate("/fixture", {"trial_id": "trial", "task": task, "patch": "candidate"},
                              candidate, functional)
        self.assertEqual(result["scores"], {"functional": 100, "performance": 50})
        self.assertEqual(result["score"], 87.5)
        self.assertNotIn("future_evolution_status", result)
        commands = reference.commands + candidate.commands
        self.assertFalse(any("cargo test" in c or "git apply" in c or "metabench_future" in c for c in commands))
        self.assertTrue(any("--features safe-feature" in c for c in commands))


if __name__ == "__main__":
    unittest.main()
