import json
import hashlib
from pathlib import Path
import tempfile
import unittest

from metabench.native_cli import command, parse_events


class DshCliTest(unittest.TestCase):
    def test_profile_selection_and_strict_mcp_are_preserved_on_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patch = root/'profile.json'
            patch.write_text(json.dumps([{'id': 'agent-default-model', 'config': {
                'provider': 'fireworks-ai', 'model': 'chosen-model', 'reasoningEffort': 'max'}}]))
            mcp = root/'mcp.json'
            mcp.write_text(json.dumps({'mcpServers': {'bench': {
                'command': '/python', 'args': ['bridge.py', '--token-file', '/private/capability']}}}))
            spec = {'provider': 'dsh', 'model': 'chosen-model', 'reasoning': 'max',
                    'command': ['/native/dsh'], 'dsh_patch': str(patch)}
            first, resumed = command(spec, None, mcp), command(spec, 'session-exact', mcp)
            self.assertNotIn('--session-id', first)
            self.assertEqual(resumed[resumed.index('--session-id')+1], 'session-exact')
            bridge = json.loads(mcp.with_suffix('.dsh.json').read_text())[0]['insert'][0]['config']
            self.assertTrue(bridge['failOnStartupError'])
            self.assertEqual(bridge['args'], ['bridge.py', '--token-file', '/private/capability'])
            with self.assertRaisesRegex(ValueError, 'requested model and reasoning'):
                command({**spec, 'reasoning': 'xhigh'}, None, mcp)
            pinned = {**spec, 'dsh_patch_sha256': hashlib.sha256(patch.read_bytes()).hexdigest()}
            command(pinned, None, mcp)
            patch.write_text(patch.read_text()+'\n')
            with self.assertRaisesRegex(ValueError, 'frozen DSH profile changed'):
                command(pinned, None, mcp)

    def test_cache_tokens_are_included_once_and_resume_usage_is_incremental(self):
        events = [{'type': 'session', 'sessionId': 'specific'},
                  {'type': 'status', 'phase': 'step_end', 'turn': 2, 'step': 1,
                   'usage': {'inputTokens': 10, 'cacheReadTokens': 30, 'outputTokens': 5, 'totalTokens': 45}},
                  {'type': 'tool_call', 'tool': 'mcp__bench__run_tests'},
                  {'type': 'status', 'phase': 'step_end', 'turn': 2, 'step': 2,
                   'usage': {'inputTokens': 7, 'cacheReadTokens': 4, 'outputTokens': 3, 'totalTokens': 14}},
                  {'type': 'status', 'phase': 'turn_end', 'reason': {'kind': 'completed'}},
                  {'type': 'final', 'text': 'done'}]
        result = parse_events('dsh', events, 'm', 'specific', {'total_tokens': 1000})
        self.assertEqual(result['status'], 'submitted')
        self.assertEqual(result['usage']['input_tokens'], 51)
        self.assertEqual(result['usage']['cached_input_tokens'], 34)
        self.assertEqual(result['usage']['total_tokens'], 59)
        self.assertEqual(result['provider']['tool_calls'], 1)
        self.assertIsNone(result['usage']['reasoning_output_tokens'])
        with self.assertRaises(ValueError):
            parse_events('dsh', events, 'm', 'different')
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            parse_events('dsh', events+[events[1]], 'm')

    def test_partial_turn_and_missing_usage_do_not_become_success_or_zero(self):
        events = [{'type': 'session', 'sessionId': 'specific'},
                  {'type': 'status', 'phase': 'step_end', 'turn': 1, 'step': 1},
                  {'type': 'final', 'text': 'partial'}]
        result = parse_events('dsh', events, 'm')
        self.assertEqual(result['status'], 'agent_error')
        self.assertIsNone(result['usage']['total_tokens'])
        result = parse_events('dsh', events+[{'type': 'status', 'phase': 'turn_end',
                                            'reason': {'kind': 'error'}}], 'm')
        self.assertEqual(result['status'], 'agent_error')


if __name__ == '__main__':
    unittest.main()
