import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from metabench.legacy.agent import CommandModel, trajectory


class Workspace:
    environment = {}

    def snapshot(self):
        return "preserved candidate snapshot"


class RoundTimeoutTest(unittest.TestCase):
    def test_expired_round_preserves_snapshot_and_continues_next_round(self):
        timeout = {"returncode": -9, "timed_out": True, "output": "", "stderr": ""}
        submitted = {"returncode": 0, "timed_out": False, "stderr": "", "output": json.dumps({
            "action": {"submit": True}, "usage": {"input_tokens": 10, "output_tokens": 3}})}
        budget = {"max_steps": 2, "max_calls_per_step": 1, "seconds_per_step": 10,
                  "tool_timeout": 1, "max_tool_output": 100}
        task = {"instance_id": "timeout-control", "problem_statement": "Finish the task."}
        with tempfile.TemporaryDirectory() as temp, patch("metabench.legacy.agent.execute", side_effect=[timeout, submitted]):
            rows = trajectory(Workspace(), task, CommandModel(["unused"], "test", "high"), budget, Path(temp))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["status"], "round_budget_exhausted")
        self.assertEqual(rows[1]["status"], "submitted")
        self.assertEqual(rows[0]["patch"], rows[1]["patch"])
        self.assertEqual(rows[1]["agent_steps"], 2)
        self.assertIsNone(rows[1]["usage"]["total_tokens"])
        self.assertEqual(rows[1]["step_usage"]["total_tokens"], 13)


if __name__ == "__main__":
    unittest.main()
