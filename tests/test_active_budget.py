import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from metabench.native_cli import run_turn
from metabench.queue import TestQueue
from metabench.vm_pool import SSHVM


class ActiveBudgetTest(unittest.TestCase):
    def test_remote_lock_wait_is_not_reported_as_test_execution(self):
        class Stop:
            def is_set(self): return False
            def wait(self, duration): pass
        driver = object.__new__(SSHVM)
        states = iter([
            {'state': 'running', 'observed_at': 1000, 'execution_started_at': None},
            {'state': 'running', 'observed_at': 1010, 'execution_started_at': 1005},
            {'state': 'completed', 'observed_at': 1012, 'execution_started_at': 1005,
             'result': {'execution': {'returncode': 0}}},
        ])
        driver.status = lambda identity: next(states)
        ready = []
        with patch('metabench.vm_pool.time.time', return_value=200):
            result = driver.wait('job', Stop(), on_started=ready.append)
        self.assertEqual(ready, [195])  # Different remote epoch; preserve duration.
        self.assertEqual(result['execution']['returncode'], 0)

    def test_wait_union_clips_rounds_excludes_hidden_and_stops_at_vm_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = TestQueue(Path(temp) / 'queue.sqlite')
            for trial, kind, created, claimed, ready in [
                ('one', 'public', 5, 11, 14),
                ('one', 'public', 12, 15, 16),
                ('one', 'public', 18, None, None),
                ('one', 'verify', 1, None, None),
                ('other', 'public', 1, None, None),
            ]:
                job = queue.enqueue(trial, 'vm', kind, {})
                with queue.connect() as db:
                    db.execute('UPDATE jobs SET created=?,started=?,resource_started=?,finished=? WHERE id=?',
                               (created, claimed, ready, ready, job))
            # 10..16 plus 18..20. The second wait overlaps; the first began
            # in the preceding round; claim at 11 is not lock acquisition at 14.
            self.assertEqual(queue.queued_seconds('one', 10, 20), 8)
            self.assertEqual(queue.queued_seconds('one', 16, 18), 0)
            self.assertEqual(queue.queued_seconds('one', 20, 23), 3)
            with queue.connect() as db:
                db.execute("UPDATE jobs SET finished=19 WHERE trial_id='one' AND kind='public' AND created=5")
            # Own execution 14..19 overlaps 3 seconds of those queued intervals.
            self.assertEqual(queue.queued_seconds('one', 10, 20), 5)

    def test_queued_native_turn_can_exceed_wall_budget_but_execution_cannot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / 'native.py'
            script.write_text('import json,sys,time\n'
                              'sys.stdin.read()\n'
                              'print(json.dumps({"type":"thread.started","thread_id":"test"}),flush=True)\n'
                              'time.sleep(1.2)\n'
                              'print(json.dumps({"type":"turn.completed","usage":{}}),flush=True)\n')
            spec = {'provider': 'codex', 'model': 'test', 'reasoning': 'high'}
            with patch('metabench.native_cli.command', return_value=[sys.executable, str(script)]):
                result = run_turn(spec, root, 'task', root / 'queued', mcp_path=root/'unused',
                                  timeout=.7, queue_wait=lambda start, end: min(.9, end-start))
                self.assertEqual(result['status'], 'submitted')
                self.assertGreater(result['wall_seconds'], .7)
                self.assertLess(result['active_seconds'], .7)
                self.assertAlmostEqual(result['wall_seconds'], result['active_seconds'] + result['queue_wait_seconds'])
                result = run_turn(spec, root, 'task', root / 'executing', mcp_path=root/'unused',
                                  timeout=.3, queue_wait=lambda start, end: 0)
                self.assertEqual(result['status'], 'round_timeout')
                self.assertEqual(result['queue_wait_seconds'], 0)
                result = run_turn(spec, root, 'task', root / 'infra', mcp_path=root/'unused',
                                  timeout=10, queue_wait=lambda start, end: end-start, queue_timeout=.3)
                self.assertEqual(result['status'], 'infrastructure_error')
                self.assertNotEqual(result['status'], 'round_timeout')
                def broken_clock(start, end):
                    raise ValueError('timing store unavailable')
                result = run_turn(spec, root, 'task', root / 'broken-clock', mcp_path=root/'unused',
                                  timeout=10, queue_wait=broken_clock)
                self.assertEqual(result['status'], 'infrastructure_error')
                self.assertIsNotNone(result['returncode'])


if __name__ == '__main__':
    unittest.main()
