import os
import signal
import subprocess
import contextlib
from typing import Protocol
from .driver import LaunchSpec

class ExecutionProvider(Protocol):
    def start(self, spec: LaunchSpec): ...
    def terminate(self, proc): ...

class LocalProcessProvider:
    """Process execution in the current host/container; NOT a per-session sandbox."""
    def start(self, spec):
        return subprocess.Popen(spec.args,cwd=spec.cwd,env=spec.env,stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,stderr=subprocess.STDOUT,start_new_session=os.name!='nt')

    def terminate(self, proc):
        if proc.poll() is not None:
            return
        if os.name == 'nt':
            subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'], capture_output=True)
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)

