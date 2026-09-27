import importlib.util
import sys
import unittest
from pathlib import Path

from metabench.usage import normalize

EXAMPLES = Path(__file__).resolve().parents[1] / "examples/metabench"
sys.path.insert(0, str(EXAMPLES))
spec = importlib.util.spec_from_file_location("claude_model", EXAMPLES / "claude_model.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)
sys.path.pop(0)


def response():
    model = "claude-opus-5-5"
    return [{"type": "system", "subtype": "init", "model": model, "tools": ["StructuredOutput"], "mcp_servers": [],
             "plugins": [{"name": "agents-md", "path": "builtin", "source": "agents-md@builtin"}]},
            {"type": "assistant", "message": {"id": "msg-one", "model": model, "content": [{"type": "tool_use", "name": "StructuredOutput"}]}},
            {"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
             "modelUsage": {model: {}}, "structured_output": {"command": "cargo test", "submit": False},
             "usage": {"input_tokens": 10, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20,
                       "output_tokens": 30, "output_tokens_details": {"thinking_tokens": 25},
                       "iterations": [{"type": "message"}]}}]


class ClaudeAdapterTest(unittest.TestCase):
    def test_cache_read_and_write_are_added_once_to_input(self):
        action, usage, _ = adapter.parse_response(response(), "claude-opus-5-5")
        self.assertEqual(action, {"command": "cargo test"})
        normalized = normalize(usage)
        self.assertEqual(normalized["input_tokens"], 130)
        self.assertEqual(normalized["total_tokens"], 160)
        self.assertEqual(normalized["cached_input_tokens"], 100)
        self.assertEqual(normalized["cache_write_input_tokens"], 20)
        self.assertEqual(normalized["reasoning_output_tokens"], 25)
        self.assertIsNone(normalized["cost_usd"])

    def test_unreported_token_details_remain_unknown(self):
        value = adapter.normalized_usage({"input_tokens": 10, "output_tokens": 30})
        self.assertIsNone(value["input_tokens"])
        self.assertIsNone(value["reasoning_output_tokens"])
        with self.assertRaises(ValueError):
            adapter.normalized_usage({"input_tokens": -1})

    def test_account_hold_is_a_provider_error_not_a_zero_score(self):
        event = {"type": "result", "subtype": "success", "is_error": True,
                 "terminal_reason": "api_error", "result": "Your account is on hold and can't use Claude Code."}
        with self.assertRaisesRegex(RuntimeError, "account is on hold"):
            adapter.parse_response([event], "claude-opus-5-5")

    def test_native_tools_fallback_and_extra_turns_are_rejected(self):
        variants = []
        native = response(); native[0]["tools"].append("Bash"); variants.append(native)
        fallback = response(); fallback[1]["message"]["model"] = "another-model"; variants.append(fallback)
        turns = response(); turns[-1]["usage"]["iterations"].append({"type": "message"}); variants.append(turns)
        action = response(); action[1]["message"]["content"][0]["name"] = "Read"; variants.append(action)
        plugin = response(); plugin[0]["plugins"].append({"path": "/custom", "source": "user-plugin"}); variants.append(plugin)
        for events in variants:
            with self.assertRaises(RuntimeError):
                adapter.parse_response(events, "claude-opus-5-5")

    def test_requested_effort_and_native_tool_limits_are_explicit(self):
        for effort in ("high", "xhigh", "max"):
            args = adapter.invocation("claude", "claude-opus-5-5", effort)
            self.assertEqual(args[args.index("--effort") + 1], effort)
            self.assertEqual(args[args.index("--tools") + 1], "")
            self.assertEqual(args[args.index("--max-turns") + 1], "1")
            self.assertIn("--safe-mode", args)
            self.assertNotIn("--fallback-model", args)


if __name__ == "__main__":
    unittest.main()
