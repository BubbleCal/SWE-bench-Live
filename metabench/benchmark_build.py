"""Stable benchmark source trees and verified, evaluator-only build receipts."""
import hashlib
import json
import math
import os
from pathlib import PurePosixPath
import re
import shlex
import subprocess
import time

from .schema import digest

CACHE_POLICY = "verified-benchmark-binary-v1"


def install_in_index(repo, spec, env):
    """Track harness inputs in the desired tree instead of rewriting them per job."""
    crate = spec["crate"]
    if not isinstance(crate, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", crate):
        raise ValueError("invalid benchmark crate")
    if not isinstance(spec["benchmark_source"], str) or not spec["benchmark_source"]:
        raise ValueError("benchmark source must be nonempty")

    def git(*args, data=None):
        return subprocess.run(["git", "-C", str(repo), *args], input=data, env=env,
                              capture_output=True, check=True).stdout

    manifest = f"rust/{crate}/Cargo.toml"
    source = f"rust/{crate}/benches/metabench_perf.rs"
    entry = git("ls-files", "--stage", "--", manifest).split()
    if not entry or entry[0] not in (b"100644", b"100755"):
        raise ValueError("benchmark manifest must be a tracked regular file")
    if git("ls-files", "--", source):
        raise ValueError("candidate uses reserved benchmark path: " + source)
    contents = git("show", ":" + manifest)
    contents += b'\n[[bench]]\nname = "metabench_perf"\nharness = false\n'
    for path, mode, data in ((manifest, entry[0].decode(), contents),
                             (source, "100644", spec["benchmark_source"].encode())):
        blob = git("hash-object", "-w", "--stdin", data=data).decode().strip()
        git("update-index", "--add", "--cacheinfo", mode, blob, path)


def binary_hash(workspace, binary):
    if not isinstance(binary, str) or not PurePosixPath(binary).is_absolute():
        return None
    check = workspace.command("sha256sum -- " + shlex.quote(binary), 60)
    words = check.get("output", "").split()
    if check["returncode"] == 0 and words and re.fullmatch(r"[0-9a-f]{64}", words[0]):
        return words[0]
    return None


def artifact(result):
    artifacts = []
    for line in result["output"].splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if (isinstance(event, dict) and event.get("reason") == "compiler-artifact"
                and event.get("target", {}).get("name") == "metabench_perf" and event.get("executable")):
            artifacts.append(event)
    return artifacts[0] if not result["returncode"] and len(artifacts) == 1 else None


def owned_binary(workspace, binary):
    """Only invalidate this container's configured Cargo output, never arbitrary paths."""
    target = PurePosixPath(workspace.environment.get("env", {}).get("CARGO_TARGET_DIR", "/build-cache/target"))
    if not target.is_absolute():
        target = PurePosixPath("/repo") / target
    path = PurePosixPath(binary)
    return path.is_absolute() and ".." not in path.parts and ".." not in target.parts and path != target and path.is_relative_to(target)


def archive_receipt(workspace, contents):
    """Checkpoint evidence must survive later builds replacing the cache pointer."""
    checksum = hashlib.sha256(contents).hexdigest()
    directory = workspace.directory / (workspace.lane + ".benchmark-builds")
    directory.mkdir(exist_ok=True)
    path = directory / (checksum + ".json")
    if path.exists():
        if path.read_bytes() != contents:
            raise RuntimeError("archived benchmark build receipt changed")
    else:
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(contents)
        os.replace(temporary, path)
    return {"build_receipt": str(path), "build_receipt_sha256": checksum}


def build(workspace, spec):
    """Reuse only an intact binary for the exact source, toolchain and configuration."""
    started = time.monotonic()
    features = spec.get("features", [])
    if not isinstance(features, list) or any(not isinstance(f, str) or not f for f in features):
        raise ValueError("benchmark features must be a list of nonempty strings")
    argv = ["cargo", "bench", "--locked", "--profile", "release-with-debug", "-p", spec["crate"],
            "--bench", "metabench_perf", "--no-run", "--message-format=json"]
    if features:
        argv += ["--features", ",".join(features)]
    command = shlex.join(argv)
    toolchain = workspace.command("rustc -vV && cargo -V", 30)
    if toolchain["returncode"]:
        raise RuntimeError("cannot identify benchmark toolchain: " + toolchain["output"][-2000:])
    contract = {"policy": CACHE_POLICY, "source_tree": workspace.identity["source_tree"],
                "base_commit": workspace.base, "image_id": workspace.identity["image_id"],
                "environment": workspace.environment, "benchmark": spec,
                "toolchain": toolchain["output"], "command": command}
    key = digest(contract)
    receipt_path = workspace.directory / (workspace.lane + ".benchmark-build.json")
    try:
        saved_bytes = receipt_path.read_bytes()
        saved = json.loads(saved_bytes)
    except (OSError, ValueError):
        saved = {}
    if (isinstance(saved, dict) and saved.get("key") == key and saved.get("contract") == contract
            and isinstance(saved.get("compiled_at"), (int, float)) and math.isfinite(saved["compiled_at"])
            and isinstance(saved.get("execution"), dict) and saved["execution"].get("returncode") == 0
            and isinstance(saved.get("binary_sha256"), str)
            and binary_hash(workspace, saved.get("binary")) == saved["binary_sha256"]):
        return saved["binary"], {
            "returncode": 0, "output": "", "stderr": None, "timed_out": False, "seconds": 0.0,
            "cache_hit": True, "cache_policy": CACHE_POLICY, "cache_key": key,
            "binary_sha256": saved["binary_sha256"], "compiled_at": saved["compiled_at"],
            **archive_receipt(workspace, saved_bytes),
            "cache_lookup_seconds": time.monotonic() - started}
    lookup_seconds = time.monotonic() - started
    result = workspace.command(command, 900)
    compiled = artifact(result)
    if compiled and compiled.get("fresh") is True:
        # Cargo may consider an existing but corrupted executable fresh. Without
        # a validated receipt, accepting that file would bless the corruption.
        binary = compiled["executable"]
        if not owned_binary(workspace, binary):
            raise RuntimeError("refusing to invalidate a benchmark outside its Cargo target directory")
        removed = workspace.command("rm -f -- " + shlex.quote(binary), 30)
        if removed["returncode"]:
            raise RuntimeError("cannot invalidate unverified benchmark binary")
        initial = result
        result = workspace.command(command, 900)
        compiled = artifact(result)
        result = {**result, "seconds": initial["seconds"] + result["seconds"],
                  "unverified_fresh_execution": initial}
        if compiled and compiled.get("fresh") is True:
            raise RuntimeError("Cargo did not rebuild the unverified benchmark executable")
    result = {**result, "cache_hit": False, "cache_policy": CACHE_POLICY, "cache_key": key,
              "cache_lookup_seconds": lookup_seconds}
    if compiled is None:
        return None, result
    binary = compiled["executable"]
    checksum = binary_hash(workspace, binary)
    if checksum is None:
        raise RuntimeError("built benchmark binary is missing or cannot be checksummed")
    receipt = {"key": key, "contract": contract, "binary": binary, "binary_sha256": checksum,
               "compiled_at": time.time(), "execution": result}
    contents = (json.dumps(receipt, allow_nan=False) + "\n").encode()
    evidence = archive_receipt(workspace, contents)
    temporary = receipt_path.with_suffix(".tmp")
    temporary.write_bytes(contents)
    os.replace(temporary, receipt_path)
    result.update(binary_sha256=checksum, compiled_at=receipt["compiled_at"],
                  **evidence)
    return binary, result
