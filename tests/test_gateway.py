import concurrent.futures
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

from metabench.queue import TestQueue
from metabench.test_service import TestService
from metabench.test_mcp import OPENER, request


class GatewayTest(unittest.TestCase):
    def test_concurrent_poll_bursts_and_lost_submission_receipts(self):
        class Checkout:
            calls=0
            def snapshot(self):
                self.calls+=1
                return f'candidate snapshot {self.calls}'
        class Pool:errors={}
        with tempfile.TemporaryDirectory() as directory:
            queue=TestQueue(Path(directory)/'queue.sqlite');service=TestService(queue,Pool());checkout=Checkout()
            try:
                token=service.register('trial',checkout,'base.tar',{},'vm')
                original=OPENER.open;lost=[False]
                def lose_first_reply(*args,**kwargs):
                    response=original(*args,**kwargs)
                    if not lost[0]:
                        lost[0]=True;response.close()
                        raise ConnectionResetError('response lost after server accepted the job')
                    return response
                with patch.object(OPENER,'open',side_effect=lose_first_reply):
                    identity=request(service.url+'/test',token,{'command':'pytest'})['job_id']
                self.assertEqual(checkout.calls,1)
                self.assertEqual(len(queue.for_trial('trial')),1)
                self.assertEqual(queue.get(identity)['payload']['patch'],'candidate snapshot 1')
                barrier=threading.Barrier(64)
                def poll(_):
                    for repeat in range(8):
                        barrier.wait(timeout=15)
                        # No client retries: the backlog itself must handle bursts.
                        with urlopen(Request(service.url+'/jobs/'+identity,
                                     headers={'Authorization':'Bearer '+token}),timeout=10) as response:
                            self.assertEqual(json.load(response)['job_id'],identity)
                with concurrent.futures.ThreadPoolExecutor(max_workers=64) as executor:
                    list(executor.map(poll,range(64)))
            finally:service.close()
