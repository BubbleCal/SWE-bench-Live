"""JSON model adapter using the fork's existing RepoLaunch provider dependencies.

Install launch/ as described in Development.md. Configure provider credentials in the
adapter's environment; credentials are not passed to task containers. No default model
or reasoning setting is chosen here.
"""

import contextlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "launch"))


def main():
    request = json.load(sys.stdin)
    # Import and provider diagnostics go to stderr, keeping stdout machine-readable.
    with contextlib.redirect_stdout(sys.stderr):
        import litellm
        options = {"model": request["model"]}
        if request["reasoning"] != "default":
            options["reasoning_effort"] = request["reasoning"]
        # Use RepoLaunch's existing provider dependency directly: its LLMProvider
        # preflight sends a separate hello call whose usage is not attributed.
        response = litellm.completion(messages=request["messages"], **options)
    text = response.choices[0].message.content.strip()
    if text.startswith("```"):
        text = "\n".join(text.splitlines()[1:-1])
    usage = response.usage
    print(json.dumps({"action": json.loads(text), "usage": {
        "input_tokens": getattr(usage, "prompt_tokens", None), "output_tokens": getattr(usage, "completion_tokens", None),
        "cost_usd": getattr(response, "_hidden_params", {}).get("response_cost")}}))


if __name__ == "__main__":
    main()
