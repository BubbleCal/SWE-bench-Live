import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from metabench.model_policy import resolve_agents
from metabench.native_cli import command


CATALOG = {'models': [
    {'slug': 'gpt-example-a', 'context_window': 100, 'max_context_window': 1000,
     'effective_context_window_percent': 95},
    {'slug': 'gpt-example-b', 'context_window': 200, 'max_context_window': 2000,
     'effective_context_window_percent': 90},
]}


class ModelPolicyTest(unittest.TestCase):
    def agents(self):
        return [{'provider': 'codex', 'model': model, 'reasoning': effort}
                for model, effort in [('gpt-example-a', 'high'), ('gpt-example-a', 'xhigh'), ('gpt-example-b', 'high')]]

    def test_model_maximum_is_resolved_per_model_once_and_input_is_unchanged(self):
        original = self.agents()
        with patch('metabench.model_policy.fetch_catalog', return_value=CATALOG) as fetch:
            resolved = resolve_agents(original)
        fetch.assert_called_once_with(['codex'])
        self.assertEqual([a['model_policy']['context_window'] for a in resolved], [1000, 1000, 2000])
        self.assertEqual([a['model_policy']['auto_compact_token_limit'] for a in resolved], [950, 950, 1800])
        self.assertTrue(all(a['model_policy']['service_tier'] == 'default' and a['model_policy']['fast_mode'] is False for a in resolved))
        self.assertTrue(all('model_policy' not in a for a in original))

    def test_missing_or_invalid_maximum_cannot_silently_use_the_small_default(self):
        for maximum in (None, 0, -1, True, 99, '1000'):
            catalog = {'models': [{**CATALOG['models'][0], 'max_context_window': maximum}]}
            with self.subTest(maximum=maximum), patch('metabench.model_policy.fetch_catalog', return_value=catalog):
                with self.assertRaises(ValueError):
                    resolve_agents(self.agents()[:1])
        with patch('metabench.model_policy.fetch_catalog', return_value={'models': []}):
            with self.assertRaises(ValueError):
                resolve_agents(self.agents()[:1])

    def test_fresh_run_refreshes_catalog_but_frozen_resume_does_not(self):
        with patch('metabench.model_policy.fetch_catalog', return_value=CATALOG):
            initial = resolve_agents(self.agents()[:1])
        newer = {'models': [{**CATALOG['models'][0], 'max_context_window': 3000}]}
        with patch('metabench.model_policy.fetch_catalog', return_value=newer) as fetch:
            frozen = resolve_agents(initial, refresh=False)
            fetch.assert_not_called()
            current = resolve_agents(initial)
        self.assertEqual(frozen[0]['model_policy']['context_window'], 1000)
        self.assertEqual(current[0]['model_policy']['context_window'], 3000)

    def test_foreign_or_tampered_policy_is_rejected(self):
        with patch('metabench.model_policy.fetch_catalog', return_value=CATALOG):
            spec = resolve_agents(self.agents()[:1])[0]
        for changes in ({'context_window': 100}, {'service_tier': 'fast'}, {'fast_mode': True}, {'model': 'gpt-example-b'}):
            with self.subTest(changes=changes):
                bad = {**spec, 'model_policy': {**spec['model_policy'], **changes}}
                with self.assertRaises(ValueError):
                    resolve_agents([bad], refresh=False)

    def test_initial_and_resumed_cli_enforce_max_context_and_standard_after_extra_args(self):
        with patch('metabench.model_policy.fetch_catalog', return_value=CATALOG):
            spec = resolve_agents(self.agents()[:1])[0]
        spec['extra_args'] = ['-c', 'model_context_window=10', '-c', 'service_tier="fast"', '-c', 'features.fast_mode=true']
        with tempfile.TemporaryDirectory() as temp:
            mcp = Path(temp) / 'mcp.json'
            mcp.write_text(json.dumps({'mcpServers': {'bench': {'command': 'python', 'args': ['bridge.py']}}}))
            for session in (None, 'original-session'):
                args = command(spec, session, mcp)
                overrides = dict(args[i+1].split('=', 1) for i, flag in enumerate(args[:-1]) if flag == '-c')
                self.assertEqual(overrides['model_context_window'], '1000')
                self.assertEqual(overrides['model_auto_compact_token_limit'], '950')
                self.assertEqual(overrides['service_tier'], '"default"')
                self.assertEqual(overrides['features.fast_mode'], 'false')
                self.assertIn('--ignore-user-config', args)
                if session:
                    self.assertIn(session, args)

    def test_non_gpt_providers_do_not_fetch_or_override_model_settings(self):
        agents = [{'provider': 'claude', 'model': 'claude-opus-5-5', 'reasoning': 'high'},
                  {'provider': 'codex', 'model': 'control-model', 'reasoning': 'high'}]
        with patch('metabench.model_policy.fetch_catalog') as fetch:
            self.assertEqual(resolve_agents(agents), agents)
            fetch.assert_not_called()

    def test_requested_fast_mode_survives_initial_and_resumed_cli_overrides(self):
        agents = self.agents()
        agents[-1]['fast_mode'] = True
        with patch('metabench.model_policy.fetch_catalog', return_value=CATALOG):
            resolved = resolve_agents(agents)
        self.assertEqual([a['model_policy']['fast_mode'] for a in resolved], [False, False, True])
        self.assertEqual([a['model_policy']['service_tier'] for a in resolved], ['default', 'default', 'fast'])
        spec = resolved[-1]
        spec['extra_args'] = ['-c', 'service_tier="default"', '-c', 'features.fast_mode=false']
        with tempfile.TemporaryDirectory() as temp:
            mcp = Path(temp) / 'mcp.json'
            mcp.write_text(json.dumps({'mcpServers': {'bench': {'command': 'python', 'args': ['bridge.py']}}}))
            for session in (None, 'original-session'):
                args = command(spec, session, mcp)
                overrides = dict(args[i+1].split('=', 1) for i, flag in enumerate(args[:-1]) if flag == '-c')
                self.assertEqual(overrides['service_tier'], '"fast"')
                self.assertEqual(overrides['features.fast_mode'], 'true')
                self.assertEqual(overrides['model_context_window'], '2000')
                self.assertEqual(overrides['model_auto_compact_token_limit'], '1800')

    def test_fast_mode_is_frozen_and_cannot_be_changed_on_resume(self):
        agent = {**self.agents()[-1], 'fast_mode': True}
        with patch('metabench.model_policy.fetch_catalog', return_value=CATALOG):
            spec = resolve_agents([agent])[0]
        with patch('metabench.model_policy.fetch_catalog') as fetch:
            self.assertEqual(resolve_agents([spec], refresh=False), [spec])
            fetch.assert_not_called()
            with self.assertRaisesRegex(ValueError, 'frozen GPT model policy changed'):
                resolve_agents([{**spec, 'fast_mode': False}], refresh=False)
        for value in (None, 0, 1, 'true', 'false'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'explicit boolean'):
                resolve_agents([{**agent, 'fast_mode': value}])


if __name__ == '__main__':
    unittest.main()
