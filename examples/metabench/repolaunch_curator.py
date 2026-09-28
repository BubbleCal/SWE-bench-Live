"""Curation adapter using the existing RepoLaunch LiteLLM dependency.

Usage in an adapter argv: python repolaunch_curator.py --model PROVIDER/MODEL --reasoning high
Always review generated requirements and run metabench validate before freezing a task.
"""

import argparse
import contextlib
import json
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--reasoning", required=True)
    args = parser.parse_args()
    source = json.load(sys.stdin)
    instruction = source.pop("instruction")
    # Paths are operator metadata. A one-shot adapter uses the provided history;
    # a richer trusted curator can implement repository exploration separately.
    source.pop("repo_path", None)
    with contextlib.redirect_stdout(sys.stderr):
        import litellm
        options = {"model": args.model}
        if args.reasoning != "default":
            options["reasoning_effort"] = args.reasoning
        response = litellm.completion(messages=[{"role": "system", "content": instruction},
                                                {"role": "user", "content": json.dumps(source)}], **options)
    content = response.choices[0].message.content.strip()
    if content.startswith("```"):
        content = "\n".join(content.splitlines()[1:-1])
    print(json.dumps(json.loads(content)))


if __name__ == "__main__":
    main()
