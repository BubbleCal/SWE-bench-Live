"""One logical round, with bounded same-session capacity recovery."""
import hashlib
import json
import os
from pathlib import Path
import time

from . import usage
from .native_cli import parse_events, run_turn

CAPACITY_MESSAGE = "Selected model is at capacity. Please try a different model."
RECOVERY_POLICY = "same-session-capacity-v1"


def durable_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_events(path):
    records = []
    for line in Path(path).read_text().splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return records


def capacity_error(turn):
    return turn.get("status") == "agent_error" and any(
        e.get("type") in ("error", "turn.failed") and
        (e.get("message") == CAPACITY_MESSAGE or e.get("error", {}).get("message") == CAPACITY_MESSAGE)
        for e in read_events(turn["native_event_log"]))


def session_usage(session):
    paths = list((Path.home() / ".codex/sessions").glob("*/*/*/*" + session + "*.jsonl"))
    if len(paths) != 1:
        return None
    latest = None
    with paths[0].open() as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = record.get("payload", {})
            if record.get("type") == "event_msg" and payload.get("type") == "token_count" and payload.get("info"):
                latest = usage.normalize(payload["info"]["total_token_usage"])
    return latest


def run_round(spec, root, prompt, out, *, session_id=None, mcp_path, timeout=None,
              stop=None, previous_usage=None, queue_wait=None, queue_timeout=None,
              budget_mode="active", attempt_runner=run_turn, max_attempts=6, retry_delay=30):
    if spec["provider"] != "codex" or budget_mode != "active":
        return attempt_runner(spec, root, prompt, out, session_id=session_id, mcp_path=mcp_path,
                              timeout=timeout, stop=stop, previous_usage=previous_usage,
                              queue_wait=queue_wait, queue_timeout=queue_timeout, budget_mode=budget_mode)
    out = Path(out)
    if out.exists() and any(out.iterdir()) and not (out / "round.json").exists():
        raise RuntimeError("Existing native turn has no logical-round manifest; refusing to replace it")
    out.mkdir(parents=True, exist_ok=True)
    contract = {"policy": RECOVERY_POLICY, "model": spec["model"], "reasoning": spec["reasoning"],
                "model_policy": spec.get("model_policy"), "session_id": session_id,
                "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest(), "budget": timeout,
                "budget_mode": budget_mode, "max_attempts": max_attempts}
    manifest = out / "round.json"
    if manifest.exists():
        if json.loads(manifest.read_text()) != contract:
            raise ValueError("logical round configuration changed")
    else:
        durable_json(manifest, contract)
    if (out / "result.json").exists():
        return json.loads((out / "result.json").read_text())
    attempts = []
    for directory in sorted(out.glob("attempt-*")):
        if not directory.is_dir():
            continue
        result = directory / "result.json"
        if not result.exists():
            raise RuntimeError("Native invocation interrupted without a terminal receipt; inspect its durable clock and process before resuming: " + str(directory))
        attempts.append(json.loads(result.read_text()))
    current_session = attempts[-1].get("session_id") if attempts else session_id
    while not attempts or capacity_error(attempts[-1]):
        charged = sum(a["active_seconds" if budget_mode == "active" else "wall_seconds"] for a in attempts)
        remaining = None if timeout is None else timeout - charged
        if attempts and (len(attempts) >= max_attempts or (remaining is not None and remaining <= 0)):
            break
        if attempts and not current_session:
            break
        if stop is not None and stop.is_set():
            raise InterruptedError("controller stopping between native invocations")
        if attempts:
            delay = min(120, retry_delay * len(attempts))
            if stop is not None:
                if stop.wait(delay):
                    raise InterruptedError("controller stopping during capacity backoff")
            else:
                time.sleep(delay)
        invocation_prompt = prompt if not attempts else (
            "The provider interrupted this invocation with a capacity error. Continue the same benchmark round "
            "in the same worktree and session. This is not a new round. Do not seek hidden scoring feedback. "
            + (f"Only {remaining:.6f} seconds remain from this round's original active budget. " if remaining is not None else "")
            + "Use bench.run_tests for compilation and public tests.")
        observed_before = session_usage(current_session) if current_session else None
        turn = attempt_runner(spec, root, invocation_prompt, out / f"attempt-{len(attempts):02}",
                              session_id=current_session, mcp_path=mcp_path, timeout=remaining,
                              stop=stop, previous_usage=observed_before or previous_usage,
                              queue_wait=queue_wait, queue_timeout=queue_timeout, budget_mode=budget_mode)
        if current_session and turn.get("session_id") != current_session:
            raise ValueError("capacity continuation changed native session")
        current_session = turn.get("session_id")
        attempts.append(turn)
    if not attempts:
        raise RuntimeError("no native invocation was recorded")
    combined = [event for a in attempts for event in read_events(a["native_event_log"])]
    event_path = out / "events.jsonl"
    event_path.write_text("".join(json.dumps(e) + "\n" for e in combined))
    try:
        result = parse_events("codex", combined, spec["model"], session_id, previous_usage, require_test_tool=True)
    except ValueError:
        result = {**attempts[-1]}
    last = attempts[-1]
    active = sum(a["active_seconds"] for a in attempts)
    queued = sum(a["queue_wait_seconds"] for a in attempts)
    wall = last["started_at"] + last["wall_seconds"] - attempts[0]["started_at"]
    infrastructure = max(0.0, wall - active - queued)
    result.update(status=last["status"], session_id=current_session, seconds=wall, wall_seconds=wall,
                  active_seconds=active, queue_wait_seconds=queued, infrastructure_wait_seconds=infrastructure,
                  started_at=attempts[0]["started_at"], budget_mode=budget_mode, returncode=last["returncode"],
                  native_event_log=str(event_path), provider_attempts=attempts,
                  recovery_policy=RECOVERY_POLICY, model_policy=last.get("model_policy"))
    if capacity_error(last):
        result.update(status="infrastructure_error", error="provider capacity recovery exhausted bounded attempts or remaining budget")
    result.setdefault("provider", {})["requested_reasoning"] = spec["reasoning"]
    observed = session_usage(current_session) if current_session else None
    if observed is not None:
        result["provider"]["session_usage"] = observed
        result["observed_session_usage"] = observed
    if result["status"] != "submitted":
        result["observed_usage_lower_bound"] = observed or result["usage"]
        result["usage"] = usage.normalize({})
    durable_json(out / "result.json", result)
    return result
