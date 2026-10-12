"""Remote teardown owns subprocesses even after their interpreter has exited."""
import os
import signal
import subprocess
import time
from types import SimpleNamespace

import psutil
import pytest

from agent.delegation_context import delegated_child_context
from agent.tool_invocation import CallContext, bind_call_context
from tools.code_kernel_remote import _REMOTE_KERNELS, execute_in_remote_kernel, shutdown_all_remote_kernels
from tools.code_kernel_teardown import confirmed_remote_stop


@pytest.mark.platforms('posix')
@pytest.mark.live_system_guard_bypass  # The fixture must reap its deliberately reparented subprocess.
@pytest.mark.parametrize('identity_inherited', [True, False])
def test_remote_cell_exit_handles_subprocess_after_runner_exit(tmp_path, monkeypatch, identity_inherited):
    class Shell:
        def get_temp_dir(self):
            return str(tmp_path)
        def execute(self, command, cwd=None, timeout=None, stdin_data=None):
            result = subprocess.run(['/bin/sh', '-c', command], cwd=cwd, timeout=timeout,
                                    input=stdin_data, text=True, capture_output=True, check=False)
            return {'output': result.stdout + result.stderr, 'returncode': result.returncode}

    def alive(pid):
        try:
            return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False

    def after_runner_exit(kernel):
        end = time.monotonic() + 3
        while alive(int(kernel.pid)) and time.monotonic() < end:
            time.sleep(.01)
        assert not alive(int(kernel.pid)), 'fixture requires the runner to exit before teardown discovery'
        confirmed_remote_stop(kernel)

    monkeypatch.setattr('tools.code_kernel_teardown.confirmed_remote_stop', after_runner_exit)
    ready = tmp_path / 'owned-ready'
    child_source = ('import os, signal, time; from pathlib import Path; '
                    'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                    f'Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(60)')
    source = ('import subprocess, sys, time\nfrom pathlib import Path\n'
              f'owned = subprocess.Popen([sys.executable, "-c", {child_source!r}], env={None if identity_inherited else {}!r})\n'
              f'while not Path({str(ready)!r}).exists(): time.sleep(.01)\n'
              'print(owned.pid)\nsys.exit(0)\n')
    child = SimpleNamespace(session_id='exited-remote-runner', _delegate_depth=1)
    try:
        with bind_call_context(CallContext(child, child.session_id, 'exit-cell')), delegated_child_context(child.session_id):
            result = execute_in_remote_kernel(source, env=Shell(), env_type='fixture', task_env_id=child.session_id,
                sandbox_tools=frozenset(), timeout=10, max_tool_calls=10, reset=False)
        if identity_inherited:
            assert result['status'] == 'success', result
            assert result['kernel']['ended']
            assert not alive(int(ready.read_text())), 'owned process survived its exited runner'
        else:
            assert result['status'] == 'error' and result['incomplete_teardown'], result
            assert alive(int(ready.read_text()))
            assert any(key[0].endswith('::child::' + child.session_id) for key in _REMOTE_KERNELS)
    finally:
        if ready.exists() and alive(int(ready.read_text())):
            os.kill(int(ready.read_text()), signal.SIGKILL)
        shutdown_all_remote_kernels()
