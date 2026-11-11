#!/usr/bin/env python3
"""Read-only quota guard for local headless Strix; Python 3.9+, POSIX."""


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


def run_guard(command, usage, interval=30, grace=60, output=None):
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
        try:
            used = usage()
            print('Týdenní využití: %g %%' % used, file=output, flush=True)
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
            print('Nelze spustit Strix; ověřte --executable a oprávnění.', file=output, flush=True)
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
                print('Týdenní využití: %g %%' % used, file=output, flush=True)
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
            print('VAROVÁNÍ: proces stále běží; SIGKILL neposílám, čekám na ukončení.',
                  file=output, flush=True)
            child.wait()
        return status
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


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
        raise GuardError('Neplatný auth soubor Strix. Spusťte strix auth login a guard znovu.') from None
    if expires <= time.time() + timeout:
        raise GuardError('Token expiroval nebo brzy expiruje. Spusťte strix auth login a guard znovu.')
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
            raise GuardError('Příliš velká odpověď usage.')
        return weekly_percent(json.loads(body))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise GuardError('HTTP 401: spusťte strix auth login a guard znovu.') from None
        raise GuardError('Usage HTTP chyba; běh není bezpečně hlídán.') from None
    except (OSError, ValueError, UnicodeError, http.client.HTTPException):
        raise GuardError('Usage nelze načíst (síť nebo neplatný JSON).') from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise GuardError('Přesměrování usage odmítnuto; token neopustí pevný endpoint.')


class GuardError(Exception):
    """Only fixed, secret-free diagnostic messages belong here."""


def weekly_percent(data):
    limits = data.get('rate_limit') if isinstance(data, dict) else None
    if not isinstance(limits, dict):
        raise GuardError('Chybí rate_limit v odpovědi usage.')
    windows = [w for w in limits.values() if isinstance(w, dict)
               and w.get('limit_window_seconds') == 604800]
    if len(windows) != 1:
        raise GuardError('Chybí jednoznačné týdenní okno (604800 sekund).')
    used = windows[0].get('used_percent')
    if type(used) not in (int, float) or not 0 <= used <= 100:
        raise GuardError('Neplatné týdenní used_percent.')
    return used


def main(argv=None):
    parser = argparse.ArgumentParser(description='Strix: hlídání týdenní ChatGPT kvóty na 80 %.')
    parser.add_argument('--check', action='store_true', help='Jen načíst usage; nespouštět Strix.')
    parser.add_argument('--interval', type=float, default=30, help='Interval kontrol v sekundách (30).')
    parser.add_argument('--grace', type=float, default=60, help='Počet sekund do varování při ukončování (60).')
    parser.add_argument('--timeout', type=float, default=10, help='Síťový timeout v sekundách (10).')
    parser.add_argument('--executable', default='strix', help='Cesta k důvěryhodnému Strix executable.')
    parser.add_argument('strix_args', nargs=argparse.REMAINDER, help='Argumenty Strix za --.')
    args = parser.parse_args(argv)
    forwarded = args.strix_args
    for name in ('interval', 'grace', 'timeout'):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
            parser.error('--%s musí být konečné kladné číslo.' % name)
    if args.check and forwarded:
        parser.error('--check nepřijímá argumenty Strix.')
    if not args.check and (not forwarded or forwarded[0] != '--' or len(forwarded) < 2):
        parser.error('Argumenty Strix zadejte za --; například -- -t https://example.invalid')
    if forwarded:
        forwarded = forwarded[1:]
    for index, arg in enumerate(forwarded):
        if arg.split('=', 1)[0] in ('--interactive', '--no-non-interactive', '--tui'):
            parser.error('Interaktivní režim není podporován; guard vždy přidává -n.')
        if arg in ('--resume', '-r') and (index + 1 == len(forwarded)
                or forwarded[index + 1].startswith('-')):
            parser.error('--resume vyžaduje název běhu.')
    def usage():
        return fetch_usage(timeout=args.timeout)
    if args.check:
        try:
            used = usage()
            print('Týdenní využití: %g %%' % used)
            return QUOTA_EXIT if used >= 80 else 0
        except GuardError as exc:
            print(str(exc), file=sys.stderr)
            return ERROR_EXIT
    return run_guard([args.executable, '-n'] + forwarded, usage,
                     interval=args.interval, grace=args.grace)


if __name__ == '__main__':
    sys.exit(main())
