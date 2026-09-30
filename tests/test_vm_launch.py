import tempfile
import unittest
from unittest.mock import Mock

from metabench.vm_pool import SSHVM


class WorkerReceiptTest(unittest.TestCase):
    def driver(self, directory):
        return SSHVM({"id": "fixture", "host": "fixture", "root": "/fixture"}, directory)

    def stop(self):
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        return stop

    def test_transient_startup_observation_does_not_fail_a_live_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            driver = self.driver(directory)
            driver.status = Mock(side_effect=[
                {"state": "interrupted", "execution_started_at": None},
                {"state": "running", "execution_started_at": None},
                {"state": "completed", "result": {"score": 100},
                 "execution_started_at": 99, "observed_at": 100},
            ])
            started = Mock()
            self.assertEqual(driver.wait("job", self.stop(), on_started=started), {"score": 100})
            started.assert_called_once()
            self.assertEqual(driver.status.call_count, 3)

    def test_repeated_missing_receipts_fail_without_relaunching_unknown_work(self):
        with tempfile.TemporaryDirectory() as directory:
            driver = self.driver(directory)
            driver.status = Mock(return_value={"state": "interrupted"})
            driver.launch = Mock()
            with self.assertRaisesRegex(RuntimeError, "no completed receipt"):
                driver.wait("job", self.stop())
            self.assertEqual(driver.status.call_count, 5)
            driver.launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
