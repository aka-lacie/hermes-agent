"""Host-wide workflow child accounting, independent of any plugin instance.

Suspended parents release active work, never their resident allocation. At a
nested resident ceiling fail promptly: waiting cannot free its own ancestors.
"""
from contextlib import contextmanager
import threading


class WorkflowAdmission:
    def __init__(self, active_limit=8, resident_limit=32, queue_limit=256):
        self.active_limit = active_limit
        self.resident_limit = resident_limit
        self.queue_limit = queue_limit
        self.condition = threading.Condition(threading.RLock())
        self.active = self.resident = self.queued = self.pending_items = 0
        self.construct_lock = threading.RLock()
        self.profile_active = {}

    def reserve_items(self, count):
        from agent.workflow_agents import AgentAdmissionError
        with self.condition:
            if self.pending_items + self.queued + count > self.queue_limit:
                raise AgentAdmissionError("Host workflow queue is full; reduce map size or retry later.")
            self.pending_items += count
        return QueuedItems(self, count)

    def active_available(self, profile, profile_limit):
        return self.active < self.active_limit and (profile_limit is None or self.profile_active.get(profile, 0) < profile_limit)

    def change_active(self, profile, delta):
        self.active += delta
        self.profile_active[profile] = self.profile_active.get(profile, 0) + delta
        if not self.profile_active[profile]:
            self.profile_active.pop(profile)

    def acquire(self, check, *, nested=False, active=True, profile=None, profile_limit=None):
        from agent.workflow_agents import AgentAdmissionError
        with self.condition:
            if self.queued + self.pending_items >= self.queue_limit:
                raise AgentAdmissionError("Host workflow queue is full; reduce map size or retry later.")
            self.queued += 1
            try:
                while True:
                    check()
                    if nested and self.resident >= self.resident_limit:
                        raise AgentAdmissionError("Resident child limit reached while ancestors wait; reduce nested fan-out.")
                    if (not active or self.active_available(profile, profile_limit)) and self.resident < self.resident_limit:
                        if active:
                            self.change_active(profile, 1)
                        self.resident += 1
                        return Lease(self, active=active, profile=profile, profile_limit=profile_limit)
                    self.condition.wait(.05)
            finally:
                self.queued -= 1


class QueuedItems:
    def __init__(self, pool, remaining):
        self.pool = pool
        self.remaining = remaining

    def consume(self):
        with self.pool.condition:
            self.remaining -= 1
            self.pool.pending_items -= 1

    def close(self):
        with self.pool.condition:
            self.pool.pending_items -= self.remaining
            self.remaining = 0
            self.pool.condition.notify_all()


class Lease:
    def __init__(self, admission, *, active=True, profile=None, profile_limit=None):
        self.admission = admission
        self.profile, self.profile_limit = profile, profile_limit
        self.active = active
        self.closed = False
        self.suspensions = 0

    def activate(self, check):
        with self.admission.condition:
            while not self.active and not self.closed:
                check()
                if self.admission.active_available(self.profile, self.profile_limit):
                    self.admission.change_active(self.profile, 1)
                    self.active = True
                    return
                self.admission.condition.wait(.05)
            check()

    def release_active(self):
        """A settled worker may retain residency while remote teardown is uncertain."""
        with self.admission.condition:
            if self.active:
                self.admission.change_active(self.profile, -1)
                self.active = False
                self.admission.condition.notify_all()

    def close(self):
        with self.admission.condition:
            if self.closed:
                return
            self.closed = True
            self.admission.resident -= 1
            if self.active:
                self.admission.change_active(self.profile, -1)
            self.active = False
            self.admission.condition.notify_all()

    @contextmanager
    def suspend(self, check):
        pool = self.admission
        with pool.condition:
            self.suspensions += 1
            if self.active:
                self.active = False
                pool.change_active(self.profile, -1)
                pool.condition.notify_all()
        try:
            yield
        finally:
            with pool.condition:
                self.suspensions -= 1
                if not self.closed and not self.suspensions:
                    while not pool.active_available(self.profile, self.profile_limit):
                        check()
                        pool.condition.wait(.05)
                    check()
                    pool.change_active(self.profile, 1)
                    self.active = True


HOST_ADMISSION = WorkflowAdmission()


def inherit_workflow_admission(function):
    """Normal delegate_task descendants share their waiting workflow ancestor's pool."""
    import functools
    import inspect
    signature = inspect.signature(function)

    @functools.wraps(function)
    def call(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        parent = bound.arguments.get("parent_agent")
        request = (vars(parent) if parent is not None else {}).get("_workflow_request")
        explicit = bound.arguments.get("_workflow_request")
        if request is None or explicit is not None:
            return function(*args, **kwargs)
        lease = (vars(parent) if parent is not None else {}).get("_workflow_admission_lease")
        # The model-facing action may return a background dispatch handle. Its
        # children retain residents and inherit cancellation after this scope.
        request.check()
        if lease is None:
            return function(*args, **kwargs)
        with lease.suspend(request.check):
            return function(*args, **kwargs)
    return call


def descendant_request(parent):
    """Reserve only residency during construction; workers acquire active slots."""
    from agent.workflow_agents import WorkflowRequest
    ancestor = (vars(parent) if parent is not None else {}).get("_workflow_request")
    if ancestor is None:
        return None
    request = WorkflowRequest(ancestor.code_context, None, parent, ancestor.cancellation, ancestor.deadline)
    request.lease = HOST_ADMISSION.acquire(request.check, nested=True, active=False, **profile_limits(parent))
    return request


def prepare_child_workflow(parent, explicit, children):
    try:
        return explicit or descendant_request(parent)
    except Exception:
        from tools.delegate_tool_child_run import _close_child, _detach_child
        for _, _, built in children:
            _detach_child(parent, built)
            _close_child(built, "Could not close child after workflow admission failed")
        raise


def close_unattached_workflow(request):
    if request is not None and not request.attached and request.lease is not None:
        request.lease.close()


def profile_limits(parent):
    from pathlib import Path
    from hermes_constants import get_hermes_home
    from tools.delegate_tool import _get_max_concurrent_children
    database = getattr(parent, "_session_db", None)
    path = getattr(database, "db_path", None)
    profile = str(Path(path).resolve().parent) if isinstance(path, (str, Path)) else str(get_hermes_home().resolve())
    return {"profile": profile, "profile_limit": _get_max_concurrent_children()}
