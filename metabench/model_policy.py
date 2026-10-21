"""Resolve GPT benchmark settings from the selected native CLI's model catalog."""
import copy
import json
import subprocess

from .schema import digest

POLICY = "codex-gpt-max-context-standard-v1"
FAST_POLICY = "codex-gpt-max-context-fast-v1"
CATALOG_FIELDS = ("slug", "context_window", "max_context_window", "effective_context_window_percent")


def applies(spec):
    return spec.get("provider") == "codex" and spec.get("model", "").startswith("gpt-")


def fetch_catalog(command):
    # The native CLI resolves this through its current authenticated account.
    # Never substitute an API documentation limit or a hardcoded model table.
    try:
        result = subprocess.run([*command, "debug", "models"], capture_output=True,
                                text=True, timeout=60, check=True)
        data = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise RuntimeError("cannot refresh the native Codex model catalog") from error
    models = data.get("models") if isinstance(data, dict) else data
    if not isinstance(models, list):
        raise ValueError("native Codex model catalog must contain a model list")
    # Catalog entries can contain prompt templates. Only retain numeric policy
    # metadata; those templates are neither instructions nor report content.
    return {"models": [{key: model.get(key) for key in CATALOG_FIELDS}
                       for model in models if isinstance(model, dict)]}


def _policy(model, metadata, *, fast_mode=False):
    default = metadata.get("context_window")
    maximum = metadata.get("max_context_window")
    percent = metadata.get("effective_context_window_percent")
    if (metadata.get("slug") != model or type(default) is not int or default <= 0
            or type(maximum) is not int or maximum < default
            or type(percent) is not int or not 0 < percent <= 100):
        raise ValueError("GPT benchmark requires valid advertised max_context_window and effective-context metadata: " + model)
    effective = maximum * percent // 100
    if effective <= 0:
        raise ValueError("effective GPT context must be positive")
    entry = {key: metadata[key] for key in CATALOG_FIELDS}
    return {"policy_id": FAST_POLICY if fast_mode else POLICY, "model": model, "context_window": maximum,
            "effective_context_window": effective, "auto_compact_token_limit": effective,
            "service_tier": "fast" if fast_mode else "default", "fast_mode": fast_mode,
            "catalog_source": "codex debug models", "catalog_entry": entry,
            "catalog_entry_hash": digest(entry)}


def resolve_agents(agents, *, refresh=True):
    """Refresh once per CLI for new runs; validate frozen settings for resumes."""
    catalogs = {}
    resolved = []
    for original in agents:
        spec = copy.deepcopy(original)
        if not applies(spec):
            resolved.append(spec)
            continue
        fast_mode = spec.get("fast_mode", False)
        if type(fast_mode) is not bool:
            raise ValueError("GPT fast_mode must be an explicit boolean")
        frozen = spec.get("model_policy")
        if not refresh and frozen is not None:
            if not isinstance(frozen, dict) or not isinstance(frozen.get("catalog_entry"), dict):
                raise ValueError("invalid frozen GPT model policy")
            expected = _policy(spec["model"], frozen["catalog_entry"], fast_mode=fast_mode)
            if frozen != expected:
                raise ValueError("frozen GPT model policy changed")
        else:
            command = spec.get("command", ["codex"])
            if not isinstance(command, list) or not command or any(not isinstance(s, str) or not s for s in command):
                raise ValueError("native command must be a nonempty argv array")
            key = tuple(command)
            if key not in catalogs:
                catalogs[key] = fetch_catalog(command)
            matches = [m for m in catalogs[key]["models"] if m.get("slug") == spec["model"]]
            if len(matches) != 1:
                raise ValueError("GPT model missing or duplicated in native catalog: " + spec["model"])
            spec["model_policy"] = _policy(spec["model"], matches[0], fast_mode=fast_mode)
        resolved.append(spec)
    return resolved


def cli_overrides(spec):
    if not applies(spec):
        return []
    policy = resolve_agents([spec], refresh=False)[0]["model_policy"]
    return ["-c", "model_context_window=" + str(policy["context_window"]),
            "-c", "model_auto_compact_token_limit=" + str(policy["auto_compact_token_limit"]),
            "-c", "service_tier=" + json.dumps(policy["service_tier"]),
            "-c", "features.fast_mode=" + json.dumps(policy["fast_mode"])]
