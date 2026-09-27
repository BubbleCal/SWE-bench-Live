"""Durable VM-side jobs; one kernel lock covers prepare, compile and tests."""
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from .evaluate import evaluate_workspace
from .process import execute
from .runtime import RuntimeErrorWithLog
from .verification import append_to_index

VM_LOCK = Path("/tmp/metabench-vm.lock")


def write(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, allow_nan=False, indent=2) + "\n")
    os.replace(temporary, path)


def must(argv, **kwargs):
    result = execute(argv, **kwargs)
    if result["returncode"]:
        raise RuntimeErrorWithLog(result["output"])
    return result


class CachedWorkspace:
    def __init__(self, root, request, lane):
        trial = request["trial_id"]
        if not re.fullmatch(r"[0-9a-f]{24}", trial):
            raise ValueError("invalid trial identity")
        self.environment = request["environment"]
        self.lane = lane
        self.directory = root / "trials" / trial
        self.directory.mkdir(parents=True, exist_ok=True)
        self.root = self.directory / lane
        # Public agents must never inherit hidden-test Git objects from a prior
        # grading round or a later run that reuses this trial's warm containers.
        self.seed = self.directory / (lane + ".seed")
        identity = {"trial_id": trial, "base_sha256": request["base_sha256"], "environment": self.environment}
        manifest = self.directory / "identity.json"
        if manifest.exists():
            if json.loads(manifest.read_text()) != identity:
                raise ValueError("persistent directory identity mismatch")
        elif self.seed.exists():
            raise ValueError("unregistered VM seed exists")
        if not self.seed.exists():
            self.seed.mkdir()
            archive = Path(request["base_archive"])
            if hashlib.sha256(archive.read_bytes()).hexdigest() != request["base_sha256"]:
                raise ValueError("base archive hash mismatch")
            with tarfile.open(archive) as source:
                for item in source.getmembers():
                    path = (self.seed / item.name).resolve()
                    if not path.is_relative_to(self.seed.resolve()):
                        raise ValueError("base archive escapes its directory")
                    if item.issym() or item.islnk():
                        target = (path.parent if item.issym() else self.seed) / item.linkname
                        if not target.resolve().is_relative_to(self.seed.resolve()):
                            raise ValueError("base link escapes its directory")
                    if not (item.isfile() or item.isdir() or item.issym() or item.islnk()):
                        raise ValueError("unsupported archive entry")
                source.extractall(self.seed, filter="data")
            base_info = json.loads((self.seed / "base.json").read_text())
            if base_info["format"] != "metabench-git-base-v1" or not re.fullmatch(r"[0-9a-f]{40,64}", base_info["commit"]):
                raise ValueError("invalid base Git identity")
            for args in (("init", "--bare", "-q"), ("config", "user.name", "metabench"),
                         ("config", "user.email", "benchmark@localhost"), ("config", "core.hooksPath", "/dev/null")):
                must(["git", "-C", str(self.seed), *args])
            (self.seed / "shallow").write_text(base_info["commit"] + "\n")
            with (self.seed / "base.pack").open("rb") as pack:
                subprocess.run(["git", "-C", str(self.seed), "index-pack", "--stdin"], stdin=pack,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            (self.seed / "base.pack").unlink()
            must(["git", "-C", str(self.seed), "update-ref", "refs/heads/base", base_info["commit"]])
            must(["git", "-C", str(self.seed), "symbolic-ref", "HEAD", "refs/heads/base"])
            tree = must(["git", "-C", str(self.seed), "rev-parse", "HEAD^{tree}"])["output"].strip()
            if tree != base_info["tree"]:
                raise ValueError("base tree differs from exported Git tree")
            write(manifest, identity)
        self.base = must(["git", "-C", str(self.seed), "rev-parse", "HEAD"])["output"].strip()
        if not self.root.exists():
            must(["git", "-C", str(self.seed), "worktree", "add", "--detach", str(self.root), self.base])
        expected_git = self.seed / "worktrees" / lane
        marker = self.root / ".git"
        if not marker.is_file() or marker.is_symlink() or marker.read_text().strip() != "gitdir: " + str(expected_git):
            raise ValueError("worktree metadata was modified; refusing to reset an unknown repository")
        image = self.environment["image"]
        if not (image.startswith("sha256:") or "@sha256:" in image):
            raise ValueError("VM image must be pinned")
        image_id = must(["docker", "image", "inspect", image, "--format", "{{.Id}}"])["output"].strip()
        self.container = "metabench-native-" + trial + "-" + lane
        existing = execute(["docker", "inspect", self.container, "--format", "{{.Image}} {{.State.Running}}"])
        if existing["returncode"]:
            must(["docker", "run", "-d", "--name", self.container,
                  "--label", "metabench.native=true", "--label", "metabench.trial=" + trial, "--network=none", "--cap-drop=ALL",
                  "--cap-add=DAC_OVERRIDE",
                  "--cap-add=CHOWN", "--cap-add=FOWNER",
                  "--security-opt=no-new-privileges", "--pids-limit", "512",
                  "--cpus", str(self.environment.get("cpus", 4)), "--memory", self.environment.get("memory", "8g"),
                  "--mount", f"type=bind,src={self.root},dst=/repo",
                  "--mount", f"type=bind,src={self.root},dst={self.root}",
                  "--mount", f"type=bind,src={self.seed},dst={self.seed},readonly",
                  "--workdir", "/repo", "--entrypoint", "/bin/sh", image_id,
                  "-c", "while :; do sleep 3600; done"], timeout=120)
        else:
            previous_image, running = existing["output"].strip().split()
            if previous_image != image_id:
                raise ValueError("persistent container image changed")
            if running != "true":
                must(["docker", "start", self.container], timeout=120)
        must(["docker", "exec", self.container, "git", "config", "--global", "--replace-all", "safe.directory", "/repo"])
        self.identity = {"backend": "docker", "image_id": image_id, "score_eligible": True,
                         "persistent_container": self.container, "vm_directory": str(self.root),
                         "base_commit": self.base,
                         "capabilities": ["DAC_OVERRIDE", "CHOWN", "FOWNER"], "network": "none"}

    def restore_source_ownership(self):
        # Tests may create root-owned pycache/build scratch files in the bind
        # mount. Return only this source tree to the host owner before git reset.
        command = (f"find /repo -xdev ! -user {os.getuid()} -exec chown -h {os.getuid()}:{os.getgid()} {{}} +\n"
                   "find /repo -xdev -type d ! -perm -0700 -exec chmod u+rwx {} +\n"
                   "find /repo -xdev -type f ! -perm -0600 -exec chmod u+rw {} +")
        return execute(["docker", "exec", self.container, "/bin/sh", "-ec", command], timeout=60)

    def apply(self, patch):
        if patch.strip():
            must(["git", "-C", str(self.root), "apply", "--index", "--whitespace=nowarn", "-"], input=patch)

    def prepare(self, patches, verification_append=None):
        state_path = self.directory / (self.lane + ".source.json")
        key = hashlib.sha256(json.dumps([self.base, patches, verification_append]).encode()).hexdigest()
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        tree = must(["git", "-C", str(self.root), "write-tree"])["output"].strip()
        clean = execute(["git", "-C", str(self.root), "diff", "--quiet"])["returncode"] == 0
        reused = state.get("key") == key and state.get("tree") == tree and clean
        if not clean:
            must(["git", "-C", str(self.root), "read-tree", "--reset", "-u", tree])
        # The index includes candidate-added files; remove only scratch files.
        # Identical snapshots never rewrite source mtimes or invalidate Cargo.
        must(["git", "-C", str(self.root), "clean", "-ffdx"])
        if not reused:
            # Build the desired tree in a separate index, then update only files
            # whose contents changed. A documentation-only edit must not rewrite
            # previously modified Rust sources and trigger a spurious rebuild.
            with tempfile.TemporaryDirectory(dir=self.directory, prefix="index-") as temporary:
                env = {**os.environ, "GIT_INDEX_FILE": str(Path(temporary) / "index")}
                must(["git", "-C", str(self.root), "read-tree", self.base], env=env)
                for patch in patches:
                    if patch.strip():
                        must(["git", "-C", str(self.root), "apply", "--cached", "--whitespace=nowarn", "-"], input=patch, env=env)
                if verification_append:
                    append_to_index(self.root, verification_append, env)
                desired = must(["git", "-C", str(self.root), "write-tree"], env=env)["output"].strip()
            must(["git", "-C", str(self.root), "read-tree", "-m", "-u", tree, desired])
            write(state_path, {"key": key, "tree": desired})
        self.identity["source_reused"] = reused

    def command(self, command, timeout=1800, input=None):
        variables = {"CARGO_TARGET_DIR": "/build-cache/target", **self.environment.get("env", {})}
        arguments = [part for key, value in variables.items() for part in ("-e", key + "=" + str(value))]
        wrapped = "timeout --signal=KILL " + str(max(.1, timeout)) + " /bin/sh -c " + shlex.quote(command)
        return execute(["docker", "exec", "-i", *arguments, self.container, "/bin/sh", "-c", wrapped],
                       input=input, timeout=timeout + 10)


def run(root, job_dir):
    root, job_dir = Path(root).resolve(), Path(job_dir).resolve()
    if not job_dir.is_relative_to(root / "queue"):
        raise ValueError("job directory escapes queue")
    result_path = job_dir / "result.json"
    request = json.loads((job_dir / "request.json").read_text())
    root.mkdir(parents=True, exist_ok=True)
    queued = time.time()
    # The job is detached from SSH. A client disconnect cannot release this lock
    # while its compiler/test process is still running.
    with VM_LOCK.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if result_path.exists():
            return
        started = time.time()
        write(job_dir / "running.json", {"pid": os.getpid(), "started_at": started})
        workspace = None
        try:
            # Fencing after a killed worker: stop any surviving owned processes
            # before another job touches source or starts a compiler.
            running = must(["docker", "ps", "-q", "--filter", "label=metabench.native=true"])["output"].split()
            for container in running:
                must(["docker", "stop", "--time", "2", container], timeout=30)
            workspace = CachedWorkspace(root, request, "public" if request["kind"] == "public" else "verify")
            if request["kind"] == "public":
                workspace.prepare([request["patch"]])
                for command in workspace.environment.get("agent_setup", []):
                    setup = workspace.command(command, workspace.environment.get("setup_timeout", 1800))
                    if setup["returncode"]:
                        raise RuntimeErrorWithLog(setup["output"])
                execution = workspace.command(request["command"], request.get("timeout", 1800))
                result = {"kind": "public", "execution": execution, "environment": workspace.identity}
            else:
                workspace.prepare([request["patch"], request["task"].get("test_patch", "")],
                                  request["task"].get("verification_append"))
                result = evaluate_workspace(request["task"], request["patch"], request["environment"], workspace, patches_applied=True)
                result["kind"] = "verify"
        except Exception as error:
            result = {"status": "infrastructure_error", "error": str(error), "kind": request["kind"]}
        finally:
            if workspace is not None:
                ownership = workspace.restore_source_ownership()
                # Stop processes, retain the container filesystem/build cache.
                # The next job also fences leftovers before doing any work.
                stopped = execute(["docker", "stop", "--time", "2", workspace.container], timeout=30)
                if stopped["returncode"]:
                    result = {"status": "infrastructure_error", "error": "container cleanup failed: " + stopped["output"], "kind": request["kind"]}
                elif ownership["returncode"]:
                    result = {"status": "infrastructure_error", "error": "source ownership cleanup failed: " + ownership["output"], "kind": request["kind"]}
        result.update(vm_started_at=started, vm_finished_at=time.time(), remote_lock_wait_seconds=started-queued)
        write(result_path, result)


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2])
