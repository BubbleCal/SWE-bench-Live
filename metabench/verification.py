"""Install frozen sibling tests without overwriting any candidate source bytes."""
import json
import re
import shlex
import subprocess
from pathlib import PurePosixPath


def validate_append(modules):
    if not isinstance(modules, dict):
        raise ValueError("verification_append must be a path-to-module mapping")
    for path, module in modules.items():
        if not isinstance(path, str) or not path or PurePosixPath(path).is_absolute() or any(
                part in ("..", ".git") for part in PurePosixPath(path).parts):
            raise ValueError("unsafe verification path")
        if not isinstance(module, dict) or not isinstance(module.get("source"), str) or not module["source"]:
            raise ValueError("verification module requires source")
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", module.get("namespace", "")):
            raise ValueError("verification module requires a reserved namespace")


def append_to_index(repo, modules, env):
    """Build a desired tree in the VM's private alternate index."""
    validate_append(modules)
    def git(*args, data=None):
        return subprocess.run(["git", "-C", str(repo), *args], input=data, env=env,
                              capture_output=True, check=True).stdout
    for path, module in modules.items():
        entry = git("ls-files", "--stage", "--", path).split()
        if not entry or entry[0] not in (b"100644", b"100755"):
            raise ValueError("verification target must be a tracked regular file: " + path)
        candidate = git("show", ":" + path)
        if re.search(rb"\b" + module["namespace"].encode() + rb"\b", candidate):
            raise ValueError("candidate uses reserved verifier namespace: " + path)
        assembled = candidate + b"\n\n" + module["source"].encode() + b"\n"
        blob = git("hash-object", "-w", "--stdin", data=assembled).decode().strip()
        git("update-index", "--cacheinfo", entry[0].decode(), blob, path)


def install_in_workspace(workspace, modules):
    """The non-cached evaluator uses the same append-only transport."""
    validate_append(modules)
    script = '''import json,re,sys
from pathlib import Path
for name,module in json.load(sys.stdin).items():
 p=Path(name)
 if p.is_symlink() or not p.resolve().is_relative_to(Path.cwd().resolve()):
  raise ValueError('unsafe verification target')
 candidate=p.read_bytes()
 if re.search(rb'\\b'+module['namespace'].encode()+rb'\\b',candidate):
  raise ValueError('reserved verifier namespace')
 p.write_bytes(candidate+b'\\n\\n'+module['source'].encode()+b'\\n')
'''
    result = workspace.command(shlex.join(["python3", "-c", script]), input=json.dumps(modules))
    if result["returncode"]:
        from .runtime import RuntimeErrorWithLog
        raise RuntimeErrorWithLog(result["output"])
