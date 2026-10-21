"""Headless DeepSeek Harness commands and invocation-scoped event accounting."""
import json
import hashlib
from pathlib import Path

from . import usage
from .schema import write_json


def command(spec, session_id, mcp_path):
    patch = Path(spec['dsh_patch'])
    if spec.get('dsh_patch_sha256') and hashlib.sha256(patch.read_bytes()).hexdigest() != spec['dsh_patch_sha256']:
        raise ValueError('frozen DSH profile changed')
    selections = [entry['config'] for entry in json.loads(patch.read_text())
                  if entry.get('id') == 'agent-default-model']
    if len(selections) != 1 or selections[0].get('model') != spec['model'] or selections[0].get('reasoningEffort') != spec['reasoning']:
        raise ValueError('DSH profile must select the requested model and reasoning effort')
    server = json.loads(Path(mcp_path).read_text())['mcpServers']['bench']
    bridge = Path(mcp_path).with_suffix('.dsh.json')
    write_json(bridge, [{'insert': [{
        'id': 'metabench-mcp', 'name': '@deepseek-ai/dsh-mcp-client',
        'config': {'transport': 'stdio', 'serverName': 'bench',
                   'command': server['command'], 'args': server['args'],
                   'toolCallTimeoutMs': 86400000, 'failOnStartupError': True}}]}])
    argv = [*spec.get('command', ['dsh']), '--profile', 'headless',
            '--patch', str(patch), '--patch', str(bridge), '--json']
    if session_id:
        argv += ['--session-id', session_id]
    return argv + ['-']


def environment(spec, out):
    home = Path(spec['dsh_home']).resolve()
    return {'DSH_HOME': str(home),
            'DSH_AGENTS_HOME': str(Path(spec.get('dsh_agents_home', home/'agents')).resolve()),
            'METABENCH_DSH_REQUEST_AUDIT': str(Path(out).resolve()/'requests.jsonl')}


def parse_events(events, model, expected_session=None):
    sessions = [event['sessionId'] for event in events if event.get('type') == 'session']
    if len(sessions) != 1 or not sessions[0] or (expected_session and sessions[0] != expected_session):
        raise ValueError('DSH invocation did not identify the expected single session')
    steps = [event for event in events if event.get('type') == 'status' and event.get('phase') == 'step_end']
    identifiers = [(step.get('turn'), step.get('step')) for step in steps]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError('duplicate DSH step usage')
    total = None
    for step in steps:
        raw = step.get('usage')
        if raw is None:
            increment = usage.normalize({})
        else:
            # DSH/pi-ai excludes cache reads/writes from inputTokens and only
            # emits the cache fields when nonzero. Reasoning is folded into output.
            cached, written = raw.get('cacheReadTokens', 0), raw.get('cacheWriteTokens', 0)
            inputs = raw.get('inputTokens')
            increment = usage.normalize({
                'input_tokens': inputs + cached + written if inputs is not None else None,
                'cached_input_tokens': cached, 'cache_write_input_tokens': written,
                'output_tokens': raw.get('outputTokens'),
                'reasoning_output_tokens': raw.get('reasoningTokens')})
            if raw.get('totalTokens') is not None and increment['total_tokens'] != raw['totalTokens']:
                raise ValueError('DSH usage total disagrees with input, cache, and output counters')
        total = increment if total is None else usage.add(total, increment)
    terminals = [event for event in events if event.get('type') == 'status' and event.get('phase') == 'turn_end']
    finished = (bool(terminals) and terminals[-1].get('reason', {}).get('kind') == 'completed'
                and len([event for event in events if event.get('type') == 'final']) == 1
                and not any(event.get('type') == 'error' for event in events))
    return {'session_id': sessions[0], 'usage': total or usage.normalize({}),
            'status': 'submitted' if finished else 'agent_error',
            'provider': {'provider': 'dsh', 'requested_model': model,
                         'usage_scope': 'current_invocation_steps_including_retries',
                         'tool_calls': sum(event.get('type') == 'tool_call' for event in events)}}
