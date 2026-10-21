import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from metabench.benchmark_build import build
from metabench.checkouts import TrialCheckout, git
from metabench.schema import digest
from metabench.vm_worker import CachedWorkspace


class BuildWorkspace:
    """A compiler fixture with durable files and observable compilation/sampling."""
    def __init__(self, root, request=None, lane="reference"):
        request = request or {"trial_id": "fixture", "environment": {"image": "sha256:fixture"}}
        self.directory = Path(root) / request["trial_id"]
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lane = lane
        self.environment = copy.deepcopy(request["environment"])
        self.base = "base"
        self.identity = {"source_tree": "source", "image_id": self.environment["image"]}
        self.toolchain = "rustc fixture\ncargo fixture\n"
        self.binary = "/build-cache/target/release-with-debug/metabench_perf"
        self.content = None
        self.fail_build = False
        self.cargo_fresh = False
        self.compilations = self.samples = 0
        self.commands = []
        self.preparations = []
        self.container = request["trial_id"] + "-" + lane

    def prepare(self, patches, *, benchmark):
        self.preparations.append((patches, benchmark))
        self.identity["source_tree"] = digest([patches, benchmark])

    def restore_source_ownership(self):
        return {"returncode": 0}

    def command(self, command, timeout=900, input=None):
        self.commands.append(command)
        output, code = "", 0
        if command == "rustc -vV && cargo -V":
            output = self.toolchain
        elif command.startswith("sha256sum"):
            code = int(self.content is None)
            if not code:
                output = hashlib.sha256(self.content).hexdigest() + "  " + self.binary
        elif command.startswith("cargo bench"):
            fresh = self.cargo_fresh and self.content is not None
            self.compilations += int(not fresh)
            code = int(self.fail_build)
            if not code:
                if not fresh:
                    self.content = (self.identity["source_tree"] + self.toolchain).encode()
                output = json.dumps({"reason": "compiler-artifact", "target": {"name": "metabench_perf"},
                                     "executable": self.binary, "fresh": fresh})
        elif command.startswith("rm -f -- "):
            self.content = None
        elif command.startswith("taskset"):
            self.samples += 1
            output = 'MB_METRIC {"name":"latency","unit":"ns/op","value":100}'
        else:
            raise AssertionError("unexpected fixture command: " + command)
        return {"returncode": code, "output": output, "stderr": None, "timed_out": False, "seconds": .01}


class BenchmarkBuildTest(unittest.TestCase):
    spec = {"crate": "fixture", "benchmark_source": "fn main() {}", "repeats": 7}

    def test_warm_binary_survives_controller_restart_but_missing_or_changed_bytes_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = BuildWorkspace(directory)
            binary, cold = build(workspace, self.spec)
            self.assertFalse(cold["cache_hit"])
            fresh_controller = BuildWorkspace(directory)
            fresh_controller.content = workspace.content
            reused, warm = build(fresh_controller, self.spec)
            self.assertEqual(reused, binary)
            self.assertTrue(warm["cache_hit"])
            self.assertEqual(fresh_controller.compilations, 0)
            for contents in (None, b"tampered binary"):
                fresh_controller.content = contents
                _, rebuilt = build(fresh_controller, self.spec)
                self.assertFalse(rebuilt["cache_hit"])
            self.assertEqual(fresh_controller.compilations, 2)

    def test_each_build_input_invalidates_the_cached_binary(self):
        changes = {
            "source": lambda w, s: w.identity.update(source_tree="new tree"),
            "image": lambda w, s: w.identity.update(image_id="sha256:other"),
            "flags": lambda w, s: w.environment.update(env={"RUSTFLAGS": "-C target-cpu=haswell"}),
            "toolchain": lambda w, s: setattr(w, "toolchain", "different rustc"),
            "harness": lambda w, s: s.update(benchmark_source="fn main() { println!(\"new\"); }"),
            "features": lambda w, s: s.update(features=["feature-a"]),
        }
        for name, change in changes.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                workspace, spec = BuildWorkspace(directory), copy.deepcopy(self.spec)
                build(workspace, spec)
                change(workspace, spec)
                _, evidence = build(workspace, spec)
                self.assertFalse(evidence["cache_hit"])
                self.assertEqual(workspace.compilations, 2)

    def test_cargo_fresh_cannot_revalidate_a_corrupted_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = BuildWorkspace(directory)
            build(workspace, self.spec)
            original = workspace.content
            workspace.cargo_fresh = True
            workspace.content = b"corrupted but Cargo still reports Fresh"
            _, result = build(workspace, self.spec)
            self.assertEqual(workspace.content, original)
            self.assertEqual(workspace.compilations, 2)
            self.assertIn("unverified_fresh_execution", result)
            self.assertEqual(result["binary_sha256"], hashlib.sha256(original).hexdigest())

    def test_unverified_cargo_artifact_cannot_delete_outside_target(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = BuildWorkspace(directory)
            workspace.cargo_fresh = True
            workspace.content = b"not owned by the build cache"
            workspace.binary = "/etc/important"
            with self.assertRaisesRegex(RuntimeError, "outside its Cargo target"):
                build(workspace, self.spec)
            self.assertFalse(any(c.startswith("rm ") for c in workspace.commands))

    def test_failed_build_is_not_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = BuildWorkspace(directory)
            workspace.fail_build = True
            binary, result = build(workspace, self.spec)
            self.assertIsNone(binary)
            self.assertNotEqual(result["returncode"], 0)
            self.assertFalse((workspace.directory / "reference.benchmark-build.json").exists())
            workspace.fail_build = False
            self.assertIsNotNone(build(workspace, self.spec)[0])

    def test_later_builds_preserve_old_checkpoint_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = BuildWorkspace(directory)
            _, first = build(workspace, self.spec)
            path = Path(first["build_receipt"])
            original = path.read_bytes()
            workspace.identity["source_tree"] = "later checkpoint"
            _, later = build(workspace, self.spec)
            self.assertNotEqual(first["build_receipt"], later["build_receipt"])
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(hashlib.sha256(original).hexdigest(), first["build_receipt_sha256"])

    def test_corrupt_or_incomplete_receipts_never_count_as_cache_hits(self):
        for contents in ("not json", "[]", "{}", None):
            with self.subTest(contents=contents), tempfile.TemporaryDirectory() as directory:
                workspace = BuildWorkspace(directory)
                build(workspace, self.spec)
                receipt = workspace.directory / "reference.benchmark-build.json"
                if contents is None:
                    saved = json.loads(receipt.read_text())
                    del saved["compiled_at"]
                    contents = json.dumps(saved)
                receipt.write_text(contents)
                _, result = build(workspace, self.spec)
                self.assertFalse(result["cache_hit"])
                self.assertEqual(workspace.compilations, 2)

    def test_benchmark_inputs_keep_mtimes_and_private_functional_tree_intact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            git("init", "-q", repo)
            git("config", "user.name", "test", cwd=repo)
            git("config", "user.email", "test@localhost", cwd=repo)
            crate = repo / "rust/fixture"
            (crate / "src").mkdir(parents=True)
            (crate / "Cargo.toml").write_text('[package]\nname="fixture"\nversion="0.1.0"\n')
            (crate / "src/lib.rs").write_text("pub fn value() -> u32 { 1 }\n")
            git("add", ".", cwd=repo)
            git("commit", "-qm", "base", cwd=repo)
            base = git("rev-parse", "HEAD", cwd=repo)
            workspaces = {}
            for lane in ("public", "verify", "performance", "reference"):
                checkout = TrialCheckout(repo, base, root / lane)
                workspace = object.__new__(CachedWorkspace)
                workspace.root, workspace.directory = checkout.root, root / lane
                workspace.base, workspace.lane, workspace.identity = base, lane, {}
                workspaces[lane] = workspace
            hidden = {"rust/fixture/src/lib.rs": {"namespace": "__hidden", "source": "mod __hidden {}"}}
            workspaces["verify"].prepare([""], hidden)
            before = (workspaces["verify"].root / "rust/fixture/src/lib.rs").read_bytes()
            performance = workspaces["performance"]
            performance.prepare([""], benchmark=self.spec)
            paths = [performance.root / p for p in ("rust/fixture/src/lib.rs", "rust/fixture/Cargo.toml", "rust/fixture/benches/metabench_perf.rs")]
            stamps = [p.stat().st_mtime_ns for p in paths]
            performance.prepare([""], benchmark=self.spec)
            self.assertTrue(performance.identity["source_reused"])
            self.assertEqual([p.stat().st_mtime_ns for p in paths], stamps)
            self.assertEqual(paths[1].read_text().count("[[bench]]"), 1)
            self.assertNotIn("__hidden", paths[0].read_text())
            self.assertEqual((workspaces["verify"].root / "rust/fixture/src/lib.rs").read_bytes(), before)
            for lane in ("public", "verify"):
                self.assertFalse((workspaces[lane].root / "rust/fixture/benches/metabench_perf.rs").exists())
            reserved = ('diff --git a/rust/fixture/benches/metabench_perf.rs b/rust/fixture/benches/metabench_perf.rs\n'
                        'new file mode 100644\n--- /dev/null\n+++ b/rust/fixture/benches/metabench_perf.rs\n@@ -0,0 +1 @@\n+owned by candidate\n')
            with self.assertRaisesRegex(ValueError, "reserved benchmark"):
                performance.prepare([reserved], benchmark=self.spec)


if __name__ == "__main__":
    unittest.main()
