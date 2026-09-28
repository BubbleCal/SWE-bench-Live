"""SSH VM inventory, detached execution and durable queue workers."""
import hashlib
import fcntl
import json
import shlex
import subprocess
import tarfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath

from .schema import write_json


class SSHVM:
    resource_timing = True
    def __init__(self, specification, control_directory):
        self.spec = specification
        self.id = specification["id"]
        self.root = PurePosixPath(specification["root"])
        if not self.root.is_absolute() or ".." in self.root.parts or "," in str(self.root):
            raise ValueError("VM root must be an absolute path without '..' or commas")
        self.ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                    *specification.get("ssh_args", []), specification["host"]]
        self.scp = ["scp", "-q", *specification.get("ssh_args", [])]
        self.local = Path(control_directory) / self.id
        self.local.mkdir(parents=True, exist_ok=True)
        self.installed = False
        self.assets = set()

    def remote(self, command, *, timeout=60, input=None):
        result = subprocess.run([*self.ssh, command], input=input, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if result.returncode:
            raise RuntimeError(f"VM {self.id}: {result.stderr[-2000:]}")
        return result.stdout

    def python(self, script, *arguments):
        return self.remote(shlex.join([self.spec.get("python", "python3"), "-c", script, *map(str, arguments)]))

    def copy(self, source, destination):
        subprocess.run([*self.scp, str(source), self.spec["host"] + ":" + str(destination)],
                       check=True, timeout=300, stdout=subprocess.DEVNULL)

    def install(self):
        if self.installed:
            return
        package = Path(__file__).parent
        content = b"".join(p.name.encode() + p.read_bytes() for p in sorted(package.glob("*.py")))
        revision = hashlib.sha256(content).hexdigest()
        self.control = self.root / "control" / revision
        self.remote("mkdir -p " + shlex.quote(str(self.control)))
        archive = self.local / "control.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            for path in sorted(package.glob("*.py")):
                output.add(path, arcname="metabench/" + path.name)
        self.copy(archive, self.control / "control.tar.gz")
        self.remote("tar -xzf " + shlex.quote(str(self.control / "control.tar.gz")) + " -C " + shlex.quote(str(self.control)))
        self.installed = True

    def prepare(self, environments):
        self.install()
        images = {}
        for environment in environments:
            image = environment["image"]
            if image not in images:
                images[image] = self.remote(shlex.join(["docker", "image", "inspect", image, "--format", "{{.Id}}"])).strip()
            if environment.get("image_id") and environment["image_id"] != images[image]:
                raise ValueError("pinned image identity changed on VM " + self.id)
        return {"vm_id": self.id, "images": images}

    def stage_base(self, base):
        base = Path(base)
        fingerprint = hashlib.sha256(base.read_bytes()).hexdigest()
        remote_base = self.root / "assets" / (fingerprint + ".tar")
        if fingerprint not in self.assets:
            self.remote("mkdir -p " + shlex.quote(str(self.root / "assets")))
            actual = self.python("from pathlib import Path;import hashlib,sys;p=Path(sys.argv[1]);print(hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else '')", remote_base).strip()
            if actual != fingerprint:
                self.copy(base, str(remote_base) + ".upload")
                actual = self.python("from pathlib import Path;import hashlib,sys;print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())", str(remote_base) + ".upload").strip()
                if actual != fingerprint:
                    raise ValueError("base archive transfer checksum mismatch on " + self.id)
                self.remote("mv " + shlex.quote(str(remote_base) + ".upload") + " " + shlex.quote(str(remote_base)))
            self.assets.add(fingerprint)
        return {"base_archive": str(remote_base), "base_sha256": fingerprint}

    def status(self, identity):
        directory = self.root / "queue" / identity
        script = '''import json,sys,time
from pathlib import Path
p=Path(sys.argv[1]);result=p/'result.json'
ready=p/'running.json'
timing={'observed_at':time.time(),'execution_started_at':json.loads(ready.read_text())['started_at'] if ready.exists() else None}
if result.exists(): print(json.dumps({'state':'completed','result':json.loads(result.read_text()),**timing}))
elif (p/'pid').exists():
 pid=(p/'pid').read_text().strip();cmd=Path('/proc')/pid/'cmdline'
 alive=cmd.exists() and str(p).encode() in cmd.read_bytes()
 if not alive and result.exists(): print(json.dumps({'state':'completed','result':json.loads(result.read_text()),**timing}))
 else: print(json.dumps({'state':'running' if alive else 'interrupted',**timing}))
else: print(json.dumps({'state':'not_started'}))
'''
        return json.loads(self.python(script, directory))

    def launch(self, job):
        self.install()
        identity = job["id"]
        status = self.status(identity)
        if status["state"] in ("running", "completed"):
            return
        if status["state"] == "interrupted":
            raise RuntimeError("remote worker stopped without a receipt; inspect and reconcile job " + identity)
        directory = self.root / "queue" / identity
        self.remote("mkdir -p " + shlex.quote(str(directory)) + " " + shlex.quote(str(self.root / "assets")))
        payload = dict(job["payload"])
        base = Path(payload.pop("base_archive"))
        payload.update(trial_id=job["trial_id"], kind=job["kind"], **self.stage_base(base))
        local_request = self.local / (identity + ".json")
        write_json(local_request, payload)
        self.copy(local_request, directory / "request.json.upload")
        self.remote("mv " + shlex.quote(str(directory / "request.json.upload")) + " " + shlex.quote(str(directory / "request.json")))
        argv = [self.spec.get("python", "python3"), "-m", "metabench.vm_worker", str(self.root), str(directory)]
        command = ("nohup " + shlex.join(["env", "PYTHONPATH=" + str(self.control), *argv])
                   + " >" + shlex.quote(str(directory / "worker.log")) + " 2>&1 </dev/null &")
        # $! belongs to this exact launch; it is never inferred from a process name.
        self.remote(command + " echo $! >" + shlex.quote(str(directory / "pid")))

    def wait(self, identity, stop, on_started=None):
        errors = 0
        while not stop.is_set():
            try:
                status = self.status(identity)
                errors = 0
            except (RuntimeError, subprocess.SubprocessError, ValueError):
                errors += 1
                if errors >= 5:
                    raise RuntimeError("VM connection lost; remote job and lock are retained for reconciliation")
                stop.wait(2)
                continue
            if on_started and status.get("execution_started_at") is not None:
                # Convert a remote duration, not its epoch, to controller time.
                elapsed = max(0, status["observed_at"] - status["execution_started_at"])
                on_started(time.time() - elapsed)
                on_started = None
            if status["state"] == "completed":
                return status["result"]
            if status["state"] != "running":
                raise RuntimeError("remote job has no completed receipt: " + identity)
            stop.wait(1)
        raise InterruptedError("controller stopped; remote job retains the VM lock until it exits")


class VMPool:
    def __init__(self, queue, specifications, control_directory, *, vm_count=None, driver_factory=SSHVM):
        count = len(specifications) if vm_count is None else vm_count
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= len(specifications):
            raise ValueError("vm_count must select 1..N configured VMs")
        chosen = specifications[:count]
        if len({v["id"] for v in chosen}) != count:
            raise ValueError("VM ids must be unique")
        self.queue = queue
        self.drivers = {v["id"]: driver_factory(v, control_directory) for v in chosen}
        self.stop = threading.Event()
        self.owner = uuid.uuid4().hex
        self.threads = []
        self.errors = {}

    def start(self):
        for name in self.drivers:
            worker = threading.Thread(target=self.worker, args=(name,), daemon=True)
            worker.start()
            self.threads.append(worker)
        return self

    def worker(self, name):
        lease_path = self.queue.path.parent / ("vm-" + hashlib.sha256(name.encode()).hexdigest()[:16] + ".lock")
        with lease_path.open("a+") as lease:
            while not self.stop.is_set():
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    self.stop.wait(.1)
            if not self.stop.is_set():
                self._worker(name)

    def _worker(self, name):
        driver = self.drivers[name]
        def wait(job):
            if getattr(driver, "resource_timing", False):
                return driver.wait(job["id"], self.stop, on_started=lambda timestamp:
                    self.queue.resource_started(job["id"], timestamp, owner=job["owner"]))
            self.queue.resource_started(job["id"], job["started"], owner=job["owner"])
            return driver.wait(job["id"], self.stop)
        try:
            # Reconcile a crashed local controller before claiming more work.
            for job in self.queue.running():
                if job["vm_id"] == name:
                    driver.launch(job)
                    result = wait(job)
                    self.queue.finish(job["id"], result, owner=job["owner"])
            while not self.stop.is_set():
                job = self.queue.claim(name, self.owner)
                if job is None:
                    self.stop.wait(.1)
                    continue
                driver.launch(job)
                result = wait(job)
                self.queue.finish(job["id"], result, owner=self.owner)
        except Exception as error:
            # Do not reclaim/expire an uncertain VM job. Other VMs can continue.
            self.errors[name] = str(error)

    def await_job(self, identity):
        while not self.stop.is_set():
            job = self.queue.get(identity)
            if job["status"] == "completed":
                return job
            if job["vm_id"] in self.errors:
                raise RuntimeError(self.errors[job["vm_id"]])
            self.stop.wait(.1)
        raise InterruptedError("VM pool stopped")

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=20)
