"""A trial-scoped public test gateway. Hidden grading has no HTTP endpoint."""
import json
import hashlib
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class GatewayServer(ThreadingHTTPServer):
    # Fifteen native agents can poll simultaneously. The stdlib backlog of five
    # resets bursts of loopback connections on macOS before a handler sees them.
    request_queue_size = 128


class TestService:
    __test__ = False

    def __init__(self, queue, pool):
        self.queue, self.pool = queue, pool
        self.trials = {}
        self.tokens = {}
        service = self

        class Handler(BaseHTTPRequestHandler):
            def send(self, status, value):
                body = json.dumps(value, allow_nan=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def trial(self):
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                identity = service.tokens.get(token)
                if identity is None:
                    self.send(403, {"error": "unknown trial capability"})
                    return None
                return service.trials[identity]

            def do_POST(self):
                trial = self.trial()
                if trial is None:
                    return
                if urlsplit(self.path).path != "/test":
                    self.send(404, {"error": "unknown endpoint"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 100000:
                        raise ValueError("invalid request size")
                    request = json.loads(self.rfile.read(length))
                    command = request["command"]
                    timeout = request.get("timeout", trial["test_timeout"])
                    request_id = request.get("request_id")
                    if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id)):
                        raise ValueError("invalid request identity")
                    if not isinstance(command, str) or not command.strip():
                        raise ValueError("test command must be nonempty")
                    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= trial["test_timeout"]:
                        raise ValueError("invalid test timeout")
                    with trial["lock"]:
                        identity = hashlib.sha256((trial["id"] + ":" + request_id).encode()).hexdigest()[:32] if request_id else None
                        if identity:
                            try:
                                existing = service.queue.get(identity)
                            except KeyError:
                                existing = None
                            if existing:
                                if (existing["trial_id"] != trial["id"] or existing["kind"] != "public"
                                        or existing["payload"]["command"] != command or existing["payload"]["timeout"] != timeout):
                                    raise ValueError("request identity already belongs to different work")
                                self.send(202, {"job_id": identity, "status": existing["status"]})
                                return
                        if trial["sealed"]:
                            self.send(409, {"error": "trial generation is complete; public tests are closed"})
                            return
                        patch = trial["checkout"].snapshot()
                        job = service.queue.enqueue(trial["id"], trial["vm"], "public", {
                            "base_archive": str(trial["base_archive"]), "environment": trial["environment"],
                            "patch": patch, "command": command, "timeout": timeout}, job_id=identity)
                    self.send(202, {"job_id": job, "status": "queued"})
                except (ValueError, KeyError, OSError) as error:
                    self.send(400, {"error": str(error)})

            def do_GET(self):
                trial = self.trial()
                if trial is None:
                    return
                path = urlsplit(self.path).path
                if not path.startswith("/jobs/"):
                    self.send(404, {"error": "unknown endpoint"})
                    return
                try:
                    job = service.queue.get(path.removeprefix("/jobs/"))
                except KeyError:
                    self.send(404, {"error": "unknown job"})
                    return
                if job["trial_id"] != trial["id"] or job["kind"] != "public":
                    self.send(403, {"error": "job is not this trial's public test"})
                    return
                ready = job["resource_started"]
                if ready is None and job["status"] == "completed":
                    ready = job["started"]
                value = {"job_id": job["id"], "status": job["status"],
                         "queue_wait_seconds": (ready or time.time()) - job["created"],
                         "fifo_queue_wait_seconds": (job["started"] or time.time()) - job["created"]}
                if job["result"] is not None:
                    result = job["result"]
                    value["result"] = {k: result[k] for k in ("execution", "status", "error", "vm_started_at", "vm_finished_at") if k in result}
                if job["vm_id"] in service.pool.errors:
                    value.update(status="infrastructure_error", error=service.pool.errors[job["vm_id"]])
                self.send(200, value)

            def log_message(self, *_):
                pass

        self.server = GatewayServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def register(self, identity, checkout, base_archive, environment, vm, *, test_timeout=1800):
        token = secrets.token_hex(32)
        self.tokens[token] = identity
        self.trials[identity] = {"id": identity, "checkout": checkout, "base_archive": base_archive,
                                "environment": environment, "vm": vm, "sealed": False,
                                "test_timeout": test_timeout, "lock": threading.Lock()}
        return token

    def snapshot(self, identity):
        trial = self.trials[identity]
        with trial["lock"]:
            return trial["checkout"].snapshot()

    def seal(self, identity):
        with self.trials[identity]["lock"]:
            self.trials[identity]["sealed"] = True

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
