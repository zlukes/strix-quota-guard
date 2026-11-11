#!/usr/bin/env python3
"""Read-only quota guard for local Strix TUI/headless; Python 3.9+, POSIX."""


import argparse
import math
import time
import json
from pathlib import Path
import urllib.request
import urllib.error
import http.client

import sys
import subprocess
import os
import signal

QUOTA_EXIT = 80
ERROR_EXIT = 81


def run_guard(command, usage, interval=30, grace=60, output=None, interactive=False):
    """Run on the main thread. usage returns validated percent or GuardError."""
    output = output if output is not None else sys.stderr
    requested = []

    def on_signal(signum, frame):
        # Do not raise during Popen or HTTP I/O; repeated signals must not
        # interrupt persistence in the child's signal handler.
        if not requested:
            requested.append(signum)

    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {sig: signal.signal(sig, on_signal) for sig in signals}
    try:
        if interactive:
            try:
                return run_interactive(command, usage, interval, grace, output, requested)
            except OSError:
                print('Cannot initialize the terminal or process.', file=output, flush=True)
                return ERROR_EXIT
        try:
            used = usage()
            print('Weekly usage: %g %%' % used, file=output, flush=True)
            if requested:
                return 128 + requested[0]
            if used >= 80:
                return QUOTA_EXIT
        except GuardError as exc:
            print(str(exc), file=output, flush=True)
            return 128 + requested[0] if requested else ERROR_EXIT
        try:
            child = subprocess.Popen(command, start_new_session=True, stdin=subprocess.DEVNULL)
        except OSError:
            print('Cannot start Strix; check --executable and permissions.', file=output, flush=True)
            return ERROR_EXIT
        next_poll = time.monotonic() + interval
        status = None
        while child.poll() is None:
            if requested:
                status = 128 + requested[0]
                break
            remaining = next_poll - time.monotonic()
            if remaining > 0:
                try:
                    child.wait(timeout=min(.1, remaining))
                except subprocess.TimeoutExpired:
                    pass
                continue
            try:
                used = usage()
                print('Weekly usage: %g %%' % used, file=output, flush=True)
                if used >= 80:
                    status = QUOTA_EXIT
            except GuardError as exc:
                print(str(exc), file=output, flush=True)
                status = ERROR_EXIT
            if requested:
                status = 128 + requested[0]
            if status is not None:
                break
            next_poll = time.monotonic() + interval
        if status is None:
            return child.returncode if child.returncode >= 0 else 128 - child.returncode
        if child.poll() is None:
            # start_new_session makes child's PID its PGID, never ours.
            try:
                os.killpg(child.pid, requested[0] if requested else signal.SIGINT)
            except ProcessLookupError:
                pass
        try:
            child.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            print('WARNING: process is still running; not sending SIGKILL, waiting for shutdown.',
                  file=output, flush=True)
            child.wait()
        return status
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


# A fresh interpreter acquires the controlling terminal before exec. Unlike a
# preexec_fn/os.fork callback this is safe even when the caller has threads.
_PTY_EXEC = '''import os, sys, fcntl, termios
try:
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.execvp(sys.argv[1], sys.argv[1:])
except OSError:
    os.write(2, b'Cannot start Strix; check --executable and permissions.\\n')
    sys.exit(81)
'''


def run_interactive(command, usage, interval, grace, output, requested):
    import contextlib
    import errno
    import fcntl
    import queue
    import select
    import termios
    import threading
    import tty

    # Only this thread touches the terminal. A daemon worker never prints and
    # cannot hold up input, resize, or exit while urllib is waiting on the network.
    results = queue.Queue(maxsize=1)
    done = threading.Event()
    last_message = None

    def check():
        try:
            return usage(), None
        except GuardError as exc:
            return None, str(exc)
        except Exception:
            return None, 'Cannot read usage; the run cannot be safely monitored.'

    def monitor():
        while not done.wait(interval):
            result = check()
            if done.is_set():
                return
            results.put(result)
            if result[1] is not None or result[0] >= 80:
                return

    used, error = check()
    if requested:
        return 128 + requested[0]
    if error is not None or used >= 80:
        print(error or 'Weekly usage: %g %%' % used, file=output, flush=True)
        return ERROR_EXIT if error else QUOTA_EXIT
    last_message = 'Weekly usage: %g %%' % used
    terminal = sys.stdin.fileno()
    screen = sys.stdout.fileno()
    saved = termios.tcgetattr(terminal)
    master, slave = os.openpty()
    child = None
    screen_flags = None
    terminal_lost = False
    stopped_at = None
    resized = [True]
    previous_resize = signal.signal(signal.SIGWINCH, lambda s, f: resized.__setitem__(0, True))

    def resize():
        size = fcntl.ioctl(terminal, termios.TIOCGWINSZ, b'\0' * 8)
        fcntl.ioctl(master, termios.TIOCSWINSZ, size)
        resized[0] = False

    try:
        resize()
        child = subprocess.Popen([sys.executable, '-c', _PTY_EXEC] + command,
                                 stdin=slave, stdout=slave, stderr=slave,
                                 start_new_session=True)
        os.close(slave)
        slave = None
        tty.setraw(terminal)
        screen_flags = fcntl.fcntl(screen, fcntl.F_GETFL)
        os.set_blocking(screen, False)
        os.set_blocking(master, False)
        incoming = bytearray()
        outgoing = bytearray()
        eof = False
        threading.Thread(target=monitor, daemon=True, name='quota-monitor').start()
        status = None
        stopped_at = None
        warned = False
        while True:
            if terminal_lost:
                outgoing.clear()
                if not requested:
                    requested.append(signal.SIGHUP)
            if resized[0] and not terminal_lost:
                resize()
            try:
                used, error = results.get_nowait()
                last_message = error or 'Weekly usage: %g %%' % used
                if status is None and (error is not None or used >= 80):
                    status = ERROR_EXIT if error else QUOTA_EXIT
            except queue.Empty:
                pass
            if requested and status is None:
                status = 128 + requested[0]
            if status is not None and stopped_at is None:
                done.set()
                # Interrupt the Python backend ONLY. Group SIGINT also hits the
                # Go sidecar and can race its IPC/terminal cleanup. asyncio.run
                # cancels the backend, whose finally shuts down its sidecar and
                # scan before main persists report_state. SIGTERM is not handled
                # by the interactive backend, so is not a graceful substitute.
                if child.poll() is None:
                    child.send_signal(signal.SIGINT)
                stopped_at = time.monotonic()
            if stopped_at is not None and time.monotonic() - stopped_at >= grace:
                warned = True
            if status is not None:
                incoming.clear()
            # Bound memory and handle short writes in both directions. In
            # particular a paste must not block while the TUI is painting.
            readers = []
            if not eof and len(outgoing) < 262144:
                readers.append(master)
            if not terminal_lost and len(incoming) < 262144:
                readers.append(terminal)
            writers = ([master] if incoming and not eof else []) + ([screen] if outgoing else [])
            readable, writable, _ = select.select(readers, writers, [], .05)
            if master in readable:
                try:
                    data = os.read(master, 65536)
                except BlockingIOError:
                    data = None
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    data = b''
                if data == b'':
                    eof = True
                elif data and not terminal_lost:
                    outgoing.extend(data)
            if terminal in readable:
                try:
                    data = os.read(terminal, 4096)
                except BlockingIOError:
                    data = None
                except OSError:
                    data = b''
                if data == b'':
                    terminal_lost = True
                if data and b'\x03' in data:
                    if not requested:
                        requested.append(signal.SIGINT)
                elif data and status is None:
                    incoming.extend(data)
            for fd, buffer in ((master, incoming), (screen, outgoing)):
                if fd in writable and buffer:
                    try:
                        count = os.write(fd, buffer)
                        del buffer[:count]
                    except BlockingIOError:
                        pass
                    except OSError:
                        if fd != screen:
                            raise
                        terminal_lost = True
                        outgoing.clear()
            if child.poll() is not None and not outgoing and (eof or master not in readable):
                break
        result = child.wait()
        if warned:
            last_message += '\nWARNING: shutdown exceeded --grace; waited without SIGKILL.'
        return status if status is not None else (result if result >= 0 else 128 - result)
    except (OSError, termios.error):
        last_message = 'Terminal or process I/O failed; requested graceful shutdown.'
        return ERROR_EXIT
    finally:
        done.set()
        # Even a relay/setup failure must not close the PTY (and deliver HUP)
        # while the backend is persisting. Drain without painting if forwarding
        # failed, so the sidecar cannot block its owner on a full PTY buffer.
        if child is not None and child.poll() is None:
            if stopped_at is None:
                with contextlib.suppress(ProcessLookupError):
                    child.send_signal(signal.SIGINT)
            os.set_blocking(master, False)
            while child.poll() is None:
                with contextlib.suppress(OSError):
                    os.read(master, 65536)
                time.sleep(.01)
            child.wait()
        signal.signal(signal.SIGWINCH, previous_resize)
        with contextlib.suppress(OSError):
            if screen_flags is not None:
                fcntl.fcntl(screen, fcntl.F_SETFL, screen_flags)
        with contextlib.suppress(OSError, termios.error):
            termios.tcsetattr(terminal, termios.TCSANOW, saved)
        os.close(master)
        if slave is not None:
            os.close(slave)
        if not terminal_lost and screen_flags is not None:
            # Recover common fullscreen modes even if the TUI crashed before
            # emitting its own cleanup sequences. No terminal output is logged.
            with contextlib.suppress(OSError):
                os.write(screen, b'\x1b[?1049l\x1b[?25h\x1b[0m\x1b[?2004l'
                         b'\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l')
        if last_message and not terminal_lost:
            print(last_message, file=output, flush=True)


USAGE_URL = 'https://chatgpt.com/backend-api/wham/usage'
AUTH_PATH = Path.home() / '.strix' / 'subscription-auth.json'


def fetch_usage(auth_path=AUTH_PATH, timeout=10, opener=None):
    try:
        store = json.loads(Path(auth_path).read_text(encoding='utf-8'))
        record = store.get('codex') if isinstance(store, dict) else None
        if not isinstance(record, dict) or record.get('type') != 'oauth':
            raise ValueError()
        for key in ('access', 'refresh', 'account_id'):
            value = record.get(key)
            if not isinstance(value, str) or not value or any(
                    ord(c) < 33 or ord(c) > 126 for c in value):
                raise ValueError()
        expires = record.get('expires_at')
        if type(expires) not in (int, float) or not math.isfinite(expires):
            raise ValueError()
    except (OSError, ValueError, UnicodeError, OverflowError):
        raise GuardError('Invalid Strix auth file. Run strix auth login chatgpt, then restart the guard.') from None
    if expires <= time.time() + timeout:
        raise GuardError('Token has expired or will expire soon. Run strix auth login chatgpt, then restart the guard.')
    request = urllib.request.Request(USAGE_URL, headers={
        'Authorization': 'Bearer ' + record['access'],
        'ChatGPT-Account-Id': record['account_id'],
        'Accept': 'application/json',
    }, method='GET')
    opener = opener or urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(1048577)
        if len(body) > 1048576:
            raise GuardError('Usage response is too large.')
        return weekly_percent(json.loads(body))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise GuardError('HTTP 401: run strix auth login chatgpt, then restart the guard.') from None
        raise GuardError('Usage HTTP error; the run cannot be safely monitored.') from None
    except (OSError, ValueError, UnicodeError, http.client.HTTPException):
        raise GuardError('Cannot read usage (network error or invalid JSON).') from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise GuardError('Usage redirect rejected; the token will not leave the fixed endpoint.')


class GuardError(Exception):
    """Only fixed, secret-free diagnostic messages belong here."""


def weekly_percent(data):
    limits = data.get('rate_limit') if isinstance(data, dict) else None
    if not isinstance(limits, dict):
        raise GuardError('Usage response is missing rate_limit.')
    windows = [w for w in limits.values() if isinstance(w, dict)
               and w.get('limit_window_seconds') == 604800]
    if len(windows) != 1:
        raise GuardError('Missing or ambiguous weekly window (604800 seconds).')
    used = windows[0].get('used_percent')
    if type(used) not in (int, float) or not 0 <= used <= 100:
        raise GuardError('Invalid weekly used_percent.')
    return used


def main(argv=None):
    parser = argparse.ArgumentParser(description='Strix: stop at 80% of the weekly ChatGPT quota.')
    parser.add_argument('--headless', action='store_true',
                        help='Run without the fullscreen TUI (adds -n; stdin is /dev/null).')
    parser.add_argument('--check', action='store_true', help='Read usage only; do not start Strix.')
    parser.add_argument('--interval', type=float, default=30, help='Polling interval in seconds (30).')
    parser.add_argument('--grace', type=float, default=60, help='Seconds before a shutdown warning (60).')
    parser.add_argument('--timeout', type=float, default=10, help='Network timeout in seconds (10).')
    parser.add_argument('--executable', default='strix', help='Path to a trusted Strix executable.')
    parser.add_argument('strix_args', nargs=argparse.REMAINDER, help='Strix arguments after --.')
    args = parser.parse_args(argv)
    forwarded = args.strix_args
    for name in ('interval', 'grace', 'timeout'):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
            parser.error('--%s must be a finite positive number.' % name)
    if args.check and forwarded:
        parser.error('--check does not accept Strix arguments.')
    if not args.check and (not forwarded or forwarded[0] != '--' or len(forwarded) < 2):
        parser.error('Provide Strix arguments after --; for example -- -t https://example.invalid')
    if forwarded:
        forwarded = forwarded[1:]
    for index, arg in enumerate(forwarded):
        if arg.split('=', 1)[0] in ('--interactive', '--no-non-interactive', '--tui'):
            parser.error('Do not pass TUI mode switches; interactive is the default, or use --headless before --.')
        if not args.headless and arg.split('=', 1)[0] in ('-n', '--non-interactive'):
            parser.error('Use --headless before --, not -n/--non-interactive after it.')
        if arg in ('--resume', '-r') and (index + 1 == len(forwarded)
                or forwarded[index + 1].startswith('-')):
            parser.error('--resume requires a run name.')
    if not args.check and not args.headless and not all(os.isatty(fd) for fd in (0, 1, 2)):
        parser.error('Interactive mode requires a real terminal on stdin/stdout/stderr; use --headless for pipes or automation.')
    def usage():
        return fetch_usage(timeout=args.timeout)
    if args.check:
        try:
            used = usage()
            print('Weekly usage: %g %%' % used)
            return QUOTA_EXIT if used >= 80 else 0
        except GuardError as exc:
            print(str(exc), file=sys.stderr)
            return ERROR_EXIT
    command = [args.executable] + (['-n'] if args.headless else []) + forwarded
    return run_guard(command, usage, interval=args.interval, grace=args.grace,
                     interactive=not args.headless)


if __name__ == '__main__':
    sys.exit(main())
