"""One resident interpreter per live delegated agent, across transcript rotations."""
import contextlib
import threading


def delegated_agent():
    from agent.delegation_context import is_delegated_child_context
    from agent.tool_invocation import current_call_context
    context = current_call_context()
    if not is_delegated_child_context() or context is None or context.agent is None:
        return None
    depth = vars(context.agent).get('_delegate_depth', 0)
    return context.agent if isinstance(depth, int) and depth > 0 else None


def remember_owner(owner):
    from agent.tool_invocation import current_call_context
    context = current_call_context()
    if context is None or context.agent is None:
        return owner
    agent = delegated_agent()
    if agent is not None:
        # The transcript ID rotates during compression; the live child does not.
        owner = vars(agent).setdefault('_delegated_kernel_owner', owner)
    vars(context.agent).setdefault('_delegated_kernel_owners', set()).add(owner)
    return owner


def remote_key(owner, env, env_type, task_env_id, sandbox_tools):
    # Reuse within the same remote environment across transcript rotation. A
    # different environment must reset state, even if its backend name matches.
    identity = id(env) if delegated_agent() is not None else task_env_id
    return owner, 'remote', env_type, identity, tuple(sorted(sandbox_tools))


def _retire_other_variants(owner, keep_registry, keep_key):
    from tools.code_kernel import _REGISTRY as local
    from tools.code_kernel_remote import _REGISTRY as remote
    obsolete = []
    # The per-agent slot serializes acquisition across both backends. Registry
    # locks only protect their maps; teardown never holds a registry lock.
    for registry in (local, remote):
        with registry.lock:
            for key, kernel in registry.kernels.items():
                if key[0] == owner and (registry is not keep_registry or key != keep_key):
                    if kernel.attached:
                        return False, False
                    obsolete.append((registry, key, kernel))
    for registry, key, kernel in obsolete:
        registry.discard(key, kernel)
    return True, bool(obsolete)


@contextlib.contextmanager
def kernel_slot(registry, key):
    """Reject overlap; retire an idle backend/config variant before its replacement."""
    agent = delegated_agent()
    if agent is None:
        yield True, False
        return
    lock = vars(agent).setdefault('_delegated_kernel_slot', threading.Lock())
    if not lock.acquire(blocking=False):
        yield False, False
        return
    try:
        yield _retire_other_variants(key[0], registry, key)
    finally:
        lock.release()
