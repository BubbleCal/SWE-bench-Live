"""Prepare a reviewed pilot from Lance #8873, including tests embedded in Rust source.

No task data is downloaded automatically. Point at a local clone containing the pinned commit.
The emitted solution/test patches retain Lance's source file license headers in the checkout.
"""

import argparse
import difflib
import json
import subprocess
from pathlib import Path

REFERENCE = "f7b4c594284537d915c93bf5d7708e3bb61be600"
SOURCE = "rust/lance-linalg/src/distance/hamming.rs"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", repo, *args], text=True)


def patch(before, after):
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                      fromfile="a/" + SOURCE, tofile="b/" + SOURCE))


def create(repo):
    base = git(repo, "rev-parse", REFERENCE + "^").strip()
    before = git(repo, "show", base + ":" + SOURCE)
    after = git(repo, "show", REFERENCE + ":" + SOURCE)
    marker = "#[cfg(test)]"
    old_code, old_tests = before.split(marker, 1)
    new_code, new_tests = after.split(marker, 1)
    # Panic text is not part of the task's public contract; accept equivalent checks.
    new_tests = new_tests.replace('''#[should_panic(
        expected = "distance batch length must be divisible by dimension: batch=5, dimension=2"
    )]''', "#[should_panic]")
    solution = patch(before, new_code + marker + old_tests)
    tests = patch(before, old_code + marker + new_tests)
    task = {
        "instance_id": "lance-hamming-batch-layout-8873",
        "repo": "https://github.com/lance-format/lance",
        "base_commit": base,
        "reference_commit": REFERENCE,
        "problem_statement": (
            "Hamming batch distance calculations can silently ignore an incomplete trailing target vector "
            "in optimized builds, returning fewer distances than the supplied batch represents. "
            "Ensure hamming_distance_batch rejects malformed layouts in every build profile: "
            "dimension must be positive, query length must equal dimension, and the target byte count "
            "must be divisible by dimension. Preserve the public API and distances for valid inputs, "
            "and document the layout and panic contract."
        ),
        "patch": solution, "test_patch": tests,
        "weights": {"correctness": 80, "regression": 20},
        "checks": [
            {"id": "partial-vector", "dimension": "correctness", "critical": True,
             "command": "cargo test --locked --profile release-with-debug -p lance-linalg --lib test_hamming_distance_batch_rejects_partial_vector",
             "success_pattern": "test result: ok\\. 1 passed; 0 failed; 0 ignored",
             "failure_pattern": "test result: FAILED\\. 0 passed; 1 failed", "timeout": 1800},
            {"id": "valid-hamming", "dimension": "regression", "critical": True,
             "command": "cargo test --locked --profile release-with-debug -p lance-linalg --lib distance::hamming::tests::test_hamming_u64 -- --exact",
             "success_pattern": "test result: ok\\. 1 passed; 0 failed; 0 ignored",
             "failure_pattern": "test result: FAILED\\.", "timeout": 1800},
        ],
        "provenance": {"method": "reviewed-historical-pilot", "url": "https://github.com/lance-format/lance/pull/8873",
                       "test_adaptation": "Split Rust inline test module; remove implementation-specific panic text assertion.",
                       "limitations": "Pilot checks the historical trailing-vector regression and one existing valid-input test; not comprehensive layout-contract coverage."},
    }
    return task


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(create(args.repo), indent=2) + "\n")
