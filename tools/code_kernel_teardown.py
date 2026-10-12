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
    record = {'pid': str(os.getpid()), 'identity': identity(os.getpid())}
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
    return identity


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
    source = 'import os, signal, subprocess, time\n' + _IDENTITY_SOURCE + '''
pid = PID
expected_identity = BIRTH
if expected_identity is None:
    raise RuntimeError('remote birth identity was not captured; teardown is unconfirmed')

def running():
    if identity(pid) != expected_identity:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True, timeout=2)
    if state.returncode not in (0, 1):
        raise RuntimeError('cannot verify remote process state')
    return bool(state.stdout.strip()) and not state.stdout.lstrip().startswith('Z')

for sig, grace in ((signal.SIGTERM, .5), (signal.SIGKILL, 2.0)):
    if not running():
        break
    subprocess.run(['pkill', '-' + str(int(sig)), '-P', str(pid)], capture_output=True, timeout=2)
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass
    end = time.monotonic() + grace
    while running() and time.monotonic() < end:
        time.sleep(.05)
if running():
    raise RuntimeError('remote kernel survived forced termination')
print('HERMES_KERNEL_STOP_CONFIRMED')
'''.replace('PID', str(int(kernel.pid))).replace('BIRTH', repr(kernel.birth_identity))
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
