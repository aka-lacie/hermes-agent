"""A live delegated child owns one interpreter, regardless of transcript/backend changes."""
import json
import subprocess
import sys
import time
from types import SimpleNamespace

import psutil
import pytest

from agent.delegation_context import delegated_child_context
from agent.tool_invocation import CallContext, bind_call_context
from agent.workflow_agents_admission import WorkflowAdmission
from tools.code_kernel import (_KERNELS, execute_in_session_kernel, shutdown_all_kernels,
                               shutdown_kernels_for_delegated_agent)
from tools.code_kernel_remote import execute_in_remote_kernel, shutdown_all_remote_kernels, _REMOTE_KERNELS
from tools.delegate_tool_child_run import _close_child


@pytest.mark.platforms('posix')
def test_rotated_child_reuses_one_kernel_and_closes_variants_before_lease_release(tmp_path):
    """Real remote interpreter survives rotation, then closes before lease release."""
    class Shell:
        def get_temp_dir(self):
            return str(tmp_path)
        def execute(self, command, cwd=None, timeout=None, stdin_data=None):
            result = subprocess.run(['/bin/sh', '-c', command], cwd=cwd, timeout=timeout,
                                    input=stdin_data, text=True, capture_output=True, check=False)
            return {'output': result.stdout + result.stderr, 'returncode': result.returncode}
    env = Shell()
    child = SimpleNamespace(session_id='rotated', _delegate_depth=1, close=lambda: None)
    sibling = SimpleNamespace(session_id='sibling', _delegate_depth=1)
    pool = WorkflowAdmission(active_limit=1, resident_limit=1)
    child._workflow_admission_lease = pool.acquire(lambda: None)
    pids = []

    def execute(agent, segment, code, *, tools=frozenset(), environment=None):
        with bind_call_context(CallContext(agent, segment, 'rotation-call')), delegated_child_context(segment):
            result = execute_in_remote_kernel(code, env=environment or env, env_type='fixture', task_env_id=segment,
                sandbox_tools=tools, timeout=10, max_tool_calls=10, reset=False)
        assert result['status'] == 'success', result
        return result

    def capture_child_processes():
        pids.extend(int(kernel.pid) for key, kernel in _REMOTE_KERNELS.items()
                    if key[0].endswith(('::child::original', '::child::rotated')))
        assert pids and all(psutil.pid_exists(pid) for pid in pids)

    def running(pid):
        try:
            return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False

    try:
        execute(child, 'original', 'marker = 17')
        assert '17' in execute(child, 'original', 'print(marker)')['stdout']
        execute(child, 'rotated', "assert marker == 17\nmarker = 29")
        assert len([key for key in _REMOTE_KERNELS if key[0].endswith('::child::original')]) == 1
        execute(sibling, 'sibling', 'marker = 41')
        capture_child_processes()
        # Configuration/environment changes must replace this child's kernel,
        # while the sibling's resident interpreter and globals survive.
        changed = execute(child, 'rotated', "assert 'marker' not in globals()",
                          tools=frozenset({'terminal'}))
        assert changed['kernel']['state_reset']
        pids.extend(int(kernel.pid) for key, kernel in _REMOTE_KERNELS.items()
                    if key[0].endswith('::child::original'))
        changed = execute(child, 'rotated', 'marker = 71', environment=Shell())
        assert changed['kernel']['state_reset']
        pids.extend(int(kernel.pid) for key, kernel in _REMOTE_KERNELS.items()
                    if key[0].endswith('::child::original'))
        assert len([key for key in _REMOTE_KERNELS if key[0].endswith('::child::original')]) == 1
        with bind_call_context(CallContext(child, 'rotated', 'local-cell')), delegated_child_context('rotated'):
            local_result = json.loads(execute_in_session_kernel(
                "assert 'marker' not in globals()\nmarker = 83", task_id='rotated', mode='strict',
                child_python=sys.executable, child_cwd=str(tmp_path), sandbox_tools=frozenset(),
                timeout=10, max_tool_calls=10, reset=False, is_interrupted=lambda: False))
        assert local_result['status'] == 'success', local_result
        assert local_result['kernel']['state_reset']
        assert not any(key[0].endswith('::child::original') for key in _REMOTE_KERNELS)
        local_kernel = next(kernel for key, kernel in _KERNELS.items()
                            if key[0].endswith('::child::original'))
        changed = execute(child, 'rotated', "assert 'marker' not in globals()")
        assert changed['kernel']['state_reset']
        assert local_kernel.proc.poll() is not None
        assert not any(key[0].endswith('::child::original') for key in _KERNELS)
        pids.extend(int(kernel.pid) for key, kernel in _REMOTE_KERNELS.items()
                    if key[0].endswith('::child::original'))
        assert '41' in execute(sibling, 'sibling', 'print(marker)')['stdout']
        shutdown_kernels_for_delegated_agent(child)  # Timeout cleanup while worker still unwinds.
        assert pool.resident == 1
        # A stale thread may use its earlier segment again before it settles.
        # Recreating an already-seen owner must not be forgotten by early cleanup.
        execute(child, 'original', 'marker = 53')
        pids.extend(int(kernel.pid) for key, kernel in _REMOTE_KERNELS.items() if key[0].endswith('::child::original'))
        release = child._workflow_admission_lease.close
        def release_after_all_kernels_close():
            assert not any(key[0].endswith(('::child::original', '::child::rotated')) for key in _REMOTE_KERNELS)
            release()
        child._workflow_admission_lease.close = release_after_all_kernels_close
        _close_child(child, 'remote rotation fixture cleanup')
        assert pool.active == pool.resident == 0
        end = time.monotonic() + 5
        while any(running(pid) for pid in pids) and time.monotonic() < end:
            time.sleep(.01)
        assert not any(running(pid) for pid in pids)
        assert '41' in execute(sibling, 'sibling', 'print(marker)')['stdout']
    finally:
        shutdown_all_remote_kernels()
        shutdown_all_kernels()
