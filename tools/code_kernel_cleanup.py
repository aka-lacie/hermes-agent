"""Retain failed delegated cleanup and its resident allocation until confirmed."""
import contextvars
import logging
import threading

from tools.code_kernel_teardown import KernelTeardownError

_PENDING = {}
_LOCK = threading.Lock()
logger = logging.getLogger(__name__)


def prepare_child_kernel_cleanup(child):
    from tools.code_kernel import shutdown_kernels_for_delegated_agent
    try:
        shutdown_kernels_for_delegated_agent(child)
    except KernelTeardownError as exc:
        error = {'status': 'remote_kernel_still_resident', 'error': str(exc)}
        child._workflow_cleanup_error = error
        lease = vars(child).get('_workflow_admission_lease')
        if lease is not None:
            lease.release_active()
        with _LOCK:
            _PENDING[id(child)] = (child, contextvars.copy_context())
        logger.warning('Delegated kernel cleanup retained: %s', exc)
        return False
    return True


def finish_child_kernels(child):
    lease = vars(child).pop('_workflow_admission_lease', None)
    if lease is not None:
        lease.close()
    vars(child).pop('_workflow_cleanup_error', None)
    with _LOCK:
        _PENDING.pop(id(child), None)


def retry_pending_child_cleanup():
    # Bound each reaper pass. Pop only the retry record, never the remote
    # handle or lease; failed attempts reinsert the same live child at the end.
    with _LOCK:
        pending = [(key, _PENDING.pop(key)) for key in list(_PENDING)[:4]]
    for key, (child, context) in pending:
        try:
            from tools.delegate_tool_child_run import _close_child
            context.run(_close_child, child, "Deferred child resource cleanup failed")
        except Exception:
            logger.exception('Deferred child kernel cleanup failed')
            with _LOCK:
                _PENDING[key] = (child, context)
