"""Run one complete native CLI conversation turn, without a model/tool loop."""
import json
import hashlib
import os
import signal
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import usage
from .schema import write_json


def cli_identity(spec):
    argv = spec.get("command", [spec["provider"]])
    if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or not a for a in argv):
        raise ValueError("native command must be a nonempty argv array")
    executable = shutil.which(argv[0])
    if executable is None:
        raise ValueError("native CLI executable not found: " + argv[0])
    version = subprocess.run([*argv, "--version"], capture_output=True, text=True, timeout=15, check=True)
    paths = {str(Path(executable).resolve()), *[str(Path(a).resolve()) for a in argv[1:] if Path(a).is_file()]}
    return {"command": [executable, *argv[1:]], "version": version.stdout.strip(),
            "file_hashes": {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in sorted(paths)}}


def claude_usage(raw):
    inputs = [raw.get(k) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
    return usage.normalize({
        "input_tokens": sum(inputs) if all(v is not None for v in inputs) else None,
        "cached_input_tokens": raw.get("cache_read_input_tokens"),
        "cache_write_input_tokens": raw.get("cache_creation_input_tokens"),
        "output_tokens": raw.get("output_tokens"),
        "reasoning_output_tokens": raw.get("output_tokens_details", {}).get("thinking_tokens"),
    })


def subtract_usage(current, previous):
    values = {}
    for key in usage.FIELDS:
        left, right = current.get(key), previous.get(key)
        if left is None or right is None:
            values[key] = None
        elif left < right:
            raise ValueError("native session usage counter decreased: " + key)
        else:
            values[key] = left - right
    return usage.normalize(values)


def parse_events(provider, events, model, expected_session=None, previous_usage=None, require_test_tool=False):
    session = None
    increment = usage.normalize({})
    metadata = {"provider": provider, "requested_model": model}
    completed = False
    if provider == "codex":
        starts = [e for e in events if e.get("type") == "thread.started"]
        if starts:
            session = starts[-1]["thread_id"]
        turns = [e for e in events if e.get("type") == "turn.completed"]
        if len(turns) == 1:
            reported = usage.normalize(turns[0].get("usage", {}))
            metadata["session_usage"] = reported
            metadata["usage_scope"] = "session_cumulative_delta"
            increment = reported if expected_session is None else (
                subtract_usage(reported, previous_usage) if previous_usage is not None else usage.normalize({}))
            completed = True
        metadata["tool_calls"] = sum(e.get("type") == "item.completed" and
            e.get("item", {}).get("type") not in ("agent_message", "reasoning", "error") for e in events)
    elif provider == "claude":
        init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
        session = init.get("session_id")
        metadata["cli_version"] = init.get("claude_code_version")
        metadata["model_reported"] = init.get("model")
        if init.get("model") and init["model"] != model:
            raise ValueError("native CLI used a different model")
        if init.get("mcp_server_errors") or any(s.get("status") == "failed" for s in init.get("mcp_servers", [])):
            raise ValueError("native CLI failed to load its VM test tool")
        if require_test_tool and not any(s.get("name") == "bench" and s.get("status") in ("connected", "pending") for s in init.get("mcp_servers", [])):
            raise ValueError("native CLI did not expose the configured VM test tool")
        messages = {}
        for event in events:
            if event.get("type") != "assistant":
                continue
            message = event.get("message", {})
            if message.get("model") not in (None, model):
                raise ValueError("native CLI response came from a different model")
            if message.get("id"):
                # Several content blocks can describe the same provider message.
                previous = messages.get(message["id"])
                if previous is None or message.get("usage", {}).get("output_tokens", 0) >= previous.get("usage", {}).get("output_tokens", 0):
                    messages[message["id"]] = message
        terminal = [e for e in events if e.get("type") == "result"]
        if len(terminal) == 1:
            result = terminal[0]
            session = result.get("session_id", session)
            completed = not result.get("is_error", False) and result.get("subtype") == "success"
            raw = result.get("usage", {})
            # result.usage covers this print invocation. modelUsage and cost
            # cover the conversation, while assistant usage contains incomplete
            # streaming output counters. Neither is a substitute for result.usage.
            increment = claude_usage(raw)
            metadata["usage_scope"] = "current_invocation_result"
            metadata["conversation_model_usage"] = result.get("modelUsage", {})
            metadata["estimated_conversation_api_cost_usd"] = result.get("total_cost_usd")
            metadata["native_turns"] = result.get("num_turns")
        metadata["provider_messages"] = len(messages)
        metadata["tool_calls"] = sum(b.get("type") == "tool_use" for e in events if e.get("type") == "assistant"
                                     for b in e.get("message", {}).get("content", []))
    else:
        raise ValueError("provider must be codex or claude")
    if expected_session and session and session != expected_session:
        raise ValueError("native CLI resumed the wrong session")
    return {"session_id": session or expected_session, "usage": increment,
            "status": "submitted" if completed else "agent_error", "provider": metadata}


def command(spec, session_id, mcp_path):
    provider = spec["provider"]
    executable = spec.get("command", [provider])
    if not isinstance(executable, list) or not executable:
        raise ValueError("native command must be a nonempty argv array")
    model, effort = spec["model"], spec["reasoning"]
    if provider == "codex":
        argv = [*executable, "exec"] + (["resume"] if session_id else [])
        argv += ["--json", "--ignore-user-config", "-m", model,
                 "-c", "model_reasoning_effort=" + json.dumps(effort),
                 "-c", 'sandbox_mode="workspace-write"', "-c", 'approval_policy="never"',
                 "-c", "agents.enabled=false", "-c", 'web_search="disabled"']
        server = json.loads(Path(mcp_path).read_text())["mcpServers"]["bench"]
        for key in ("command", "args"):
            argv += ["-c", "mcp_servers.bench." + key + "=" + json.dumps(server[key])]
        argv += ["-c", "mcp_servers.bench.tool_timeout_sec=86400"]
        argv += ["-c", "mcp_servers.bench.required=true",
                 "-c", 'mcp_servers.bench.enabled_tools=["run_tests"]',
                 "-c", 'mcp_servers.bench.tools.run_tests.approval_mode="approve"']
        argv += list(spec.get("extra_args", []))
        return argv + ([session_id] if session_id else []) + ["-"]
    if provider == "claude":
        argv = [*executable, "-p", "--output-format", "stream-json", "--verbose",
                "--model", model, "--effort", effort, "--restricted", "--setting-sources", "",
                "--tools", "Bash,Read,Edit,Write,Glob,Grep,NotebookEdit",
                "--permission-mode", "acceptEdits", "--permission-prompts", "none",
                "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash,mcp__bench__run_tests",
                "--disallowedTools", "Agent", "--strict-mcp-config", "--mcp-config", str(mcp_path),
                "--no-chrome", "--prompt-suggestions", "false"]
        argv += ["--resume", session_id] if session_id else ["--session-id", str(uuid.uuid4())]
        return argv + list(spec.get("extra_args", []))
    raise ValueError("provider must be codex or claude")


def run_turn(spec, root, prompt, out, *, session_id=None, mcp_path, timeout=None, stop=None,
             previous_usage=None, queue_wait=None, queue_timeout=None, budget_mode="active"):
    if budget_mode not in ("active", "wall"):
        raise ValueError("budget_mode must be active or wall")
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    argv = command(spec, session_id, mcp_path)
    write_json(out / "invocation.json", {"argv": argv, "cwd": str(root), "session_id": session_id})
    (out / "prompt.txt").write_text(prompt)
    started = time.monotonic()
    started_at = time.time()
    timed_out = False
    interrupted = False
    infrastructure_error = None
    clock = {"wall_seconds": 0.0, "queue_wait_seconds": 0.0, "active_seconds": 0.0}
    def timing():
        wall = time.monotonic() - started
        waiting = min(wall, max(0, queue_wait(started_at, started_at + wall))) if queue_wait else 0.0
        return {"wall_seconds": wall, "queue_wait_seconds": waiting, "active_seconds": wall - waiting}
    environment = {k: v for k, v in os.environ.items() if not k.startswith("CODEX_") or k == "CODEX_HOME"}
    if spec["provider"] == "claude":
        environment["CLAUDE_CODE_EFFORT_LEVEL"] = spec["reasoning"]
    with (out / "events.jsonl").open("w") as stdout, (out / "stderr.txt").open("w") as stderr:
        process = subprocess.Popen(argv, cwd=root, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                   text=True, env=environment, start_new_session=True)
        try:
            process.stdin.write(prompt)
            process.stdin.close()
            while process.poll() is None:
                if stop is not None and stop.is_set():
                    raise KeyboardInterrupt
                clock = timing()
                if queue_timeout is not None and clock["queue_wait_seconds"] >= queue_timeout:
                    raise RuntimeError("public test infrastructure wait limit exceeded")
                charged = clock["active_seconds"] if budget_mode == "active" else clock["wall_seconds"]
                if timeout is not None and charged >= timeout:
                    raise subprocess.TimeoutExpired(argv, timeout)
                try:
                    process.wait(timeout=.2)
                except subprocess.TimeoutExpired:
                    pass
        except (Exception, KeyboardInterrupt) as error:
            interrupted = isinstance(error, KeyboardInterrupt)
            timed_out = isinstance(error, subprocess.TimeoutExpired)
            infrastructure_error = str(error) if not interrupted and not timed_out else None
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    events = []
    for line in (out / "events.jsonl").read_text().splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # A truncated terminal record never becomes a completed turn.
    try:
        result = parse_events(spec["provider"], events, spec["model"], session_id, previous_usage, require_test_tool=True)
    except ValueError as error:
        result = {"session_id": session_id, "usage": usage.normalize({}), "status": "agent_error",
                  "provider": {"provider": spec["provider"], "validation_error": str(error)}}
    try:
        measured = timing()
    except Exception as error:
        # Never leave a paid native process running when the timing store fails.
        # Preserve the last observed wait rather than inventing an eligible score.
        wall = time.monotonic() - started
        measured = {"wall_seconds": wall, "queue_wait_seconds": clock["queue_wait_seconds"],
                    "active_seconds": wall - clock["queue_wait_seconds"]}
        infrastructure_error = str(error)
    result.update(seconds=measured["wall_seconds"], **measured, started_at=started_at,
                  budget_mode=budget_mode, returncode=process.returncode,
                  native_event_log=str(out / "events.jsonl"))
    result["provider"]["requested_reasoning"] = spec["reasoning"]
    if infrastructure_error:
        result.update(status="infrastructure_error", error=infrastructure_error)
    elif timed_out:
        result["status"] = "round_timeout"
    elif interrupted:
        result["status"] = "interrupted"
    elif process.returncode:
        result["status"] = "agent_error"
    if result["status"] != "submitted":
        result["observed_usage_lower_bound"] = result["usage"]
        result["usage"] = usage.normalize({})
    write_json(out / "result.json", result)
    return result
