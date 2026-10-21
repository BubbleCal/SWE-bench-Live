import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from metabench.checkouts import git
from metabench.elastic import attach
from metabench.evaluate import evaluate, validate
from metabench.native_run import run_matrix
from metabench.queue import TestQueue
from metabench.schema import freeze
from metabench.test_mcp import request
from metabench.usage import normalize
from metabench.vm_pool import VMPool


class SplitPoolsTest(unittest.TestCase):
    def test_public_test_finishes_while_hidden_grade_is_blocked_and_resume_does_not_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            git("init", "-q", repo)
            git("config", "user.name", "test", cwd=repo)
            git("config", "user.email", "test@localhost", cwd=repo)
            (repo / "value.py").write_text("value = 0\n")
            git("add", ".", cwd=repo)
            git("commit", "-qm", "base", cwd=repo)
            base = git("rev-parse", "HEAD", cwd=repo)
            (repo / "value.py").write_text("value = 1\n")
            fix = git("diff", cwd=repo) + "\n"
            environment = {"image": "sha256:" + "a" * 64}
            task = {"instance_id": "issue", "repo": str(repo), "base_commit": base,
                    "problem_statement": "Set the value to one.", "patch": fix, "test_patch": "",
                    "weights": {"correctness": 1}, "checks": [{"id": "value", "dimension": "correctness",
                    "critical": True, "command": "python3 -c 'from value import value; assert value == 1; print(\"OK\")'",
                    "success_pattern": "^OK$", "failure_pattern": "AssertionError"}]}
            task = validate(repo, task, environment, root / "validation", trusted_local=True, repeats=2)
            self.assertTrue(task["validation"]["passed"])
            task["validation"]["environment"] = {"backend": "docker", "score_eligible": True}
            suite = freeze([task])
            matrix = {"agents": [{"provider": "claude", "model": "fixture", "reasoning": "high"}],
                      "max_rounds": 2, "parallel_agents": 1, "max_parallel_agents_per_vm": 1,
                      "vms": [{"id": "public", "host": "worker-public"}],
                      "grading_vms": [{"id": "grading", "host": "worker-grading"}]}
            hidden_started, release_hidden = threading.Event(), threading.Event()
            calls, launched, staged, prepared = [], [], [], []

            class Driver:
                def __init__(self, spec, control):
                    self.id, self.jobs = spec["id"], {}
                def prepare(self, environments):
                    prepared.append(self.id)
                    return {"vm_id": self.id}
                def stage_base(self, base):
                    staged.append(self.id)
                    return {"vm_id": self.id}
                def launch(self, job):
                    if job["kind"] == "public" and self.id != "public":
                        raise AssertionError("public job leaked onto grading worker")
                    if job["kind"] == "verify" and self.id != "grading":
                        raise AssertionError("hidden job blocked public worker")
                    self.jobs[job["id"]] = job
                    launched.append(job)
                def wait(self, identity, stop):
                    job = self.jobs[identity]
                    if job["kind"] == "public":
                        return {"execution": {"returncode": 0, "output": "public result"}}
                    hidden_started.set()
                    if not release_hidden.wait(5):
                        raise AssertionError("second-round public test could not run while grading was blocked")
                    return evaluate(repo, task, job["payload"]["patch"], {}, trusted_local=True)

            def native(spec, worktree, prompt, out, **options):
                self.assertEqual(options["timeout"], 900)
                self.assertEqual(set(prepared), {"public", "grading"})
                self.assertEqual(set(staged), {"public", "grading"})
                calls.append(options["session_id"])
                (worktree / "value.py").write_text("value = 1\n")
                if len(calls) == 2:
                    self.assertTrue(hidden_started.wait(3))
                    self.assertFalse(release_hidden.is_set())
                config = json.loads(Path(options["mcp_path"]).read_text())["mcpServers"]["bench"]["args"]
                url, token = config[config.index("--url") + 1], Path(config[config.index("--token-file") + 1]).read_text()
                job = request(url + "/test", token, {"command": "public"})["job_id"]
                deadline = time.monotonic() + 3
                while request(url + "/jobs/" + job, token)["status"] != "completed":
                    if time.monotonic() >= deadline:
                        raise AssertionError("public test starved behind hidden grading")
                    time.sleep(.01)
                if len(calls) == 2:
                    hidden = next(j for j in launched if j["kind"] == "verify")
                    with self.assertRaises(HTTPError) as error:
                        request(url + "/jobs/" + hidden["id"], token)
                    self.assertEqual(error.exception.code, 403)
                    error.exception.close()
                    release_hidden.set()
                Path(out).mkdir()
                return {"session_id": "same-session", "usage": normalize({"input_tokens": 10, "output_tokens": 2}),
                        "status": "submitted", "seconds": .1, "provider": {}}

            with patch("metabench.native_run.cli_identity", return_value={"version": "fixture"}):
                try:
                    rows = run_matrix(suite, repo, environment, matrix, root / "run", driver_factory=Driver, agent_runner=native)
                finally:
                    release_hidden.set()
                self.assertEqual(calls, [None, "same-session"])
                self.assertTrue(all(r["score"] == 100 and r["public_vm_id"] == "public"
                                    and r["grading_vm_id"] == r["vm_id"] == "grading" for r in rows))
                self.assertEqual(sum(j["kind"] == "verify" for j in launched), 1, "unchanged patch still deduplicates")
                manifest = json.loads((root / "run/run.json").read_text())
                self.assertEqual(manifest["worker_pools"], {"public": ["public"], "grading": ["grading"], "separate": True})
                self.assertEqual(manifest["status"], "Complete")
                count = len(launched)
                run_matrix(suite, repo, environment, matrix, root / "run", resume=True, driver_factory=Driver, agent_runner=native)
                self.assertEqual(len(launched), count)
                self.assertEqual(len(calls), 2)
                changed = copy.deepcopy(matrix)
                changed["grading_vms"][0]["host"] = "other-grader"
                with self.assertRaisesRegex(ValueError, "configuration changed"):
                    run_matrix(suite, repo, environment, changed, root / "run", resume=True, driver_factory=Driver, agent_runner=native)
                class UnavailableGrader(Driver):
                    def stage_base(self, base):
                        if self.id == "grading":
                            raise RuntimeError("grading archive transfer failed")
                        return super().stage_base(base)
                with self.assertRaisesRegex(RuntimeError, "grading archive transfer failed"):
                    run_matrix(suite, repo, environment, matrix, root / "failed", driver_factory=UnavailableGrader, agent_runner=native)
                self.assertEqual(len(calls), 2, "both pools must be ready before model calls")

    def test_overlapping_inventory_and_invalid_grading_counts_fail_before_model_calls(self):
        matrix = {"agents": [{"provider": "claude", "model": "fixture", "reasoning": "high"}],
                  "vms": [{"id": "public", "host": "alice@same-vm"}],
                  "grading_vms": [{"id": "grading", "host": "bob@same-vm"}]}
        with tempfile.TemporaryDirectory() as directory:
            for change in ({}, {"grading_vm_count": 0}, {"grading_vm_count": True},
                           {"grading_vm_count": 2}, {"grading_vms": [{"id": "public", "host": "other"}]}):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    run_matrix({"tasks": []}, "unused", {}, {**matrix, **change}, Path(directory) / "out")
            self.assertFalse((Path(directory) / "out").exists())

    def test_wrong_kind_is_fenced_without_launch_for_pending_and_recovered_jobs(self):
        for already_running in (False, True):
            with self.subTest(already_running=already_running), tempfile.TemporaryDirectory() as directory:
                queue = TestQueue(Path(directory) / "queue.sqlite")
                identity = queue.enqueue("trial", "public", "verify", {})
                if already_running:
                    queue.claim("public", "old-owner")
                class Driver:
                    def __init__(self, *args): pass
                    def launch(self, job): raise AssertionError("misrouted job must never launch")
                pool = VMPool(queue, [{"id": "public"}], directory, driver_factory=Driver, kinds={"public"}).start()
                try:
                    with self.assertRaisesRegex(RuntimeError, "wrong worker pool"):
                        pool.await_job(identity)
                    self.assertEqual(queue.get(identity)["status"], "running")
                    if already_running:
                        self.assertEqual(queue.get(identity)["owner"], "old-owner")
                finally:
                    pool.close()

    def test_legacy_elastic_router_cannot_merge_separate_pools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "run.json").write_text(json.dumps({"worker_pools": {"separate": True}}))
            with self.assertRaisesRegex(ValueError, "separate public/grading pools"):
                attach(root, [])
            self.assertFalse((root / "queue.sqlite").exists())


if __name__ == "__main__":
    unittest.main()
