import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from metabench.checkouts import TrialCheckout, git
from metabench.vm_worker import CachedWorkspace


@unittest.skipUnless(os.environ.get('METABENCH_TEST_IMAGE'),'set METABENCH_TEST_IMAGE for Docker integration')
class PersistentDockerTest(unittest.TestCase):
    def test_exact_commit_private_lanes_and_unchanged_source_mtimes(self):
        containers=[]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);repo=root/'repo';git('init','-q',repo)
            git('config','user.name','test',cwd=repo);git('config','user.email','test@localhost',cwd=repo)
            (repo/'code.py').write_text('value = 1\n');git('add','.',cwd=repo);git('commit','-qm','base',cwd=repo)
            base=git('rev-parse','HEAD',cwd=repo);checkout=TrialCheckout(repo,base,root/'checkout')
            archive=root/'base.tar';checkout.base_archive(archive)
            image=subprocess.check_output(['docker','image','inspect',os.environ['METABENCH_TEST_IMAGE'],'--format','{{.Id}}'],text=True).strip()
            request={'trial_id':hashlib.sha256(str(root).encode()).hexdigest()[:24],
                     'base_archive':str(archive),'base_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
                     'environment':{'image':image,'cpus':1,'memory':'1g'}}
            try:
                public=CachedWorkspace(root/'vm',request,'public');containers.append(public.container)
                self.assertEqual(public.base,base)
                public.prepare(['']);stamp=(public.root/'code.py').stat().st_mtime_ns
                run=public.command("mkdir -p /build-cache; printf warm > /build-cache/marker; python -c 'import code'",30)
                self.assertEqual(run['returncode'],0,run)
                self.assertEqual(public.restore_source_ownership()['returncode'],0)
                subprocess.run(['docker','stop','--time','1',public.container],check=True,stdout=subprocess.DEVNULL)
                resumed=CachedWorkspace(root/'vm',request,'public');resumed.prepare([''])
                self.assertTrue(resumed.identity['source_reused'])
                self.assertEqual(stamp,(public.root/'code.py').stat().st_mtime_ns)
                self.assertEqual(resumed.command('test -f /build-cache/marker',30)['returncode'],0)
                changed='diff --git a/notes.txt b/notes.txt\nnew file mode 100644\n--- /dev/null\n+++ b/notes.txt\n@@ -0,0 +1 @@\n+documentation only\n'
                resumed.prepare([changed]);self.assertEqual(stamp,(public.root/'code.py').stat().st_mtime_ns)
                verify=CachedWorkspace(root/'vm',request,'verify');containers.append(verify.container)
                secret='diff --git a/secret.txt b/secret.txt\nnew file mode 100644\n--- /dev/null\n+++ b/secret.txt\n@@ -0,0 +1 @@\n+private verification data\n'
                verify.prepare([secret]);blob=git('rev-parse',':secret.txt',cwd=verify.root)
                absent=subprocess.run(['git','-C',str(public.root),'cat-file','-e',blob],capture_output=True)
                self.assertNotEqual(absent.returncode,0)
                self.assertEqual(verify.command('test ! -e /build-cache/marker',30)['returncode'],0)
                self.assertNotEqual(public.seed,verify.seed)
            finally:
                for name in containers:subprocess.run(['docker','rm','-f',name],check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


if __name__=='__main__':unittest.main()
