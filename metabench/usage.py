"""Token counters with explicit subset semantics and missing-data propagation."""

import math

FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "cost_usd")


def normalize(value):
    if not isinstance(value, dict):
        raise ValueError("model usage must be an object")
    result = {key: value.get(key) for key in FIELDS}
    for key, item in result.items():
        if item is not None and (isinstance(item, bool) or not isinstance(item, (int, float))
                                 or not math.isfinite(item) or item < 0):
            raise ValueError(f"invalid usage counter: {key}")
        if item is not None and key != "cost_usd" and not isinstance(item, int):
            raise ValueError(f"token counter must be an integer: {key}")
    for subset, parent in (("cached_input_tokens", "input_tokens"), ("cache_write_input_tokens", "input_tokens"), ("reasoning_output_tokens", "output_tokens")):
        if result[subset] is not None and result[parent] is not None and result[subset] > result[parent]:
            raise ValueError(f"{subset} cannot exceed {parent}")
    return derived(result)


def derived(value):
    value = {key: value.get(key) for key in FIELDS}
    value["total_tokens"] = value["input_tokens"] + value["output_tokens"] if value["input_tokens"] is not None and value["output_tokens"] is not None else None
    value["uncached_input_tokens"] = value["input_tokens"] - value["cached_input_tokens"] if value["input_tokens"] is not None and value["cached_input_tokens"] is not None else None
    value["non_reasoning_output_tokens"] = value["output_tokens"] - value["reasoning_output_tokens"] if value["output_tokens"] is not None and value["reasoning_output_tokens"] is not None else None
    return value


def empty():
    return derived({key: 0 for key in FIELDS})


def add(left, right):
    return derived({key: left[key] + right[key] if left.get(key) is not None and right.get(key) is not None else None for key in FIELDS})
