"""Clean base snapshots. Docker is the benchmark backend; host mode is for trusted controls."""

import io
import os
import shlex
import subprocess
import tarfile
import tempfile
import uuid
from pathlib import Path

from .process import execute


class RuntimeErrorWithLog(RuntimeError):
    pass


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def export_base(repo, commit, destination):
    # git archive does not expose refs, remotes, reflogs, or future objects.
    archive = subprocess.check_output(["git", "-C", str(repo), "archive", "--format=tar", commit])
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        for member in source.getmembers():
            target = (Path(destination) / member.name).resolve()
            if not target.is_relative_to(Path(destination).resolve()):
                raise ValueError("unsafe source archive path")
            if member.issym() or member.islnk():
                link = (target.parent if member.issym() else Path(destination)) / member.linkname
                if not link.resolve().is_relative_to(Path(destination).resolve()):
                    raise ValueError("source link escapes the repository")
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise ValueError("unsupported special file in source archive")
        source.extractall(destination, filter="data")


class Workspace:
    def __init__(self, repo, task, environment, *, trusted_local=False):
        self.temp = tempfile.TemporaryDirectory(prefix="metabench-")
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.container = None
        self.environment = environment
        self.trusted_local = trusted_local
        try:
            export_base(repo, task["base_commit"], self.root)
            if trusted_local:
                self.identity = {"backend": "trusted-local", "score_eligible": False}
            else:
                image = environment["image"]
                if not (image.startswith("sha256:") or "@sha256:" in image):
                    raise ValueError("pin the environment image by digest or local sha256 image ID")
                resolved = execute(["docker", "image", "inspect", image, "--format", "{{.Id}}"])
                if resolved["returncode"]:
                    raise RuntimeErrorWithLog(resolved["output"])
                image_id = resolved["output"].strip()
                expected = environment.get("image_id")
                if expected and image_id != expected:
                    raise ValueError("Docker image changed since environment validation")
                self.identity = {"backend": "docker", "image_id": image_id, "score_eligible": True}
                name = "metabench-" + uuid.uuid4().hex
                result = execute(["docker", "run", "-d", "--name", name, "--network=none",
                                  "--cap-drop=ALL", "--security-opt=no-new-privileges",
                                  "--pids-limit", "512", "--cpus", str(environment.get("cpus", 4)),
                                  "--memory", environment.get("memory", "8g"),
                                  "--workdir", "/repo", "--entrypoint", "/bin/sh", image_id,
                                  "-c", "while :; do sleep 3600; done"], timeout=60)
                if result["returncode"]:
                    raise RuntimeErrorWithLog(result["output"])
                self.container = name
                self.must('test -z "$(ls -A /repo)"')
                # A cap-drop=ALL root cannot overwrite host-owned/read-only sources.
                def owned(member):
                    member.uid = member.gid = 0
                    member.uname = member.gname = "root"
                    if member.isdir():
                        member.mode |= 0o700
                    elif member.isfile():
                        member.mode |= 0o600
                    return member
                archive_path = Path(self.temp.name) / "source.tar"
                with tarfile.open(archive_path, "w") as archive:
                    archive.add(self.root, arcname=".", filter=owned)
                with archive_path.open("rb") as source:
                    copied = subprocess.run(["docker", "cp", "-", name + ":/repo"], stdin=source,
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
                if copied.returncode:
                    raise RuntimeErrorWithLog(copied.stdout.decode(errors="replace"))
            self.must("git init -q && git config user.email benchmark@localhost && "
                      "git config user.name benchmark && git add -A && "
                      "git -c core.hooksPath=/dev/null commit -qm base")
            self.base = self.must("git rev-parse HEAD")["output"].strip()
        except BaseException:
            self.close()
            raise

    def command(self, command, timeout=60, input=None):
        if self.container:
            # Killing only the Docker CLI would leave an exec process running in the container.
            wrapped = "timeout --signal=KILL " + str(max(0.1, timeout)) + " /bin/sh -c " + shlex.quote(command)
            variables = [item for key, value in self.environment.get("env", {}).items() for item in ("-e", key + "=" + value)]
            return execute(["docker", "exec", "-i", *variables, self.container, "/bin/sh", "-c", wrapped],
                           input=input, timeout=timeout + 5)
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TMPDIR")}
        env["HOME"] = self.temp.name
        env.update(self.environment.get("env", {}))
        # Source runs only with explicit trusted-local opt-in. No API/cloud credentials are inherited.
        return execute(["/bin/sh", "-c", command], cwd=self.root, input=input, timeout=timeout, env=env)

    def must(self, command, timeout=60, input=None):
        result = self.command(command, timeout, input)
        if result["returncode"]:
            raise RuntimeErrorWithLog(result["output"])
        return result

    def apply(self, patch):
        if patch.strip():
            # --index prevents applying against modified file contents accidentally.
            self.must("git apply --index --whitespace=nowarn -", input=patch)

    def snapshot(self):
        # Capture untracked source files as well as changes to tracked files.
        self.must("git add -A")
        return self.must(f"git diff --cached --binary {self.base}")["output"]

    def close(self):
        if self.container:
            execute(["docker", "rm", "-f", self.container], timeout=30)
            self.container = None
        self.temp.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
