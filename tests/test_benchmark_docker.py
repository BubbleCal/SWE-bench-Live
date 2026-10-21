"""Offline Rust builds through real persistent Docker lanes (no model calls)."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from metabench.benchmark_build import build
from metabench.checkouts import TrialCheckout, git
from metabench.checkpoint_eval import evaluate
from metabench.schema import digest
from metabench.vm_worker import CachedWorkspace


@unittest.skipUnless(os.environ.get("METABENCH_TEST_RUST_IMAGE"), "set METABENCH_TEST_RUST_IMAGE for Rust Docker integration")
class BenchmarkDockerTest(unittest.TestCase):
    def test_real_compiler_cache_lanes_and_fresh_paired_samples(self):
        containers = set()
        with tempfile.TemporaryDirectory() as directory:
            root, repo = Path(directory), Path(directory) / "repo"
            git("init", "-q", repo)
            git("config", "user.name", "test", cwd=repo)
            git("config", "user.email", "test@localhost", cwd=repo)
            (repo / "rust/fixture/src").mkdir(parents=True)
            (repo / "Cargo.toml").write_text('[workspace]\nmembers=["rust/fixture"]\nresolver="2"\n'
                                            '[profile.release-with-debug]\ninherits="release"\ndebug=1\nlto="thin"\n')
            (repo / "Cargo.lock").write_text('version = 3\n\n[[package]]\nname = "fixture"\nversion = "0.1.0"\n')
            (repo / "rust/fixture/Cargo.toml").write_text('[package]\nname="fixture"\nversion="0.1.0"\nedition="2021"\n')
            (repo / "rust/fixture/src/lib.rs").write_text('pub fn value() -> u64 { 100 }\n')
            git("add", ".", cwd=repo)
            git("commit", "-qm", "base", cwd=repo)
            base = git("rev-parse", "HEAD", cwd=repo)
            checkout = TrialCheckout(repo, base, root / "checkout")
            archive = root / "base.tar"
            checkout.base_archive(archive)
            image = subprocess.check_output(["docker", "image", "inspect", os.environ["METABENCH_TEST_RUST_IMAGE"],
                                             "--format", "{{.Id}}"], text=True).strip()
            identity = hashlib.sha256(str(root).encode()).hexdigest()[:24]
            spec = {"crate": "fixture", "benchmark_source":
                    'fn main() { println!(r#"MB_METRIC {{"name":"latency","unit":"ns/op","value":{}}}"#, fixture::value()); }\n',
                    "repeats": 7}
            request = {"trial_id": identity, "base_archive": str(archive),
                       "base_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                       "environment": {"image": image, "cpus": 2, "memory": "2g"},
                       "patch": "", "task": {"patch": "", "supplemental": spec}}
            reference_id = digest({"role": "reference-performance-v1", "base_sha256": request["base_sha256"],
                                   "environment": request["environment"], "patch": "", "spec": spec})[:24]
            containers.add("metabench-native-" + reference_id + "-reference")
            try:
                public = CachedWorkspace(root / "vm", request, "public")
                containers.add(public.container)
                public.prepare([""])
                verify = CachedWorkspace(root / "vm", request, "verify")
                containers.add(verify.container)
                verify.prepare([""], {"rust/fixture/src/lib.rs": {
                    "namespace": "__metabench_hidden", "source": '#[cfg(test)] mod __metabench_hidden { #[test] fn correct() { assert_eq!(super::value(),100); } }'}})
                checked = verify.command("cargo test --locked --offline --profile release-with-debug -p fixture", 120)
                self.assertEqual(checked["returncode"], 0, checked)
                functional_source = (verify.root / "rust/fixture/src/lib.rs").read_bytes()
                functional_stamp = (verify.root / "rust/fixture/src/lib.rs").stat().st_mtime_ns
                performance = CachedWorkspace(root / "vm", request, "performance")
                containers.add(performance.container)
                performance.prepare([""], benchmark=spec)
                binary, cold = build(performance, spec)
                self.assertIsNotNone(binary, cold)
                self.assertFalse(cold["cache_hit"])
                cold_output = performance.command(binary, 30)
                self.assertEqual(cold_output["returncode"], 0, cold_output)
                self.assertIn('"value":100', cold_output["output"])
                stamp = (performance.root / "rust/fixture/benches/metabench_perf.rs").stat().st_mtime_ns
                subprocess.run(["docker", "stop", "--time", "1", performance.container], check=True, stdout=subprocess.DEVNULL)
                reopened = CachedWorkspace(root / "vm", request, "performance")
                reopened.prepare([""], benchmark=spec)
                _, warm = build(reopened, spec)
                self.assertTrue(warm["cache_hit"])
                self.assertEqual((reopened.root / "rust/fixture/benches/metabench_perf.rs").stat().st_mtime_ns, stamp)
                self.assertEqual((verify.root / "rust/fixture/src/lib.rs").read_bytes(), functional_source)
                self.assertEqual((verify.root / "rust/fixture/src/lib.rs").stat().st_mtime_ns, functional_stamp)
                self.assertNotIn("__metabench_hidden", (reopened.root / "rust/fixture/src/lib.rs").read_text())
                self.assertFalse((public.root / "rust/fixture/benches/metabench_perf.rs").exists())
                self.assertNotEqual(public.seed, verify.seed)
                self.assertNotEqual(verify.seed, reopened.seed)
                # Cargo must rebuild if an executable disappeared from a retained container.
                self.assertEqual(reopened.command("rm -- " + binary, 30)["returncode"], 0)
                _, rebuilt = build(reopened, spec)
                self.assertFalse(rebuilt["cache_hit"])
                self.assertEqual(reopened.command(binary, 30)["returncode"], 0)
                self.assertEqual(reopened.command("printf corrupted > " + binary, 30)["returncode"], 0)
                _, repaired = build(reopened, spec)
                self.assertFalse(repaired["cache_hit"])
                self.assertEqual(reopened.command(binary, 30)["returncode"], 0,
                                 "Cargo Fresh must not preserve a corrupted cached executable")
                # The production sampler uses CPU 2; generic two-core CI runners
                # still exercise the real compiler, checksum and lane assertions above.
                affinity = reopened.command("taskset -c 2 true", 30)
                with self.subTest("fresh paired samples from cached binaries"):
                    if affinity["returncode"]:
                        self.skipTest("CPU 2 is unavailable for the production sampler on this Docker host")
                    functional = {"score": 100, "scores": {"correctness": 100}, "critical_pass": True, "checks": {}}
                    first = evaluate(root / "vm", request, verify, functional)
                    second = evaluate(root / "vm", request, verify, functional)
                    self.assertEqual(first["score"], 87.5)
                    self.assertEqual(second["score"], 87.5)
                    self.assertTrue(second["supplemental_evaluation"]["reference_build"]["cache_hit"])
                    self.assertTrue(second["supplemental_evaluation"]["candidate_build"]["cache_hit"])
                    self.assertEqual(len(second["supplemental_evaluation"]["pairs"]), 7)
                    self.assertTrue(all(p[side]["execution"]["returncode"] == 0
                                        for p in second["supplemental_evaluation"]["pairs"] for side in ("reference", "candidate")))
                    self.assertEqual((verify.root / "rust/fixture/src/lib.rs").read_bytes(), functional_source)
            finally:
                for container in containers:
                    subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
