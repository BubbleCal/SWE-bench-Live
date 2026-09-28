import os
import tempfile
import unittest
from pathlib import Path

from metabench.checkouts import git
from metabench.verification import append_to_index, validate_append


class VerificationTest(unittest.TestCase):
    def test_preserves_candidate_bytes_and_tests_in_alternate_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);repo=root/'repo';git('init','-q',repo)
            git('config','user.name','test',cwd=repo);git('config','user.email','test@localhost',cwd=repo)
            candidate=b'fn implementation() {}\r\nmod tests { /* candidate tests */ }\r\n'
            (repo/'lib.rs').write_bytes(candidate);git('add','.',cwd=repo);git('commit','-qm','base',cwd=repo)
            env={**os.environ,'GIT_INDEX_FILE':str(root/'alternate-index')}
            import subprocess
            subprocess.run(['git','-C',str(repo),'read-tree','HEAD'],env=env,check=True)
            module={'lib.rs':{'source':'mod __metabench_hidden {}','namespace':'__metabench_hidden'}}
            append_to_index(repo,module,env)
            result=subprocess.check_output(['git','-C',str(repo),'show',':lib.rs'],env=env)
            self.assertEqual(result,candidate+b'\n\nmod __metabench_hidden {}\n')
            self.assertEqual((repo/'lib.rs').read_bytes(),candidate)
            with self.assertRaisesRegex(ValueError,'reserved'):
                append_to_index(repo,module,env)

    def test_rejects_unsafe_paths(self):
        for path in ('../secret','/secret','.git/config','src/../../secret'):
            with self.assertRaises(ValueError):
                validate_append({path:{'source':'test','namespace':'hidden'}})
