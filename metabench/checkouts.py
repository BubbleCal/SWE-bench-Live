"""Persistent, private Git worktrees for native CLI trials."""
import json
import io
import subprocess
import tarfile
import tempfile
from pathlib import Path

from .schema import digest, write_json


def git(*args, cwd=None):
    return subprocess.check_output(["git", *map(str, args)], cwd=cwd, text=True).strip()


class TrialCheckout:
    def __init__(self, source, commit, directory):
        self.directory = Path(directory).resolve()
        self.root = self.directory / "worktree"
        self.store = self.directory / "objects.git"
        self.manifest = self.directory / "checkout.json"
        identity = {"source_commit": git("rev-parse", commit + "^{commit}", cwd=source),
                    "source_tree": git("rev-parse", commit + "^{tree}", cwd=source)}
        if self.manifest.exists():
            if json.loads(self.manifest.read_text()) != identity:
                raise ValueError("worktree belongs to another base; choose a new trial directory")
            if not self.root.is_dir():
                raise ValueError("recorded worktree is missing")
        else:
            self.directory.mkdir(parents=True, exist_ok=True)
            if self.root.exists() or self.store.exists():
                raise ValueError("unregistered worktree exists; refusing to overwrite it")
            git("init", "--bare", "-q", self.store)
            # A separate object database per trial prevents both future-history
            # leakage and discovering another model's candidate commits.
            git("--git-dir", self.store, "fetch", "--quiet", "--no-tags", "--depth=1",
                "--no-write-fetch-head", str(Path(source).resolve()), identity["source_commit"])
            git("--git-dir", self.store, "worktree", "add", "--quiet", "--detach",
                self.root, identity["source_commit"])
            git("config", "user.name", "metabench", cwd=self.root)
            git("config", "user.email", "benchmark@localhost", cwd=self.root)
            git("config", "core.hooksPath", "/dev/null", cwd=self.root)
            write_json(self.manifest, identity)
        self.base = identity["source_commit"]

    def snapshot(self):
        git("add", "-A", cwd=self.root)
        return subprocess.check_output(["git", "diff", "--cached", "--binary", self.base],
                                       cwd=self.root, text=True)

    def base_archive(self, destination):
        # Ship exact base Git objects, not a newly committed copy of source files.
        # --revs plus the private shallow boundary excludes future/candidate refs.
        with tempfile.TemporaryDirectory(dir=self.directory, prefix="base-pack-") as temporary:
            pack = Path(temporary) / "base.pack"
            with pack.open("wb") as output:
                subprocess.run(["git", "--no-replace-objects", "pack-objects", "--stdout", "--revs", "--threads=1"],
                               cwd=self.root, input=(self.base + "\n").encode(), stdout=output, check=True)
            metadata = json.dumps({"format": "metabench-git-base-v1", "commit": self.base,
                                   "tree": git("rev-parse", self.base + "^{tree}", cwd=self.root)}, sort_keys=True).encode()
            with tarfile.open(destination, "w") as archive:
                info = tarfile.TarInfo("base.json"); info.size = len(metadata); info.mode = 0o644
                archive.addfile(info, io.BytesIO(metadata))
                info = tarfile.TarInfo("base.pack"); info.size = pack.stat().st_size; info.mode = 0o644
                with pack.open("rb") as source:
                    archive.addfile(info, source)


def trial_id(configuration, task, repeat=0):
    return digest({"configuration": configuration, "issue": task["instance_id"],
                   "base_commit": task["base_commit"], "repeat": repeat})[:24]
