"""Fail-closed teardown for resident delegated interpreters."""
import functools
import json
import shlex


class KernelTeardownError(RuntimeError):
    pass


_IDENTITY_SOURCE = """def identity(pid):
    from pathlib import Path
    try:
        stat = Path('/proc/%s/stat' % pid).read_text()
        return 'proc:' + stat.rsplit(')', 1)[1].split()[19]
    except FileNotFoundError:
        pass
    state = subprocess.run(['ps', '-o', 'lstart=', '-p', str(pid)], capture_output=True, text=True, timeout=2)
    if state.returncode not in (0, 1):
        raise RuntimeError('cannot verify remote process identity')
    return 'ps:' + state.stdout.strip() if state.stdout.strip() else None
"""


REMOTE_READY_SOURCE = _IDENTITY_SOURCE + """
def publish_ready():
    if os.getsid(0) != os.getpid():
        os.setsid()
    record = {'pid': str(os.getpid()), 'identity': identity(os.getpid()), 'sid': os.getsid(0)}
    target = os.path.join(KDIR, 'kernel_ready.json')
    fd = os.open(target + '.tmp', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(record, stream)
    os.replace(target + '.tmp', target)
"""


def capture_remote_identity(kernel):
    source = ("import json, time\nfrom pathlib import Path\n" +
              f"path = Path({kernel.kernel_dir!r}) / 'kernel_ready.json'\n" +
              "end = time.monotonic() + 2\n" +
              "while not path.exists() and time.monotonic() < end: time.sleep(.05)\n" +
              "print('HERMES_KERNEL_IDENTITY:' + path.read_text())")
    output = kernel.sh('python3 -c ' + shlex.quote(source), timeout=5)
    line = next((line.partition(':')[2] for line in output.splitlines()
                 if line.startswith('HERMES_KERNEL_IDENTITY:')), None)
    if line is None:
        return None
    record = json.loads(line)
    pid, identity = str(record['pid']), record['identity']
    if not pid.isdigit() or not isinstance(identity, str) or not identity:
        raise ValueError('invalid remote kernel startup receipt')
    if kernel.pid and kernel.pid != pid:
        raise ValueError('remote startup receipt does not match launch PID')
    kernel.pid = pid
    kernel.session_identity = int(record.get("sid", pid))
    return identity



_STOP_TREE_SOURCE = """
root_pid = ROOT_PID
root_birth = ROOT_BIRTH
owned_session = SESSION_IDENTITY
if root_birth is None:
    raise RuntimeError('remote birth identity was not captured; teardown is unconfirmed')
snapshot = Path(KERNEL_DIRECTORY) / 'kernel_teardown.json'
records = json.loads(snapshot.read_text()) if snapshot.exists() else {}
records[str(root_pid)] = root_birth

def running(pid, birth):
    if identity(pid) != birth:
        return False
    state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True, timeout=2)
    if state.returncode not in (0, 1):
        raise RuntimeError('cannot verify remote process state')
    return bool(state.stdout.strip()) and not state.stdout.lstrip().startswith('Z')

# Session isolation keeps children attributable after their parent exits.
# A retained identity or inherited private directory proves that a numeric SID
# still belongs to this kernel; a reused/unverifiable SID is never signaled.
result = subprocess.run(['ps', '-eo', 'pid=,ppid=,sid='], capture_output=True, text=True, timeout=2)
if result.returncode:
    raise RuntimeError('cannot enumerate owned remote subprocesses')
rows = [tuple(map(int, line.split())) for line in result.stdout.splitlines()]
members = [pid for pid, parent, sid in rows
           if sid == owned_session and running(pid, identity(pid))]
owned = running(root_pid, root_birth)
for pid, birth in list(records.items()):
    if running(int(pid), birth) and os.getsid(int(pid)) == owned_session:
        owned = True
if not owned and members:
    token = ('HERMES_KERNEL_DIR=' + KERNEL_DIRECTORY).encode()
    for pid in members:
        try:
            if token in Path('/proc/%s/environ' % pid).read_bytes().split(bytes([0])):
                owned = True
                break
        except FileNotFoundError:
            pass
    if not owned:
        raise RuntimeError('remote session ownership is unconfirmed; cleanup retained')
if owned_session is None and not owned and not snapshot.exists():
    raise RuntimeError('remote session identity is missing; cleanup retained')

children = {}
for pid, parent, sid in rows:
    children.setdefault(parent, []).append(pid)
pending = [root_pid] if running(root_pid, root_birth) else []
if owned:
    pending.extend(members)
seen = set()
while pending:
    pid = pending.pop()
    if pid in seen:
        continue
    seen.add(pid)
    birth = identity(pid)
    if pid == root_pid and birth != root_birth:
        continue
    if pid in members and os.getsid(pid) != owned_session:
        continue
    if birth is not None:
        records[str(pid)] = birth
        pending.extend(children.get(pid, []))
    if len(records) > 1024:
        raise RuntimeError('remote process tree exceeds bounded cleanup capacity')
if not owned and not snapshot.exists():
    print('HERMES_KERNEL_STOP_CONFIRMED')
    raise SystemExit(0)
# Persist before signaling so a lost teardown reply cannot forget reparented
# or session-detached descendants captured while the parent was alive.
fd = os.open(str(snapshot) + '.tmp', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w') as stream:
    json.dump(records, stream)
os.replace(str(snapshot) + '.tmp', snapshot)

def signal_one(pid, birth, sig):
    if not running(pid, birth):
        return
    try:
        if hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal'):
            fd = os.pidfd_open(pid)
            try:
                if identity(pid) == birth:
                    signal.pidfd_send_signal(fd, sig)
            finally:
                os.close(fd)
        elif identity(pid) == birth:
            os.kill(pid, sig)
    except ProcessLookupError:
        pass

def live():
    return [(int(pid), birth) for pid, birth in records.items() if running(int(pid), birth)]

def wait_until_stopped(grace):
    end = time.monotonic() + grace
    while live() and time.monotonic() < end:
        time.sleep(.05)

# Let a cooperative parent reap its children before signaling survivors.
signal_one(root_pid, root_birth, signal.SIGTERM)
wait_until_stopped(.5)
for sig, grace in ((signal.SIGTERM, .5), (signal.SIGKILL, 2.0)):
    for pid, birth in live():
        signal_one(pid, birth, sig)
    wait_until_stopped(grace)
if live():
    raise RuntimeError('owned remote subprocesses survived forced termination')
print('HERMES_KERNEL_STOP_CONFIRMED')
"""


def confirmed_remote_stop(kernel):
    # The remote already requires Python. Probe zombies as exited: kill(0)
    # alone reports an unreaped, non-resident process as still running.
    if kernel.birth_identity is None:
        try:
            kernel.birth_identity = capture_remote_identity(kernel)
        except Exception as exc:
            raise KernelTeardownError('incomplete_teardown: remote startup receipt unavailable; '
                                      'resident allocation retained; restore remote access to retry cleanup') from exc
    if not kernel.pid:
        raise KernelTeardownError('incomplete_teardown: remote startup is unconfirmed; '
                                  'resident allocation retained; restore remote access to retry cleanup')
    source = ('import json, os, signal, subprocess, time\nfrom pathlib import Path\n' +
              _IDENTITY_SOURCE + _STOP_TREE_SOURCE.replace('ROOT_PID', str(int(kernel.pid)))
              .replace('ROOT_BIRTH', repr(kernel.birth_identity))
              .replace('KERNEL_DIRECTORY', repr(kernel.kernel_dir))
              .replace('SESSION_IDENTITY', repr(kernel.session_identity)))

    try:
        acknowledgement = kernel.sh('python3 -c ' + shlex.quote(source), timeout=12)
    except Exception as exc:
        raise KernelTeardownError(
            'incomplete_teardown: remote kernel termination is unconfirmed; '
            'resident allocation retained. Restore the remote transport to retry cleanup.') from exc
    if 'HERMES_KERNEL_STOP_CONFIRMED' not in acknowledgement:
        raise KernelTeardownError(
            'incomplete_teardown: remote kernel did not confirm termination; '
            'resident allocation retained. Restore remote access and retry cleanup.')


def teardown_errors_as_result(*, remote):
    def decorate(function):
        @functools.wraps(function)
        def guarded(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except KernelTeardownError as exc:
                result = {'status': 'error', 'error': str(exc),
                          'incomplete_teardown': [{'status': 'remote_kernel_still_resident'}]}
                return result if remote else json.dumps(result)
        return guarded
    return decorate
