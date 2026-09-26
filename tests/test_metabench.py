import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from metabench.evaluate import evaluate, grade_live_status, validate
from metabench.agent import CommandModel
from metabench.mine import mine
from metabench.report import report
from metabench.run import run
from metabench.runtime import Workspace, git
from metabench.schema import digest, freeze, load_suite, public_task, write_json


class MetaBenchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "test")
        (self.repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        self.commit("initial")
        base = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        patch = subprocess.check_output(["git", "-C", str(self.repo), "diff"], text=True)
        self.commit("fix addition")
        (self.repo / "FUTURE_SECRET").write_text("Do not expose future state")
        self.commit("future change")
        python = sys.executable
        self.task = {"instance_id": "addition", "repo": str(self.repo), "base_commit": base,
                     "problem_statement": "Add two integers correctly.", "patch": patch,
                     "test_patch": "", "weights": {"correctness": 80, "regression": 20},
                     "checks": [
                         {"id": "addition", "dimension": "correctness", "critical": True,
                          "command": f"{python} -c 'from calc import add; assert add(2,3)==5; print(\"VERIFIED\")'",
                          "success_pattern": "^VERIFIED$", "failure_pattern": "AssertionError"},
                         {"id": "zero", "dimension": "regression", "critical": True,
                          "command": f"{python} -c 'from calc import add; assert add(0,0)==0; print(\"VERIFIED\")'",
                          "success_pattern": "^VERIFIED$"}]}
        self.env = {}

    def tearDown(self):
        self.temp.cleanup()

    def commit(self, message):
        git(self.repo, "add", "-A")
        git(self.repo, "-c", "core.hooksPath=/dev/null", "commit", "-qm", message)

    def validated(self):
        return validate(self.repo, self.task, self.env, self.root / "validation", trusted_local=True, repeats=2)

    def test_reference_control_and_frozen_integrity(self):
        task = self.validated()
        self.assertTrue(task["validation"]["passed"])
        suite = freeze([task])
        path = self.root / "suite.json"
        write_json(path, suite)
        self.assertEqual(load_suite(path), suite)
        changed = copy.deepcopy(task)
        changed["problem_statement"] += " changed"
        with self.assertRaisesRegex(ValueError, "validate this exact"):
            freeze([changed])
        suite["tasks"][0]["patch"] += "tamper"
        write_json(path, suite)
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            load_suite(path)

    def test_empty_submission_fails_and_alternative_solution_passes(self):
        base = evaluate(self.repo, self.task, "", self.env, trusted_local=True)
        self.assertEqual(base["score"], 0)
        self.assertEqual(base["scores"]["regression"], 100)
        alternative = self.task["patch"].replace("+    return a + b", "+    return sum((a, b))")
        result = evaluate(self.repo, self.task, alternative, self.env, trusted_local=True)
        self.assertEqual(result["score"], 100)

    def test_git_future_and_private_task_data_are_not_visible(self):
        with Workspace(self.repo, self.task, {}, trusted_local=True) as workspace:
            self.assertFalse((workspace.root / "FUTURE_SECRET").exists())
            self.assertEqual(workspace.must("git rev-list --all --count")["output"].strip(), "1")
            self.assertEqual(workspace.must("git remote -v")["output"], "")
            result = workspace.command("git show " + git(self.repo, "rev-parse", "HEAD"))
            self.assertNotEqual(result["returncode"], 0)
        task = {**self.task, "new_secret_field": "hidden", "FAIL_TO_PASS": ["secret"]}
        self.assertEqual(set(public_task(task)), {"instance_id", "problem_statement"})

    def test_zero_tests_and_missing_regressions_are_failures(self):
        task = copy.deepcopy(self.task)
        task["checks"][0]["command"] = "true"
        result = evaluate(self.repo, task, self.task["patch"], {}, trusted_local=True)
        self.assertFalse(result["critical_pass"])
        live = {"FAIL_TO_PASS": ["fixed"], "PASS_TO_PASS": ["existing"]}
        for status in ({"fixed": "pass"}, {"fixed": "pass", "existing": "skip"}):
            self.assertFalse(grade_live_status(live, status)["critical_pass"])
        self.assertTrue(grade_live_status(live, {"fixed": "pass", "existing": "pass"})["critical_pass"])

    def test_validation_rejects_nondistinguishing_and_broken_tasks(self):
        task = copy.deepcopy(self.task)
        task["checks"][0]["command"] = "echo VERIFIED"
        result = validate(self.repo, task, {}, self.root / "bad", trusted_local=True, repeats=2)
        self.assertFalse(result["validation"]["passed"])
        task["checks"][0]["command"] = "exit 1"
        result = validate(self.repo, task, {}, self.root / "not-a-test", trusted_local=True, repeats=2)
        self.assertFalse(result["validation"]["passed"])
        result = validate(self.repo, self.task, {"setup": ["exit 1"]}, self.root / "build", trusted_local=True, repeats=2)
        self.assertFalse(result["validation"]["passed"])

    def test_internal_symlinks_work_but_escaping_links_are_rejected(self):
        (self.repo / "inside.py").symlink_to("calc.py")
        self.commit("add internal link")
        task = {**self.task, "base_commit": git(self.repo, "rev-parse", "HEAD")}
        with Workspace(self.repo, task, {}, trusted_local=True) as workspace:
            self.assertTrue((workspace.root / "inside.py").is_symlink())
        (self.repo / "outside").symlink_to("../../outside")
        self.commit("add escaping link")
        task["base_commit"] = git(self.repo, "rev-parse", "HEAD")
        with self.assertRaisesRegex(ValueError, "escapes"):
            Workspace(self.repo, task, {}, trusted_local=True)

    def test_two_rounds_preserve_state_and_do_not_leak_scores(self):
        task = self.validated()
        adapter = self.root / "adapter.py"
        adapter.write_text('''import json, sys
request = json.load(sys.stdin)
text = json.dumps(request["messages"])
assert "VERIFIED" not in text
assert "critical_pass" not in text
assert "FUTURE_SECRET" not in text
assert "reference" not in request
turns = sum(m["role"] == "assistant" for m in request["messages"])
if turns == 1:
    action = {"command": "printf 'def add(a, b):\\n    return a + b\\n' > calc.py"}
else:
    action = {"submit": True}
print(json.dumps({"action": action, "usage": {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 5, "reasoning_output_tokens": 2, "cost_usd": 0.01}}))
''')
        budget = {"max_steps": 2, "max_calls_per_step": 3, "seconds_per_step": 20, "tool_timeout": 5, "max_tool_output": 1000}
        rows = run(freeze([task]), self.repo, {"by_task": {task["instance_id"]: {}}}, [sys.executable, str(adapter)], "scripted-control", "none",
                   budget, self.root / "run", trusted_local=True)
        self.assertEqual([r["score"] for r in rows], [0, 100])
        self.assertEqual([r["agent_steps"] for r in rows], [1, 3])
        self.assertEqual(rows[-1]["usage"]["input_tokens"], 30)
        self.assertEqual(rows[-1]["usage"]["cached_input_tokens"], 12)
        self.assertEqual(rows[-1]["usage"]["reasoning_output_tokens"], 6)
        self.assertEqual(rows[-1]["usage"]["total_tokens"], 45)
        self.assertEqual(rows[-1]["step_usage"]["total_tokens"], 30)
        self.assertTrue(all(not r["score_eligible"] for r in rows))
        self.assertIn("scripted-control", report(rows))

    def test_mining_preserves_reference_and_requires_validation(self):
        adapter = self.root / "curator.py"
        response = {k: self.task[k] for k in ("problem_statement", "patch", "test_patch", "checks", "weights")}
        adapter.write_text("import json, sys\nsource=json.load(sys.stdin)\nassert source['historical_patch']\nprint(" + repr(json.dumps(response)) + ")\n")
        tasks = mine(self.repo, self.task["base_commit"] + "..HEAD~1", [sys.executable, str(adapter)])
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["base_commit"], self.task["base_commit"])
        with self.assertRaises(ValueError):
            freeze(tasks)

    def test_performance_samples_and_frozen_thresholds(self):
        task = copy.deepcopy(self.task)
        task["checks"].append({"id": "latency", "dimension": "performance", "command": "echo 5", "direction": "lower", "good": 5, "bad": 10, "repeats": 3})
        task["weights"]["performance"] = 20
        result = evaluate(self.repo, task, self.task["patch"], {}, trusted_local=True)
        self.assertEqual(result["checks"]["latency"]["samples"], [5, 5, 5])
        self.assertEqual(result["scores"]["performance"], 100)
        task["checks"][-1]["command"] = "echo nan"
        result = evaluate(self.repo, task, self.task["patch"], {}, trusted_local=True)
        self.assertEqual(result["scores"]["performance"], 0)

    def test_adapter_diagnostics_unknown_usage_and_invalid_actions(self):
        script = self.root / "model.py"
        script.write_text('import json,sys\nprint("diagnostic", file=sys.stderr)\nprint(json.dumps({"action":{"submit":True}}))\n')
        model = CommandModel([sys.executable, str(script)], "test", "default")
        action, usage = model.invoke([], 5)
        self.assertEqual(action, {"submit": True})
        self.assertTrue(all(value is None for value in usage.values()))
        script.write_text('print(\'{"action": null}\')\n')
        with self.assertRaises(ValueError):
            model.invoke([], 5)

    def test_error_rows_preserve_coverage_and_reports_reject_mixed_suites(self):
        task = self.validated()
        budget = {"max_steps": 2, "max_calls_per_step": 1, "seconds_per_step": 5, "tool_timeout": 1, "max_tool_output": 100}
        rows = run(freeze([task]), self.repo, {}, ["/missing/adapter"], "missing", "none",
                   budget, self.root / "broken-run", trusted_local=True)
        self.assertEqual([r["step"] for r in rows], [1, 2])
        self.assertTrue(all(r["score"] is None for r in rows))
        self.assertIn("incomplete", report(rows))
        with self.assertRaisesRegex(ValueError, "same frozen suite"):
            report([*rows, {**rows[0], "suite_id": "other"}])


if __name__ == "__main__":
    unittest.main()
