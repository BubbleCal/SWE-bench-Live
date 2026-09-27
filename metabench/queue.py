"""Durable FIFO test jobs. Remote VM locks remain the execution authority."""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class TestQueue:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, trial_id TEXT NOT NULL, vm_id TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
                    created REAL NOT NULL, started REAL, finished REAL,
                    result TEXT, owner TEXT
                );
                CREATE INDEX IF NOT EXISTS pending_vm ON jobs(vm_id,status,created);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, trial, vm, kind, payload, *, job_id=None):
        identity = job_id or uuid.uuid4().hex
        encoded = json.dumps(payload, sort_keys=True, allow_nan=False)
        with self.connect() as db:
            previous = db.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone()
            if previous:
                if (previous["trial_id"], previous["vm_id"], previous["kind"], previous["payload"]) != (trial, vm, kind, encoded):
                    raise ValueError("job id already belongs to different work")
                return identity
            db.execute("INSERT INTO jobs(id,trial_id,vm_id,kind,payload,status,created) VALUES(?,?,?,?,?,'queued',?)",
                       (identity, trial, vm, kind, encoded, time.time()))
        return identity

    def claim(self, vm, owner):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Never expire a running job based on a timer: its remote process may
            # still be compiling or measuring. Reconcile its receipt first.
            if db.execute("SELECT 1 FROM jobs WHERE vm_id=? AND status='running'", (vm,)).fetchone():
                return None
            row = db.execute("SELECT * FROM jobs WHERE vm_id=? AND status='queued' ORDER BY created,id LIMIT 1", (vm,)).fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET status='running',started=?,owner=? WHERE id=?",
                       (time.time(), owner, row["id"]))
        return self.get(row["id"])

    def finish(self, identity, result, *, owner):
        with self.connect() as db:
            changed = db.execute("UPDATE jobs SET status='completed',finished=?,result=? WHERE id=? AND status='running' AND owner=?",
                                 (time.time(), json.dumps(result, allow_nan=False), identity, owner)).rowcount
            if changed != 1:
                raise ValueError("only the current job owner can complete it")

    def get(self, identity):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise KeyError(identity)
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        if result["result"] is not None:
            result["result"] = json.loads(result["result"])
        return result

    def running(self):
        with self.connect() as db:
            rows = db.execute("SELECT id FROM jobs WHERE status='running'").fetchall()
        return [self.get(row["id"]) for row in rows]

    def for_trial(self, identity, kind=None):
        with self.connect() as db:
            rows = db.execute("SELECT id FROM jobs WHERE trial_id=? AND (? IS NULL OR kind=?) ORDER BY created,id",
                              (identity, kind, kind)).fetchall()
        return [self.get(row["id"]) for row in rows]

    def wait(self, identity, stop=None):
        while stop is None or not stop.is_set():
            job = self.get(identity)
            if job["status"] == "completed":
                return job
            time.sleep(.2)
        raise InterruptedError("test queue stopped; durable job retained")
