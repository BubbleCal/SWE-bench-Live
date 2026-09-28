"""Bounded subprocess execution shared by adapters and runtimes."""

import os
import signal
import subprocess
import time


def execute(argv, *, cwd=None, input=None, timeout=60, env=None, merge_stderr=True):
    started = time.monotonic()
    with subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE, text=True, env=env,
                          start_new_session=True) as process:
        timed_out = False
        try:
            output, stderr = process.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            output, stderr = process.communicate()
    return {"returncode": process.returncode, "output": output, "stderr": stderr,
            "timed_out": timed_out, "seconds": time.monotonic() - started}
