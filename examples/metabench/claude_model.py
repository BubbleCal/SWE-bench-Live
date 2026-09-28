"""Use the user's authenticated Claude Code CLI as a tool-disabled JSON adapter.

The controller executes returned shell commands in its own Docker workspace.
The CLI keeps local subscription authentication; no credential is copied to Docker.
"""
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from codex_model import INSTRUCTIONS, SCHEMA


def normalized_usage(raw):
    # Anthropic's input_tokens excludes BOTH cache reads and writes, unlike the
    # benchmark's inclusive input counter. Preserve null for any absent counter.
    parts = [raw.get(k) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
    for value in [*parts, raw.get("output_tokens")]:
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError("invalid Claude token counter")
    detail = raw.get("output_tokens_details") or {}
    return {"input_tokens": sum(parts) if all(v is not None for v in parts) else None,
            "cached_input_tokens": raw.get("cache_read_input_tokens"),
            "cache_write_input_tokens": raw.get("cache_creation_input_tokens"),
            "output_tokens": raw.get("output_tokens"),
            "reasoning_output_tokens": detail.get("thinking_tokens"),
            # CLI total_cost_usd is an API-price estimate, not a subscription bill.
            "cost_usd": None}


def parse_response(events, model):
    results = [e for e in events if e.get("type") == "result"]
    if len(results) != 1:
        raise RuntimeError("expected one Claude CLI result")
    result = results[0]
    format_limit = (result.get("subtype") == "error_max_turns"
                    and result.get("terminal_reason") == "max_turns"
                    and result.get("stop_reason") == "end_turn")
    if (result.get("is_error") and not format_limit) or result.get("terminal_reason") == "api_error":
        raise RuntimeError("Claude CLI: " + str(result.get("result") or result.get("errors") or result.get("subtype")))
    if result.get("subtype") != "success" and not format_limit:
        raise RuntimeError("expected a successful Claude decision")
    # StructuredOutput adds a local formatting/tool turn to CLI num_turns. The
    # provider iteration ledger, not that counter, identifies model requests.
    iterations = result.get("usage", {}).get("iterations")
    if not isinstance(iterations, list) or len(iterations) != 1 or iterations[0].get("type") != "message":
        raise RuntimeError("expected one provider request; no hidden retries or continuations")
    init = [e for e in events if e.get("type") == "system" and e.get("subtype") == "init"]
    if len(init) != 1 or init[0].get("model") != model:
        raise RuntimeError("Claude CLI resolved an unexpected model")
    if set(init[0].get("tools", [])) - {"StructuredOutput"} or init[0].get("mcp_servers"):
        raise RuntimeError("native CLI tools or MCP servers invalidate the controlled adapter")
    builtin_plugins = {"agents-md@builtin", "telemetry@builtin"}
    if init[0].get("skills") or any(p.get("path") != "builtin" or p.get("source") not in builtin_plugins
                                   for p in init[0].get("plugins", [])):
        raise RuntimeError("CLI customizations invalidate the controlled adapter")
    if result.get("subagent_stats", {}).get("spawned", 0):
        raise RuntimeError("native subagent activity invalidates the controlled adapter")
    messages = [e.get("message", {}) for e in events if e.get("type") == "assistant"]
    if not messages or len({m.get("id") for m in messages}) != 1 or not messages[0].get("id"):
        raise RuntimeError("expected one provider message identity")
    for message in messages:
        if message.get("model") != model:
            raise RuntimeError("model fallback or synthetic output is not a benchmark response")
        for block in message.get("content", []):
            kind = block.get("type")
            if kind == "tool_use" and block.get("name") == "StructuredOutput":
                continue
            if kind not in ("text", "thinking", "redacted_thinking"):
                raise RuntimeError("unexpected native CLI action")
    models = result.get("modelUsage", {})
    if set(models) != {model}:
        raise RuntimeError("expected usage from only the requested model")
    output = result.get("structured_output")
    source = "structured_output"
    if format_limit:
        blocks = [block for message in messages for block in message.get("content", [])]
        text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text").strip()
        if any(block.get("type") == "tool_use" for block in blocks) or not text:
            raise RuntimeError("turn limit without a completed native text response")
        try:
            output = json.loads(text)
            source = "json_text"
        except json.JSONDecodeError:
            # A native final answer ends the agent turn: submit the actual
            # workspace, never extract/execute commands from narrative text.
            output = {"command": None, "submit": True}
            source = "native_final_text"
    if isinstance(output, dict) and set(output) == {"submit"} and output["submit"] is True:
        output = {"command": None, "submit": True}
    elif isinstance(output, dict) and set(output) == {"command"} and isinstance(output["command"], str):
        output = {"command": output["command"], "submit": False}
    if not isinstance(output, dict) or set(output) != {"command", "submit"}:
        raise RuntimeError("missing structured decision")
    if output["submit"] is True and output["command"] is None:
        action = {"submit": True}
    elif output["submit"] is False and isinstance(output["command"], str):
        action = {"command": output["command"]}
    else:
        raise RuntimeError("decision must contain exactly one command or submission")
    return action, normalized_usage(result.get("usage", {})), {**result, "decision_source": source}


def invocation(binary, model, effort):
    if effort not in ("low", "medium", "high", "xhigh", "max"):
        raise ValueError("unsupported requested Claude effort")
    return [binary, "-p", "--model", model, "--effort", effort,
            "--output-format", "stream-json", "--verbose", "--safe-mode",
            "--setting-sources", "", "--tools", "", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands",
            "--no-session-persistence", "--no-chrome", "--prompt-suggestions", "false",
            "--permission-mode", "dontAsk", "--permission-prompts", "none", "--max-turns", "1",
            "--system-prompt", INSTRUCTIONS, "--json-schema", json.dumps(SCHEMA)]


def main():
    request = json.load(sys.stdin)
    command = invocation(os.environ.get("METABENCH_CLAUDE", "claude"), request["model"], request["reasoning"])
    environment = {k: v for k, v in os.environ.items() if not k.startswith("CODEX_")}
    # An explicit setting has higher precedence than inherited CLI defaults.
    environment["CLAUDE_CODE_EFFORT_LEVEL"] = request["reasoning"]
    with tempfile.TemporaryDirectory(prefix="metabench-claude-") as temp:
        completed = subprocess.run(command, input=json.dumps(request["messages"]), text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=temp, env=environment)
    events = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    log = None
    if os.environ.get("METABENCH_PROVIDER_LOG_DIR"):
        directory = Path(os.environ["METABENCH_PROVIDER_LOG_DIR"])
        directory.mkdir(parents=True, exist_ok=True)
        log = directory / (uuid.uuid4().hex + ".json")
        log.write_text(json.dumps({"model": request["model"], "reasoning": request["reasoning"],
                                   "events": events, "stderr": completed.stderr, "returncode": completed.returncode}, indent=2))
    # Prefer the provider's explicit failure over a generic process exit message.
    action, usage, result = parse_response(events, request["model"])
    if completed.returncode and result["decision_source"] not in ("native_final_text", "json_text"):
        raise RuntimeError("Claude CLI exited unsuccessfully: " + completed.stderr[-2000:])
    print(json.dumps({"action": action, "usage": usage,
                      "provider": {"kind": "claude-code-cli", "event_log": str(log) if log else None,
                                   "requested_effort": request["reasoning"], "num_turns": result["num_turns"],
                                   "provider_requests": 1,
                                   "decision_source": result["decision_source"],
                                   "cli_is_error": result.get("is_error"),
                                   "cli_version": next(e["claude_code_version"] for e in events if e.get("type") == "system" and e.get("subtype") == "init"),
                                   "raw_usage": result.get("usage"), "model_usage": result.get("modelUsage"),
                                   "estimated_api_cost_usd": result.get("total_cost_usd")}}))


if __name__ == "__main__":
    main()
