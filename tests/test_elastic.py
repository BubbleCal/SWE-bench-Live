import tempfile
import threading
import time
import unittest
from pathlib import Path

from metabench.elastic import assign_trial,capacity_plan,distribute,install_routes,snapshot
from metabench.queue import TestQueue
from metabench.vm_pool import VMPool


class ElasticTest(unittest.TestCase):
    def test_original_controller_observes_jobs_finished_by_attached_workers(self):
        active={};lock=threading.Lock();peak=[0];started=threading.Event();added_started=threading.Event();release=threading.Event()
        class Driver:
            def __init__(self,spec,directory):self.id=spec['id']
            def launch(self,job):
                with lock:
                    active[self.id]=active.get(self.id,0)+1
                    if active[self.id]!=1:raise AssertionError('same VM overlap')
                    peak[0]=max(peak[0],sum(active.values()))
                if self.id=='vm1':started.set()
                else:added_started.set()
            def wait(self,identity,stop):
                if self.id=='vm1':release.wait(10)
                time.sleep(.03)
                with lock:active[self.id]-=1
                return {'actual_vm':self.id}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);queue=TestQueue(root/'queue.sqlite')
            identities=[queue.enqueue(str(i),'vm1','public',{}) for i in range(12)]
            original=VMPool(queue,[{'id':'vm1'}],root/'old',driver_factory=Driver).start()
            self.assertTrue(started.wait(2));install_routes(queue)
            added=VMPool(queue,[{'id':'vm2'},{'id':'vm3'}],root/'new',driver_factory=Driver).start()
            try:
                distribute(queue,['vm1','vm2','vm3'])
                self.assertTrue(added_started.wait(5))
                release.set()
                results=[original.await_job(identity)['result'] for identity in identities]
                self.assertEqual({r['actual_vm'] for r in results},{'vm1','vm2','vm3'})
                self.assertGreater(peak[0],1)
            finally:release.set();original.close();added.close()

    def test_running_jobs_are_fenced_and_future_inserts_keep_affinity(self):
        with tempfile.TemporaryDirectory() as directory:
            queue=TestQueue(Path(directory)/'queue.sqlite');install_routes(queue)
            first=queue.enqueue('one','vm1','public',{'patch':'a'})
            queue.claim('vm1','owner')
            second=queue.enqueue('one','vm1','public',{'patch':'b'})
            self.assertFalse(assign_trial(queue,'one','vm2'))
            self.assertEqual(queue.get(second)['vm_id'],'vm1')
            queue.finish(first,{'score':100},owner='owner')
            self.assertTrue(assign_trial(queue,'one','vm2'))
            third=queue.enqueue('one','vm1','verify',{'patch':'c'})
            self.assertEqual(queue.get(first)['vm_id'],'vm1')
            self.assertEqual(queue.get(second)['vm_id'],'vm2')
            self.assertEqual(queue.get(third)['vm_id'],'vm2')
            # A later identical checkpoint still uses the original controller's
            # VM argument; its already-routed immutable job remains idempotent.
            self.assertEqual(queue.enqueue('one','vm1','verify',{'patch':'c'},job_id=third),third)
            with self.assertRaises(ValueError):
                queue.enqueue('one','vm1','verify',{'patch':'changed'},job_id=third)
            self.assertIsNone(queue.claim('vm1','old-controller'))
            self.assertEqual(queue.claim('vm2','new-controller')['id'],second)
            with self.assertRaises(ValueError):
                queue.finish(second,{},owner='old-controller')
            queue.finish(second,{},owner='new-controller')
            with self.assertRaises(ValueError):assign_trial(queue,'one','vm3')

    def test_pressure_requires_sustained_backlog_and_respects_ceiling(self):
        now=time.time();jobs=[]
        for i in range(4):jobs.append(dict(created=now-800,started=now-500+i*50,finished=now-450+i*50,status='completed'))
        jobs.extend(dict(created=now-400,started=None,finished=None,status='queued') for _ in range(20))
        self.assertEqual(capacity_plan(jobs,1,3,now=now)['recommended_vms'],3)
        self.assertEqual(capacity_plan(jobs,2,3,now=now)['recommended_vms'],3)
        fresh=[{**job,'created':now} if job['status']=='queued' else job for job in jobs]
        self.assertEqual(capacity_plan(fresh,1,3,now=now)['recommended_vms'],1)

    def test_pending_trials_distribute_without_changing_payloads_or_active_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            queue=TestQueue(Path(directory)/'queue.sqlite');install_routes(queue)
            ids=[queue.enqueue(str(i),'vm1','public',{'index':i}) for i in range(9)]
            active=queue.claim('vm1','owner');distribute(queue,['vm1','vm2','vm3'])
            jobs,affinity,events=snapshot(queue)
            self.assertEqual(queue.get(active['id'])['owner'],'owner')
            self.assertNotIn(active['trial_id'],affinity)
            self.assertEqual({job['vm_id'] for job in jobs},{'vm1','vm2','vm3'})
            for i,identity in enumerate(ids):self.assertEqual(queue.get(identity)['payload'],{'index':i})
            queue.finish(active['id'],{},owner='owner')
            queue.enqueue(active['trial_id'],'vm1','verify',{})
            distribute(queue,['vm1','vm2','vm3'])
            self.assertIn(active['trial_id'],snapshot(queue)[1])
