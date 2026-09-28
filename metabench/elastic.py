"""Attach VM workers to a live run without restarting native model sessions."""
import argparse
import collections
import fcntl
import hashlib
import json
import math
import statistics
import time
from pathlib import Path

from .queue import TestQueue
from .vm_pool import VMPool


def install_routes(queue):
    # Existing controllers can keep their originally loaded Python code. Routing
    # happens in the INSERT transaction, before any worker can claim a new job.
    with queue.connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS trial_vm_affinity (
                trial_id TEXT PRIMARY KEY, vm_id TEXT NOT NULL, assigned_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS vm_routing_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
                trial_id TEXT NOT NULL, vm_id TEXT NOT NULL, queued_jobs INTEGER NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS route_new_trial_jobs AFTER INSERT ON jobs
            WHEN EXISTS(SELECT 1 FROM trial_vm_affinity WHERE trial_id=NEW.trial_id)
            BEGIN
                UPDATE jobs SET vm_id=(SELECT vm_id FROM trial_vm_affinity WHERE trial_id=NEW.trial_id)
                WHERE id=NEW.id AND status='queued';
            END;
        """)


def assign_trial(queue, trial, vm):
    """Pin a trial only at a safe boundary, retaining all completed-job receipts."""
    with queue.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM jobs WHERE trial_id=? AND status='running'", (trial,)).fetchone():
            return False
        previous = db.execute("SELECT vm_id FROM trial_vm_affinity WHERE trial_id=?", (trial,)).fetchone()
        if previous:
            if previous["vm_id"] != vm:
                raise ValueError("trial already has a stable VM assignment")
            return True
        now = time.time()
        db.execute("INSERT INTO trial_vm_affinity VALUES(?,?,?)", (trial, vm, now))
        moved = db.execute("UPDATE jobs SET vm_id=? WHERE trial_id=? AND status='queued'", (vm, trial)).rowcount
        db.execute("INSERT INTO vm_routing_events(at,trial_id,vm_id,queued_jobs) VALUES(?,?,?,?)", (now, trial, vm, moved))
    return True


def snapshot(queue):
    with queue.connect() as db:
        jobs = [dict(row) for row in db.execute("SELECT id,trial_id,vm_id,status,created,started,finished FROM jobs")]
        assignments = {row["trial_id"]: row["vm_id"] for row in db.execute("SELECT * FROM trial_vm_affinity")}
        events = [dict(row) for row in db.execute("SELECT * FROM vm_routing_events ORDER BY sequence")]
    return jobs, assignments, events


def capacity_plan(jobs, current, maximum, *, target_wait=180, now=None):
    if not 1 <= current <= maximum or target_wait <= 0:
        raise ValueError("invalid capacity policy")
    now = time.time() if now is None else now
    pending = [job for job in jobs if job["status"] == "queued"]
    completed = [job for job in jobs if job["finished"] is not None and job["started"] is not None]
    service = statistics.median(job["finished"] - job["started"] for job in completed) if completed else 30
    waits = sorted(job["started"] - job["created"] for job in completed)
    oldest = max((now-job["created"] for job in pending), default=0)
    # Require a backlog at least one minute ago as well as current pressure.
    prior_depth = sum(job["created"] <= now-60 and (job["started"] is None or job["started"] > now-60) for job in jobs)
    desired = current
    if oldest > target_wait and prior_depth >= current*2 and len(pending) >= current*2:
        desired = min(maximum, max(current, math.ceil(len(pending)*service/target_wait)))
    return {"at": now, "queued": len(pending), "oldest_wait_seconds": oldest,
            "queue_depth_60s_ago": prior_depth, "median_service_seconds": service,
            "completed_wait_p90_seconds": waits[int((len(waits)-1)*.9)] if waits else None,
            "current_vms": current, "recommended_vms": desired, "max_vms": maximum,
            "target_queue_wait_seconds": target_wait}


def distribute(queue, vm_ids):
    jobs, assignments, _ = snapshot(queue)
    counts = collections.Counter(job["trial_id"] for job in jobs if job["status"] == "queued")
    running = {job["trial_id"] for job in jobs if job["status"] == "running"}
    loads = collections.Counter(job["vm_id"] for job in jobs if job["status"] in ("queued", "running"))
    loads.update(assignments.values())  # reserve room for each trial's later jobs
    for trial, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        if trial in assignments or trial in running:
            continue
        target = min(vm_ids, key=lambda name: (loads[name], name))
        old = collections.Counter(job["vm_id"] for job in jobs if job["trial_id"] == trial and job["status"] == "queued")
        if assign_trial(queue, trial, target):
            loads.subtract(old);loads[target] += count+1;assignments[trial] = target


def attach(run, specifications, *, max_vms=3, target_wait=180):
    run = Path(run).resolve()
    with (run/'elastic.lock').open('a+') as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest = json.loads((run/'run.json').read_text())
        original = manifest['matrix']['vms'][:manifest['vm_count']]
        new = specifications[:max_vms-len(original)]
        all_vms = [*original, *new]
        if not new or len({vm['id'] for vm in all_vms}) != len(all_vms) or len({vm['host'] for vm in all_vms}) != len(all_vms):
            raise ValueError('additional VMs must have distinct identities and hosts')
        queue = TestQueue(run/'queue.sqlite');install_routes(queue)
        env = manifest['environments'];environments = list(env['by_task'].values()) if 'by_task' in env else [env]
        pool = VMPool(queue, new, run/'elastic-control')
        preflight = {name: driver.prepare(environments) for name, driver in pool.drivers.items()}
        bases = sorted((run/'base-assets').glob('*.tar'))
        if not bases:  # Older runs exported the base only inside each trial.
            bases = sorted((run/'trials').glob('*/base.tar'))
        for name, driver in pool.drivers.items():
            preflight[name]['base_assets'] = [driver.stage_base(base) for base in bases]
        record = {'experiment_id':manifest['experiment_id'], 'started_at':time.time(),
                  'initial_vms':original, 'vms':all_vms, 'preflight':preflight,
                  'overlay_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  'policy':{'max_vms':max_vms,'target_wait_seconds':target_wait,'stable_trial_affinity':True,'move_running_jobs':False},
                  'pressure':[], 'user_requested_keep_running':True}
        record['pressure'].append(capacity_plan(snapshot(queue)[0],len(original),max_vms,target_wait=target_wait))
        pool.start()
        try:
            while True:
                current = json.loads((run/'run.json').read_text())
                if current['status'] in ('Complete','Partial · needs attention','Interrupted'):
                    record['status']='controller_finished';break
                distribute(queue,[vm['id'] for vm in all_vms if vm['id'] not in pool.errors])
                jobs,assignments,events=snapshot(queue)
                record.update(status='attention' if pool.errors else 'active',assignments=assignments,events=events,worker_errors=dict(pool.errors),updated_at=time.time())
                record['pressure'].append(capacity_plan(jobs,len(all_vms),max_vms,target_wait=target_wait))
                path=run/'runtime-topology.json';temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(record,indent=2)+'\n');temporary.replace(path)
                if pool.errors:
                    print('ELASTIC_VM_PARKED',json.dumps(pool.errors),flush=True)
                    # Never complete or reclaim an uncertain remote job. Its VM
                    # remains fenced; the operator can reconnect/reconcile it.
                time.sleep(5)
        finally:
            pool.close();record['finished_at']=time.time()
            path=run/'runtime-topology.json';temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(record,indent=2)+'\n');temporary.replace(path)
        return record


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',required=True);parser.add_argument('--vms',required=True)
    parser.add_argument('--max-vms',type=int,default=3);parser.add_argument('--target-wait',type=float,default=180)
    args=parser.parse_args();inventory=json.loads(Path(args.vms).read_text())
    attach(args.run,inventory['vms'],max_vms=args.max_vms,target_wait=args.target_wait)
