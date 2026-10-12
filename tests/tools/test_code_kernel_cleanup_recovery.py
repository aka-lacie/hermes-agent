"""Transport loss must retain the remote process handle and resident allocation."""
import json
import os
from pathlib import Path
import subprocess
import time
import sys
from types import SimpleNamespace

import psutil
import pytest

from agent.delegation_context import delegated_child_context
from agent.tool_invocation import CallContext, bind_call_context
from agent.workflow_agents_admission import WorkflowAdmission
from tools.code_kernel_cleanup import retry_pending_child_cleanup, _PENDING
from tools.code_kernel_remote import _REMOTE_KERNELS, execute_in_remote_kernel, shutdown_all_remote_kernels
from tools.delegate_tool_child_run import _close_child


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('initial_failure', [None, 'identity', 'launch'])
def test_unreachable_remote_retains_handle_and_lease_until_recovery(tmp_path, initial_failure):
    class Shell:
        reachable = True
        commands = []
        def get_temp_dir(self):
            return str(tmp_path)
        def execute(self, command, cwd=None, timeout=None, stdin_data=None):
            self.commands.append(command)
            if not self.reachable:
                raise ConnectionError('remote unavailable')
            if initial_failure == 'identity' and 'HERMES_KERNEL_IDENTITY:' in command and not hasattr(self, 'lost_identity'):
                self.lost_identity = True
                self.reachable = False
                raise ConnectionError('initial identity reply lost')
            result = subprocess.run(['/bin/sh', '-c', command], cwd=cwd, timeout=timeout,
                                    input=stdin_data, text=True, capture_output=True, check=False)
            if initial_failure == 'launch' and 'nohup' in command and not hasattr(self, 'lost_launch'):
                self.lost_launch = True
                self.reachable = False
                raise ConnectionError('launch succeeded but reply was lost')
            return {'output': result.stdout + result.stderr, 'returncode': result.returncode}

    env = Shell()
    closed = []
    child = SimpleNamespace(session_id='remote-recovery', _delegate_depth=1,
                            close=lambda: closed.append(True))
    pool = WorkflowAdmission(active_limit=1, resident_limit=1)
    lease = child._workflow_admission_lease = pool.acquire(lambda: None)

    def execute(tools=frozenset()):
        with bind_call_context(CallContext(child, child.session_id, 'recovery-call')), delegated_child_context(child.session_id):
            return execute_in_remote_kernel(f'from pathlib import Path; Path({str(tmp_path / "executed")!r}).touch()', env=env, env_type='fixture',
                task_env_id=child.session_id, sandbox_tools=tools, timeout=10, max_tool_calls=10, reset=False)

    try:
        first = execute()
        assert first['status'] == ('error' if initial_failure else 'success'), first
        assert (tmp_path / 'executed').exists() is (initial_failure is None)
        key, kernel = next((key, kernel) for key, kernel in _REMOTE_KERNELS.items()
                           if key[0].endswith('::child::remote-recovery'))
        ready = Path(kernel.kernel_dir) / 'kernel_ready.json'
        end = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < end:
            time.sleep(.01)
        process = psutil.Process(int(json.loads(ready.read_text())['pid']))
        env.reachable = False
        result = execute(frozenset({'terminal'}))
        assert result['status'] == 'error' and 'incomplete_teardown' in result['error']
        assert _REMOTE_KERNELS[key] is kernel and process.is_running()
        assert pool.resident == 1
        _close_child(child, 'unreachable fixture cleanup')
        assert pool.resident == 1 and pool.active == 0 and not lease.closed
        assert child._workflow_admission_lease is lease
        assert _PENDING[id(child)][0] is child and _REMOTE_KERNELS[key] is kernel
        assert child._workflow_cleanup_error['status'] == 'remote_kernel_still_resident'
        env.reachable = True
        retry_pending_child_cleanup()
        assert pool.resident == pool.active == 0 and lease.closed
        assert key not in _REMOTE_KERNELS and id(child) not in _PENDING
        assert not process.is_running() or process.status() == psutil.STATUS_ZOMBIE
        retry_pending_child_cleanup()
        assert pool.resident == 0 and closed == [True]
        assert sum('nohup' in command for command in env.commands) == 1
    finally:
        env.reachable = True
        retry_pending_child_cleanup()
        shutdown_all_remote_kernels()


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('reused', ['root', 'descendant'])
def test_remote_retry_does_not_signal_a_reused_pid(tmp_path, reused):
    from tools.code_kernel_remote import RemoteKernel

    class Shell:
        def execute(self, command, cwd=None, timeout=None):
            result = subprocess.run(['/bin/sh', '-c', command], timeout=timeout,
                                    text=True, capture_output=True, check=False)
            return {'output': result.stdout + result.stderr, 'returncode': result.returncode}

    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    try:
        directory = tmp_path / 'retired'
        directory.mkdir()
        original_pid = process.pid if reused == 'root' else os.getpid()
        if reused == 'descendant':
            (directory / 'kernel_teardown.json').write_text(json.dumps({str(process.pid): 'original-child-birth'}))
        kernel = RemoteKernel(env=Shell(), env_type='fixture', kernel_dir=str(directory),
                              pid=str(original_pid), rpc_token='fixture', owner='retired', pinned=True)
        # The saved identity belongs to the original process. The PID now
        # identifies a different, live process that cleanup must never signal.
        kernel.birth_identity = 'original-process-birth-identity'
        kernel.session_identity = original_pid
        from tools.code_kernel_teardown import KernelTeardownError
        try:
            kernel.kill()
        except KernelTeardownError as error:
            # A reused SID whose ownership cannot be proved stays retained.
            assert 'unconfirmed' in str(error) or 'did not confirm' in str(error)
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)
