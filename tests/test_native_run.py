import copy
import json
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from metabench.checkouts import git
from metabench.evaluate import evaluate, validate
from metabench.native_run import run_matrix
from metabench.schema import freeze, task_fingerprint
from metabench.test_mcp import request
from metabench.usage import add, normalize


class NativeMatrixTest(unittest.TestCase):
    def test_parallel_issues_native_session_continuations_grading_and_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);repo=root/'repo';git('init','-q',repo)
            git('config','user.name','test',cwd=repo);git('config','user.email','test@localhost',cwd=repo)
            (repo/'calc.py').write_text('def add(a,b):\n    return a-b\n')
            git('add','.',cwd=repo);git('commit','-qm','base',cwd=repo);base=git('rev-parse','HEAD',cwd=repo)
            (repo/'calc.py').write_text('def add(a,b):\n    return a+b\n');fix=git('diff',cwd=repo)+'\n'
            env={'image':'sha256:'+'a'*64}
            task={'instance_id':'issue-a','repo':str(repo),'base_commit':base,'problem_statement':'Fix addition.',
                  'patch':fix,'test_patch':'','weights':{'correctness':1},'checks':[
                   {'id':'sum','dimension':'correctness','critical':True,
                    'command':'python3 -c \'from calc import add; assert add(2,3)==5; print("HIDDEN_MARKER")\'',
                    'success_pattern':'^HIDDEN_MARKER$','failure_pattern':'AssertionError'}]}
            task=validate(repo,task,env,root/'validation',trusted_local=True,repeats=2)
            self.assertTrue(task['validation']['passed'])
            task['validation']['environment']={'backend':'docker','score_eligible':True}
            second=copy.deepcopy(task);second['instance_id']='issue-b';second['validation']['task_hash']=task_fingerprint(second)
            suite=freeze([task,second]);matrix={'agents':[
                {'provider':'codex','model':'control-a','reasoning':'high'},
                {'provider':'claude','model':'control-b','reasoning':'high'}],
                'vms':[{'id':'vm-a'},{'id':'vm-b'}],'max_rounds':2}
            control_lock=threading.Lock();calls=[];sessions={};running=[0,0];prepared=[]
            increment=normalize({'input_tokens':10,'cached_input_tokens':4,'cache_write_input_tokens':0,
                                 'output_tokens':5,'reasoning_output_tokens':2})

            class Driver:
                def __init__(self,spec,directory):self.id=spec['id'];self.jobs={}
                def prepare(self,environments):prepared.append(self.id);return {'vm_id':self.id}
                def launch(self,job):self.jobs[job['id']]=job
                def wait(self,identity,stop):
                    job=self.jobs[identity]
                    if job['kind']=='public':return {'execution':{'returncode':0,'output':'PUBLIC ONLY'}}
                    return evaluate(repo,job['payload']['task'],job['payload']['patch'],{},trusted_local=True)

            def native(spec,worktree,prompt,out,**options):
                self.assertEqual(set(prepared),{'vm-a','vm-b'})
                self.assertNotIn('HIDDEN_MARKER',prompt)
                self.assertNotIn('critical_pass',prompt)
                with control_lock:
                    running[0]+=1;running[1]=max(running);calls.append((str(worktree),options['session_id']))
                try:
                    time.sleep(.03)
                    if options['session_id'] is None:
                        sessions[str(worktree)]=str(uuid.uuid4())
                        self.assertIsNone(options['previous_usage'])
                    else:
                        self.assertEqual(options['session_id'],sessions[str(worktree)])
                        if spec['provider']=='codex':self.assertEqual(options['previous_usage'],increment)
                        (worktree/'calc.py').write_text('def add(a,b):\n    return a+b\n')
                    config=json.loads(Path(options['mcp_path']).read_text())['mcpServers']['bench']['args']
                    url=config[config.index('--url')+1];token=Path(config[config.index('--token-file')+1]).read_text()
                    job=request(url+'/test',token,{'command':'public check'})['job_id']
                    while request(url+'/jobs/'+job,token)['status']!='completed':time.sleep(.01)
                    Path(out).mkdir()
                    cumulative=increment if options['session_id'] is None else add(increment,increment)
                    return {'session_id':sessions[str(worktree)],'usage':increment,'status':'submitted','seconds':.1,
                            'provider':{'session_usage':cumulative} if spec['provider']=='codex' else {}}
                finally:
                    with control_lock:running[0]-=1

            out=root/'run'
            with patch('metabench.native_run.cli_identity',return_value={'version':'control'}):
                rows=run_matrix(suite,repo,env,matrix,out,parallel=4,driver_factory=Driver,agent_runner=native)
                self.assertEqual(len(rows),8);self.assertEqual(len(sessions),4);self.assertGreater(running[1],1)
                for step in (1,2):self.assertEqual({r['score'] for r in rows if r['step']==step},{0 if step==1 else 100})
                self.assertEqual({r['usage']['total_tokens'] for r in rows if r['step']==2},{30})
                self.assertTrue((out/'report.html').exists())
                resumed=run_matrix(suite,repo,env,matrix,out,parallel=4,resume=True,driver_factory=Driver,agent_runner=native)
                self.assertEqual(len(calls),8);self.assertEqual(len(resumed),8)
                with self.assertRaisesRegex(ValueError,'configuration changed'):
                    run_matrix(suite,repo,env,matrix,out,parallel=1,resume=True,driver_factory=Driver,agent_runner=native)


if __name__=='__main__':unittest.main()
