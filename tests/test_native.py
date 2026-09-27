import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError

from metabench.checkouts import TrialCheckout, git, trial_id
from metabench.native_cli import command, parse_events
from metabench.queue import TestQueue
from metabench.test_mcp import request
from metabench.test_service import TestService
from metabench.vm_pool import VMPool


class NativeTest(unittest.TestCase):
    def test_each_configuration_and_issue_has_private_persistent_git_objects(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);repo=root/'source';git('init','-q',repo)
            git('config','user.name','test',cwd=repo);git('config','user.email','test@localhost',cwd=repo)
            (repo/'code.txt').write_text('base');git('add','.',cwd=repo);git('commit','-qm','base',cwd=repo)
            base=git('rev-parse','HEAD',cwd=repo)
            (repo/'secret').write_text('future');git('add','.',cwd=repo);git('commit','-qm','future',cwd=repo)
            future=git('rev-parse','HEAD',cwd=repo)
            one=TrialCheckout(repo,base,root/'one');two=TrialCheckout(repo,base,root/'two')
            self.assertEqual(git('rev-parse','HEAD',cwd=one.root),base)
            self.assertEqual(git('rev-list','--all','--count',cwd=one.root),'1')
            self.assertFalse((one.root/'secret').exists())
            self.assertNotEqual(git('rev-parse','--git-common-dir',cwd=one.root),git('rev-parse','--git-common-dir',cwd=two.root))
            (one.root/'code.txt').write_text('candidate')
            self.assertEqual((two.root/'code.txt').read_text(),'base')
            self.assertIn('+candidate',one.snapshot())
            self.assertEqual(TrialCheckout(repo,base,root/'one').snapshot(),one.snapshot())
            with self.assertRaises(ValueError):TrialCheckout(repo,future,root/'one')
            t={'instance_id':'a','base_commit':base}
            self.assertNotEqual(trial_id({'reasoning':'high'},t),trial_id({'reasoning':'max'},t))
            self.assertNotEqual(trial_id({},t),trial_id({},{**t,'instance_id':'b'}))

    def test_native_cli_keeps_tools_and_resumes_an_explicit_session(self):
        with tempfile.TemporaryDirectory() as temp:
            mcp=Path(temp)/'mcp.json';mcp.write_text(json.dumps({'mcpServers':{'bench':{'command':'python','args':['bridge.py']}}}))
            for provider in ('codex','claude'):
                spec={'provider':provider,'model':'chosen-model','reasoning':'high'}
                first=command(spec,None,mcp);second=command(spec,'session-uuid',mcp)
                for forbidden in ('--ephemeral','--max-turns','--output-schema','--json-schema','--no-session-persistence','--last','--continue'):
                    self.assertNotIn(forbidden,first+second)
                self.assertIn('session-uuid',second)
                self.assertIn('chosen-model',first)
                if provider=='codex':self.assertIn('mcp_servers.bench.tools.run_tests.approval_mode="approve"',first)
                else:
                    self.assertNotIn('--safe-mode',first)
                    self.assertIn('--restricted',first)
            self.assertIn('Bash',command({'provider':'claude','model':'m','reasoning':'max'},None,mcp)[command({'provider':'claude','model':'m','reasoning':'max'},None,mcp).index('--allowedTools')+1])

    def test_native_events_accept_multiple_tools_and_preserve_unknown_usage(self):
        events=[{'type':'thread.started','thread_id':'specific'},
                {'type':'item.completed','item':{'type':'command_execution'}},
                {'type':'item.completed','item':{'type':'file_change'}},
                {'type':'turn.completed','usage':{'input_tokens':100,'cached_input_tokens':70,'output_tokens':20}}]
        result=parse_events('codex',events,'m')
        self.assertEqual(result['status'],'submitted');self.assertEqual(result['provider']['tool_calls'],2)
        self.assertEqual(result['usage']['total_tokens'],120)
        self.assertIsNone(result['usage']['reasoning_output_tokens'])
        with self.assertRaises(ValueError):parse_events('codex',events,'m','different')
        self.assertEqual(parse_events('codex',events[:-1],'m')['status'],'agent_error')
        second=[*events[:-1],{'type':'turn.completed','usage':{'input_tokens':160,'cached_input_tokens':110,'output_tokens':35}}]
        resumed=parse_events('codex',second,'m','specific',result['provider']['session_usage'])
        self.assertEqual(resumed['usage']['total_tokens'],75)
        self.assertEqual(resumed['usage']['cached_input_tokens'],40)

    def test_claude_uses_invocation_result_not_cumulative_cost_or_partial_stream_usage(self):
        events=[{'type':'system','subtype':'init','model':'m','session_id':'specific'},
                {'type':'assistant','message':{'id':'a','model':'m','usage':{'input_tokens':1,'output_tokens':2}}},
                {'type':'assistant','message':{'id':'b','model':'m','usage':{'input_tokens':1,'output_tokens':3}}},
                {'type':'result','subtype':'success','is_error':False,'session_id':'specific',
                 'usage':{'input_tokens':2,'cache_read_input_tokens':100,'cache_creation_input_tokens':10,
                          'output_tokens':80,'output_tokens_details':{'thinking_tokens':50},'iterations':[{'type':'message'}]},
                 'modelUsage':{'m':{'inputTokens':9999,'outputTokens':9999}},'total_cost_usd':123}]
        result=parse_events('claude',events,'m','specific')
        self.assertEqual(result['usage']['total_tokens'],192)
        self.assertEqual(result['usage']['reasoning_output_tokens'],50)
        self.assertIsNone(result['usage']['cost_usd'])

    def test_queue_serializes_each_vm_and_parallelizes_distinct_vms(self):
        lock=threading.Lock();active={};peak={};overlap=[];seen=[];overall=[0,0]
        class Driver:
            def __init__(self,spec,control):self.id=spec['id']
            def launch(self,job):
                with lock:
                    active[self.id]=active.get(self.id,0)+1;peak[self.id]=max(peak.get(self.id,0),active[self.id])
                    overall[0]+=1;overall[1]=max(overall[1],overall[0]);seen.append(job['id'])
                time.sleep(.04)
            def wait(self,identity,stop):
                with lock:active[self.id]-=1;overall[0]-=1
                return {'status':'ok'}
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);queue=TestQueue(root/'queue.sqlite')
            ids=[queue.enqueue('trial','vm'+str(i%2),'public',{'index':i}) for i in range(12)]
            pool=VMPool(queue,[{'id':'vm0'},{'id':'vm1'}],root/'control',driver_factory=Driver).start()
            # A second coordinator must not reclaim an active local owner.
            other=VMPool(TestQueue(root/'queue.sqlite'),[{'id':'vm0'},{'id':'vm1'}],root/'other',driver_factory=Driver).start()
            try:
                for identity in ids:self.assertEqual(pool.await_job(identity)['status'],'completed')
                self.assertEqual(peak,{'vm0':1,'vm1':1});self.assertEqual(overall[1],2)
                self.assertEqual(len(seen),len(set(seen)))
            finally:pool.close();other.close()
            reopened=TestQueue(root/'queue.sqlite');self.assertTrue(all(reopened.get(i)['status']=='completed' for i in ids))
            identity=reopened.enqueue('trial','vm0','public',{})
            self.assertIsNotNone(reopened.claim('vm0','old-owner'))
            self.assertIsNone(reopened.claim('vm0','new-owner'))
            with self.assertRaises(ValueError):reopened.finish(identity,{},owner='new-owner')
            with self.assertRaises(ValueError):VMPool(queue,[{'id':'vm0'}],root,vm_count=2,driver_factory=Driver)

    def test_public_gateway_cannot_read_another_trial_or_hidden_grades(self):
        class Checkout:
            def snapshot(self):return 'candidate patch'
        class Pool:errors={}
        with tempfile.TemporaryDirectory() as temp:
            queue=TestQueue(Path(temp)/'queue.sqlite');service=TestService(queue,Pool())
            try:
                token=service.register('one',Checkout(),'base.tar',{},'vm')
                other=service.register('two',Checkout(),'base.tar',{},'vm')
                public=request(service.url+'/test',token,{'command':'pytest'})['job_id']
                private=queue.enqueue('one','vm','verify',{'task':'private'})
                for identity,cap in ((public,other),(private,token)):
                    with self.assertRaises(HTTPError) as error:request(service.url+'/jobs/'+identity,cap)
                    self.assertEqual(error.exception.code,403);error.exception.close()
                service.seal('one')
                with self.assertRaises(HTTPError) as error:request(service.url+'/test',token,{'command':'pytest'})
                self.assertEqual(error.exception.code,409);error.exception.close()
            finally:service.close()


if __name__=='__main__':unittest.main()
