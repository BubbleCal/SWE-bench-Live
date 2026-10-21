import json
import tempfile
import unittest
from pathlib import Path

from metabench import usage
from metabench.native_round import CAPACITY_MESSAGE, durable_json, run_round


class NativeRoundTest(unittest.TestCase):
    def runner(self, statuses, active, calls):
        def native(spec, root, prompt, out, **options):
            i = len(calls)
            calls.append((prompt, options))
            out.mkdir()
            status = statuses[i]
            events = [{'type': 'thread.started', 'thread_id': 'fixture-session'},
                      {'type': 'item.completed', 'item': {'type': 'mcp_tool_call', 'server': 'bench', 'tool': 'run_tests'}}]
            if status == 'submitted':
                events.append({'type': 'turn.completed', 'usage': {'input_tokens': 200, 'cached_input_tokens': 100, 'output_tokens': 20}})
            elif status == 'capacity':
                events.append({'type': 'turn.failed', 'error': {'message': CAPACITY_MESSAGE}})
            elif status == 'other_error':
                events.append({'type': 'turn.failed', 'error': {'message': 'unrelated failure'}})
            (out / 'events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events))
            result = {'session_id': 'fixture-session', 'status': 'agent_error' if status in ('capacity', 'other_error') else status,
                      'usage': usage.normalize({}), 'provider': {'requested_reasoning': 'high'},
                      'started_at': 100 + i * 20, 'wall_seconds': active[i] + 2,
                      'active_seconds': active[i], 'queue_wait_seconds': 2,
                      'budget_mode': 'active', 'returncode': 0 if status == 'submitted' else 1,
                      'native_event_log': str(out / 'events.jsonl')}
            durable_json(out / 'result.json', result)
            return result
        return native

    def run_case(self, statuses, active):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        spec = {'provider': 'codex', 'model': 'fixture', 'reasoning': 'high'}
        calls = []
        options = dict(timeout=10, mcp_path=root/'mcp', attempt_runner=self.runner(statuses, active, calls), retry_delay=0)
        result = run_round(spec, root, 'same round', root/'turn', **options)
        return root, spec, calls, options, result

    def test_capacity_retry_keeps_session_remaining_budget_raw_receipts_and_gap(self):
        root, spec, calls, options, result = self.run_case(['capacity', 'submitted'], [3, 4])
        self.assertEqual([c[1]['timeout'] for c in calls], [10, 7])
        self.assertEqual(calls[1][1]['session_id'], 'fixture-session')
        self.assertIn('7.000000 seconds remain', calls[1][0])
        self.assertEqual(result['active_seconds'], 7)
        self.assertEqual(result['queue_wait_seconds'], 4)
        self.assertEqual(result['infrastructure_wait_seconds'], 15)
        self.assertEqual(result['status'], 'submitted')
        self.assertEqual(result['usage']['total_tokens'], 220)
        self.assertEqual(len(result['provider_attempts']), 2)
        raw = (root/'turn/attempt-00/events.jsonl').read_bytes()
        resumed = run_round(spec, root, 'same round', root/'turn', **options)
        self.assertEqual(resumed, result)
        self.assertEqual(len(calls), 2)
        self.assertEqual((root/'turn/attempt-00/events.jsonl').read_bytes(), raw)

    def test_timeout_and_unrelated_error_are_never_retried(self):
        for status in ('round_timeout', 'other_error'):
            with self.subTest(status=status):
                _, _, calls, _, result = self.run_case([status], [10])
                self.assertEqual(len(calls), 1)
                self.assertEqual(result['status'], 'agent_error' if status == 'other_error' else status)

    def test_capacity_exhaustion_is_infrastructure_not_model_failure(self):
        _, _, calls, _, result = self.run_case(['capacity'], [10])
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['status'], 'infrastructure_error')

    def test_missing_terminal_receipt_refuses_duplicate_paid_invocation(self):
        root, spec, calls, options, _ = self.run_case(['submitted'], [3])
        (root/'turn/result.json').unlink()
        (root/'turn/attempt-00/result.json').unlink()
        with self.assertRaisesRegex(RuntimeError, 'durable clock'):
            run_round(spec, root, 'same round', root/'turn', **options)
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
