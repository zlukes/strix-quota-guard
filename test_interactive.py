"""Real nested PTYs; no Strix, credentials or network."""
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest

SCRATCH = os.environ.get('STRIX_GUARD_TEST_TMPDIR') or None
ROOT = str(Path(__file__).resolve().parent)


class InteractiveTests(unittest.TestCase):
    def run_terminal(self, child, action=None, usage='return 0', expected=0,
                     ready=b'READY', setup='', command=None):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
            root = Path(temp)
            child_path = root / 'child.py'
            child_path.write_text(child)
            runner = root / 'runner.py'
            runner.write_text('import sys, os, time\nfrom pathlib import Path\n'
                'sys.path.insert(0, %r)\nimport strix_quota_guard as g\n'
                'os.chdir(%r)\ncalls = 0\n'
                'def usage():\n global calls\n calls += 1\n %s\n'
                '%s\n'
                'sys.exit(g.run_guard(%r, usage, interval=.05, '
                'grace=.05, interactive=True))\n' %
                (ROOT, temp, usage.replace('\n', '\n '), setup,
                 command or [sys.executable, str(child_path)]))
            master, slave = os.openpty()
            initial = termios.tcgetattr(slave)
            initial_flags = fcntl.fcntl(slave, fcntl.F_GETFL)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 80, 0, 0))
            proc = subprocess.Popen([sys.executable, str(runner)], stdin=slave,
                                    stdout=slave, stderr=slave, start_new_session=True)
            data = bytearray()
            def wait_for(marker, timeout=4):
                deadline = time.monotonic() + timeout
                while marker not in data and time.monotonic() < deadline:
                    if select.select([master], [], [], .02)[0]:
                        data.extend(os.read(master, 65536))
                    if proc.poll() is not None:
                        break
                self.assertIn(marker, data, data.decode(errors='replace'))
            try:
                if ready is not None:
                    wait_for(ready)
                if action and action(proc, master, slave, root, wait_for, data) == 'disconnect':
                    os.close(master)
                    master = None
                deadline = time.monotonic() + 5
                while proc.poll() is None and time.monotonic() < deadline:
                    if master is not None and select.select([master], [], [], .02)[0]:
                        data.extend(os.read(master, 65536))
                    else:
                        time.sleep(.01)
                self.assertEqual(proc.wait(timeout=1), expected, data.decode(errors='replace'))
                while master is not None and select.select([master], [], [], 0)[0]:
                    data.extend(os.read(master, 65536))
                restored = termios.tcgetattr(slave) if master is not None else initial
                # Darwin sets the kernel bookkeeping PENDIN flag on tcsetattr.
                restored[3] &= ~getattr(termios, 'PENDIN', 0)
                initial[3] &= ~getattr(termios, 'PENDIN', 0)
                self.assertEqual(restored, initial)
                # Darwin also adds an internal F_GETFL bit after any write
                # (even plain print(), and F_SETFL cannot clear it). Verify the
                # mutable nonblocking flag that the relay actually changes.
                self.assertEqual(fcntl.fcntl(slave, fcntl.F_GETFL) & os.O_NONBLOCK,
                                 initial_flags & os.O_NONBLOCK)
                return bytes(data), {p.name: p.read_text() for p in root.glob('*.result')}
            finally:
                if proc.poll() is None:
                    proc.send_signal(signal.SIGINT)
                    proc.wait(timeout=5)
                if master is not None:
                    os.close(master)
                os.close(slave)

    def test_resize_and_keyboard_while_quota_request_is_blocked(self):
        child = '''import os, signal, sys, tty
assert os.get_terminal_size() == (80, 24)
def resize(s, f):
    size = os.get_terminal_size()
    print('SIZE:%d:%d' % (size.columns, size.lines), flush=True)
signal.signal(signal.SIGWINCH, resize)
tty.setraw(0)
print('READY', flush=True)
while True:
    key = os.read(0, 1)
    if key == b'x': print('INPUT_DURING_POLL', flush=True)
    if key == b'q': sys.exit(0)
'''
        def action(proc, master, slave, root, wait, data):
            deadline = time.monotonic() + 2
            while not (root / 'poll.result').exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue((root / 'poll.result').exists())
            os.write(master, b'x')
            wait(b'INPUT_DURING_POLL', timeout=.7)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
            proc.send_signal(signal.SIGWINCH)
            wait(b'SIZE:120:40', timeout=.7)
            self.assertNotIn(b'Weekly usage:', data)
            os.write(master, b'q')
        self.run_terminal(child, action,
            usage="if calls > 1:\n Path('poll.result').touch()\n time.sleep(3)\nreturn 0")

    def test_quota_and_error_stop_backend_not_sidecar_and_defer_logs(self):
        child = '''import os, signal, subprocess, sys, time, tty
from pathlib import Path
sidecar = subprocess.Popen([sys.executable, '-c', "import signal,time,sys; from pathlib import Path; signal.signal(signal.SIGINT, lambda s,f: Path('BAD.result').touch()); signal.signal(signal.SIGTERM, lambda s,f: sys.exit(0)); Path('sidecar.result').touch(); time.sleep(5)"])
def stop(s, f):
    Path('signal.result').write_text(str(s))
    time.sleep(.2)
    sidecar.terminate()
    sidecar.wait()
    Path('cleaned.result').touch()
    print('CLEANED', flush=True)
    sys.exit(0)
signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGALRM, stop)
signal.alarm(4)
while not Path('sidecar.result').exists(): time.sleep(.01)
tty.setraw(0)
Path('ready.result').touch()
print('READY', flush=True)
while True: time.sleep(.01)
'''
        for outcome, expected in [('return 80', 80), ("raise g.GuardError('Synthetic usage failure.')", 81)]:
            with self.subTest(expected=expected):
                usage = ("if calls == 1: return 0\n"
                         "while not Path('ready.result').exists(): time.sleep(.01)\n" + outcome)
                data, files = self.run_terminal(child, usage=usage, expected=expected)
                self.assertIn('cleaned.result', files)
                self.assertNotIn('BAD.result', files)
                self.assertEqual(files['signal.result'], str(int(signal.SIGINT)))
                self.assertGreater(data.index(b'shutdown exceeded --grace'), data.index(b'CLEANED'))

    def test_interrupts_shutdown_once_during_blocked_poll(self):
        child = '''import os, signal, sys, time, tty
from pathlib import Path
def stop(s, f):
    with open('signals.result', 'a') as out: out.write(str(s) + ',')
    print('STOPPING', flush=True)
    time.sleep(.2)
    Path('cleaned.result').touch()
    sys.exit(0)
signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGALRM, stop)
signal.alarm(4)
tty.setraw(0)
print('READY', flush=True)
while True: time.sleep(.01)
'''
        for sig in (None, signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig):
                def action(proc, master, slave, root, wait, data):
                    deadline = time.monotonic() + 2
                    while not (root / 'poll.result').exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue((root / 'poll.result').exists())
                    if sig is None:
                        os.write(master, b'\x03')
                    else:
                        proc.send_signal(sig)
                    wait(b'STOPPING', timeout=.7)
                    if sig is None:
                        os.write(master, b'\x03\x03')
                    else:
                        proc.send_signal(sig)
                data, files = self.run_terminal(child, action,
                    usage="if calls > 1:\n Path('poll.result').touch()\n time.sleep(3)\nreturn 0",
                    expected=128 + int(sig or signal.SIGINT))
                self.assertIn('cleaned.result', files)
                self.assertEqual(files['signals.result'], str(int(signal.SIGINT)) + ',')

    def test_large_bidirectional_io_does_not_deadlock_or_drop_bytes(self):
        child = '''import os, signal, sys, tty
signal.alarm(3)
tty.setraw(0)
print('READY', flush=True)
block = b'o' * 262144
while block:
    block = block[os.write(1, block):]
received = bytearray()
while len(received) < 131072:
    received.extend(os.read(0, 4096))
assert received == b'i' * 131072
print('TRANSFER_OK', flush=True)
'''
        def action(proc, master, slave, root, wait, data):
            os.set_blocking(master, False)
            remaining = b'i' * 131072
            deadline = time.monotonic() + 2
            while remaining and time.monotonic() < deadline:
                readable, writable, _ = select.select([master], [master], [], .01)
                if readable:
                    data.extend(os.read(master, 65536))
                if writable:
                    try:
                        remaining = remaining[os.write(master, remaining):]
                    except BlockingIOError:
                        pass
            self.assertEqual(len(remaining), 0, 'PTY forwarding blocked on backpressure')
        data, _ = self.run_terminal(child, action)
        self.assertIn(b'TRANSFER_OK', data)
        self.assertIn(b'o' * 262144, data)

    def test_terminal_disconnect_still_waits_for_backend_cleanup(self):
        child = '''import signal, sys, time, tty
from pathlib import Path
def stop(s, f):
    time.sleep(.15)
    Path('cleaned.result').write_text(str(s))
    sys.exit(0)
signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGALRM, stop)
signal.alarm(3)
tty.setraw(0)
print('READY', flush=True)
while True: time.sleep(.01)
'''
        _, files = self.run_terminal(child, lambda *args: 'disconnect', expected=129)
        self.assertEqual(files['cleaned.result'], str(int(signal.SIGINT)))

    def test_terminal_io_failure_restores_terminal_and_waits_for_cleanup(self):
        child = '''import signal, sys, time, tty
from pathlib import Path
def stop(s, f):
    time.sleep(.1)
    print('x' * 131072, flush=True)
    Path('cleaned.result').touch()
    sys.exit(0)
signal.signal(signal.SIGINT, stop)
signal.alarm(3)
tty.setraw(0)
print('READY', flush=True)
while True: time.sleep(.01)
'''
        setup = '''import select
original_select = select.select
def broken_select(*args):
    if Path('break.result').exists():
        select.select = original_select
        raise OSError('SECRET')
    return original_select(*args)
select.select = broken_select
'''
        def action(proc, master, slave, root, wait, data):
            (root / 'break.result').touch()
        data, files = self.run_terminal(child, action, setup=setup, expected=81)
        self.assertIn('cleaned.result', files)
        self.assertNotIn(b'SECRET', data)
        self.assertNotIn(b'Traceback', data)

    def test_startup_failures_and_preflight_leave_terminal_unchanged(self):
        child = "from pathlib import Path; Path('spawned.result').touch()"
        for usage, expected in [('return 80', 80), ("raise g.GuardError('Synthetic preflight failure.')", 81)]:
            data, files = self.run_terminal(child, usage=usage, expected=expected, ready=None)
            self.assertNotIn('spawned.result', files)
        data, _ = self.run_terminal('', command=['/nonexistent/SECRET'], expected=81, ready=None)
        self.assertNotIn(b'SECRET', data)
        setup = "def failed_pty(): raise OSError('SECRET')\nos.openpty = failed_pty"
        data, files = self.run_terminal(child, setup=setup, expected=81, ready=None)
        self.assertNotIn('spawned.result', files)
        self.assertNotIn(b'SECRET', data)
        self.assertNotIn(b'Traceback', data)

    def test_abnormal_child_exit_restores_display_modes(self):
        child = "import os, signal; print('\\x1b[?1049h\\x1b[?25lREADY', flush=True); os.kill(os.getpid(), signal.SIGTERM)"
        data, _ = self.run_terminal(child, expected=143)
        self.assertIn(b'\x1b[?1049l', data)
        self.assertIn(b'\x1b[?25h', data)

    def test_terminal_keyboard_and_child_exit(self):
        child = '''import os, sys, tty
assert all(os.isatty(fd) for fd in (0, 1, 2))
assert os.tcgetpgrp(0) == os.getpgrp() == os.getpid()
with open('/dev/tty', 'rb', buffering=0) as terminal:
    assert os.isatty(terminal.fileno())
tty.setraw(0)
print('READY', flush=True)
assert os.read(0, 1) == b'x'
print('KEYBOARD_OK', flush=True)
sys.exit(7)
'''
        def action(proc, master, slave, root, wait, data):
            os.write(master, b'x')
        # Exercise the real CLI default, replacing only the network callback.
        setup = '''original_run_guard = g.run_guard
def via_cli(command, usage, **kwargs):
    g.run_guard = original_run_guard
    g.fetch_usage = lambda **kw: usage()
    return g.main(['--executable', command[0], '--'] + command[1:])
g.run_guard = via_cli
'''
        data, _ = self.run_terminal(child, action, expected=7, setup=setup)
        self.assertIn(b'KEYBOARD_OK', data)


if __name__ == '__main__':
    unittest.main()
