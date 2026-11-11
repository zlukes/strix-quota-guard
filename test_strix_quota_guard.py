import importlib
import unittest
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
import urllib.error

SCRATCH = os.environ.get('STRIX_GUARD_TEST_TMPDIR') or None


class FetchTests(unittest.TestCase):
    def test_transport_failures_are_sanitized_and_redirects_rejected(self):
        g = importlib.import_module('strix_quota_guard')
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
            auth = Path(temp) / 'auth.json'
            auth.write_text(json.dumps({'codex': {'type': 'oauth', 'access': 'SECRET',
                'refresh': 'SECRET', 'account_id': 'SECRET', 'expires_at': time.time()+3600}}))
            for failure in [urllib.error.HTTPError(g.USAGE_URL, 401, 'SECRET', {}, io.BytesIO()),
                            urllib.error.HTTPError(g.USAGE_URL, 429, 'SECRET', {}, io.BytesIO()),
                            urllib.error.URLError('SECRET'), TimeoutError('SECRET')]:
                with patch('urllib.request.OpenerDirector.open', side_effect=failure):
                    with self.assertRaises(g.GuardError) as caught:
                        g.fetch_usage(auth)
                    self.assertNotIn('SECRET', str(caught.exception))
                    if getattr(failure, 'code', 0) == 401:
                        self.assertIn('strix auth login', str(caught.exception))
                if isinstance(failure, urllib.error.HTTPError):
                    failure.close()  # Release the synthetic response on Python 3.14 too.
            for body in [b'SECRET', b'{}', b'null', b'x' * 1048577]:
                with patch('urllib.request.OpenerDirector.open', return_value=io.BytesIO(body)):
                    with self.assertRaises(g.GuardError):
                        g.fetch_usage(auth)
            handler = g.NoRedirect()
            with self.assertRaises(g.GuardError):
                handler.redirect_request(None, None, 302, 'SECRET', {}, 'https://other.invalid')

    def test_invalid_or_expired_auth_never_contacts_network(self):
        g = importlib.import_module('strix_quota_guard')
        good = {'type': 'oauth', 'access': 'SECRET', 'refresh': 'SECRET',
                'account_id': 'SECRET', 'expires_at': time.time() + 3600}
        bad = [None, [], {}, {'codex': None}, {'codex': good.copy()}]
        bad[-1]['codex']['expires_at'] = time.time() - 1
        for key, value in [('type', 'api'), ('access', ''), ('access', 'a\nSECRET'),
                           ('account_id', 1), ('expires_at', None), ('expires_at', True),
                           ('expires_at', float('nan')), ('expires_at', 10**400)]:
            bad.append({'codex': dict(good, **{key: value})})
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
            auth = Path(temp) / 'auth.json'
            for data in bad:
                auth.write_text(json.dumps(data))
                with self.subTest(data=data), patch('urllib.request.OpenerDirector.open') as network:
                    with self.assertRaises(g.GuardError) as caught:
                        g.fetch_usage(auth)
                    self.assertNotIn('SECRET', str(caught.exception))
                    network.assert_not_called()
            auth.write_text('not json SECRET')
            with self.assertRaises(g.GuardError):
                g.fetch_usage(auth)
            auth.unlink()
            with self.assertRaises(g.GuardError):
                g.fetch_usage(auth)

    def test_read_only_get_uses_current_strix_token_every_time(self):
        g = importlib.import_module('strix_quota_guard')
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
            auth = Path(temp) / 'auth.json'
            seen = []
            class Opener:
                def open(self, request, timeout):
                    seen.append((request, timeout))
                    return io.BytesIO(json.dumps({'rate_limit': {'secondary_window': {
                        'limit_window_seconds': 604800, 'used_percent': 20}}}).encode())
            for token in ['FAKE-one', 'FAKE-two']:
                text = json.dumps({'codex': {'type': 'oauth', 'provider': 'codex',
                    'access': token, 'refresh': 'FAKE-refresh', 'account_id': 'FAKE-account',
                    'expires_at': time.time() + 3600}})
                auth.write_text(text)
                self.assertEqual(g.fetch_usage(auth, timeout=2, opener=Opener()), 20)
                self.assertEqual(auth.read_text(), text)
                request, timeout = seen[-1]
                self.assertEqual(request.full_url, 'https://chatgpt.com/backend-api/wham/usage')
                self.assertEqual(request.get_method(), 'GET')
                self.assertIsNone(request.data)
                self.assertEqual(request.get_header('Authorization'), 'Bearer ' + token)
                self.assertEqual(request.get_header('Chatgpt-account-id'), 'FAKE-account')
                self.assertEqual(timeout, 2)


class CliTests(unittest.TestCase):
    def setUp(self):
        for stream in ('stdout', 'stderr'):
            capture = patch('sys.' + stream, new=io.StringIO())
            capture.start()
            self.addCleanup(capture.stop)

    def test_invalid_options_fail_before_any_usage_or_spawn(self):
        g = importlib.import_module('strix_quota_guard')
        cases = [[], ['--', '--interactive'], ['--', '--interactive=true'],
                 ['--', '--no-non-interactive'], ['--check', '--', '-t', 'example.invalid'],
                 ['--', '--resume'], ['--', '-r'], ['target-without-separator']]
        for option in ('--interval', '--grace', '--timeout'):
            for value in ('0', '-1', 'nan', 'inf'):
                cases.append([option, value, '--check'])
        with patch.object(g, 'fetch_usage') as fetch, patch.object(g, 'run_guard') as run:
            for argv in cases:
                with self.subTest(argv=argv), patch('sys.stderr', new=io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        g.main(argv)
                    self.assertEqual(caught.exception.code, 2)
            fetch.assert_not_called()
            run.assert_not_called()

    def test_default_tui_requires_terminal_and_does_not_add_noninteractive(self):
        g = importlib.import_module('strix_quota_guard')
        with patch.object(g, 'fetch_usage') as fetch, patch.object(g, 'run_guard', return_value=7) as run:
            with self.assertRaises(SystemExit) as caught:
                g.main(['--', '-t', 'example.invalid'])
            self.assertEqual(caught.exception.code, 2)
            fetch.assert_not_called()
            run.assert_not_called()
            with patch('os.isatty', return_value=True):
                self.assertEqual(g.main(['--', '--resume', 'run-name']), 7)
                self.assertEqual(run.call_args.args[0], ['strix', '--resume', 'run-name'])
                self.assertTrue(run.call_args.kwargs['interactive'])
                for opt in ('-n', '--non-interactive'):
                    with self.assertRaises(SystemExit):
                        g.main(['--', opt, '-t', 'example.invalid'])

    def test_cli_check_is_read_only_and_command_is_headless(self):
        g = importlib.import_module('strix_quota_guard')
        with patch.object(g, 'fetch_usage', return_value=15) as fetch, patch.object(g, 'run_guard', return_value=7) as run:
            self.assertEqual(g.main(['--check']), 0)
            fetch.assert_called_once()
            run.assert_not_called()
            self.assertEqual(g.main(['--headless', '--interval', '2', '--grace', '4', '--executable', '/test/strix',
                                    '--', '--resume', 'run-name']), 7)
            self.assertEqual(run.call_args.args[0], ['/test/strix', '-n', '--resume', 'run-name'])
            self.assertEqual(run.call_args.kwargs['interval'], 2)
            self.assertEqual(run.call_args.kwargs['grace'], 4)
        with patch.object(g, 'fetch_usage', return_value=80):
            self.assertEqual(g.main(['--check']), 80)
        with patch.object(g, 'fetch_usage', side_effect=g.GuardError('test')):
            self.assertEqual(g.main(['--check']), 81)


class RunTests(unittest.TestCase):
    def test_child_signal_exit_uses_shell_convention(self):
        g = importlib.import_module('strix_quota_guard')
        code = 'import os,signal; os.kill(os.getpid(), signal.SIGTERM)'
        self.assertEqual(g.run_guard([sys.executable, '-c', code], lambda: 0,
                                    output=io.StringIO()), 143)

    def test_normal_exit_preserved_and_spawn_error_is_safe(self):
        g = importlib.import_module('strix_quota_guard')
        previous = signal.getsignal(signal.SIGTERM)
        for code in (0, 7):
            self.assertEqual(g.run_guard([sys.executable, '-c', 'raise SystemExit(%d)' % code],
                lambda: 79.9, output=io.StringIO()), code)
        self.assertEqual(g.run_guard(['/nonexistent/SECRET'], lambda: 0, output=io.StringIO()), 81)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_user_signals_forward_once_and_cleanup_completes(self):
        root = str(Path(__file__).resolve().parent)
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
                ready, received, cleaned = [Path(temp)/n for n in ('ready', 'received', 'cleaned')]
                child = ('import signal,time,sys,os\nfrom pathlib import Path\n'
                         'def stop(s,f):\n Path(%r).write_text(str(s))\n time.sleep(.25)\n Path(%r).touch()\n sys.exit(0)\n'
                         'for s in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP): signal.signal(s,stop)\n'
                         'Path(%r).write_text(str(os.getpid()))\nwhile True: time.sleep(.01)\n') % (str(received), str(cleaned), str(ready))
                runner = ('import sys\nsys.path.insert(0,%r)\nimport strix_quota_guard as g\n'
                          'sys.exit(g.run_guard(%r, lambda: 10, interval=30, grace=1))') % (root, [sys.executable, '-c', child])
                proc = subprocess.Popen([sys.executable, '-c', runner], stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True)
                try:
                    deadline = time.monotonic()+3
                    while not ready.exists() and proc.poll() is None and time.monotonic()<deadline:
                        time.sleep(.01)
                    self.assertTrue(ready.exists())
                    proc.send_signal(sig)
                    deadline = time.monotonic()+2
                    while not received.exists() and proc.poll() is None and time.monotonic()<deadline:
                        time.sleep(.01)
                    if proc.poll() is None:
                        proc.send_signal(sig)  # Must not interrupt child cleanup a second time.
                    proc.communicate(timeout=3)
                    self.assertEqual(proc.returncode, 128+int(sig))
                    self.assertTrue(cleaned.exists())
                    self.assertEqual(received.read_text(), str(int(sig)))
                finally:
                    if ready.exists() and not cleaned.exists():
                        try:
                            os.kill(int(ready.read_text()), signal.SIGINT)
                        except ProcessLookupError:
                            pass
                        time.sleep(.3)
                    if proc.poll() is None:
                        proc.terminate()
                    proc.communicate(timeout=3)

    def test_grace_warning_keeps_waiting_without_kill(self):
        g = importlib.import_module('strix_quota_guard')
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
            ready, cleaned = Path(temp)/'ready', Path(temp)/'cleaned'
            child = ('import signal,time,sys\nfrom pathlib import Path\n'
                     'def stop(s,f):\n time.sleep(.15)\n Path(%r).touch()\n sys.exit(0)\n'
                     'signal.signal(signal.SIGINT,stop)\nPath(%r).touch()\n'
                     'while True: time.sleep(.01)\n') % (str(cleaned), str(ready))
            count = []
            def usage():
                count.append(1)
                if len(count) == 1:
                    return 0
                deadline = time.monotonic()+3
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                return 80
            output = io.StringIO()
            self.assertEqual(g.run_guard([sys.executable, '-c', child], usage,
                interval=.02, grace=.01, output=output), 80)
            self.assertTrue(cleaned.exists())
            self.assertIn('process is still running', output.getvalue())

    def test_threshold_and_poll_error_send_sigint_and_wait_for_real_cleanup(self):
        g = importlib.import_module('strix_quota_guard')
        for outcome, status in [(80, 80), (g.GuardError('simulovaná chyba'), 81)]:
            with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
                ready, cleaned = Path(temp)/'ready', Path(temp)/'cleaned'
                child = ('import signal,time,sys,os\nfrom pathlib import Path\n'
                         'def stop(s,f):\n time.sleep(.08)\n Path(%r).write_text(str(s))\n sys.exit(0)\n'
                         'signal.signal(signal.SIGINT,stop)\n'
                         'Path(%r).write_text(str(os.getpgrp()))\n'
                         'while True: time.sleep(.01)\n') % (str(cleaned), str(ready))
                calls = []
                def usage():
                    calls.append(1)
                    if len(calls) == 1:
                        return 12
                    deadline = time.monotonic()+3
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(ready.exists(), 'child did not become ready')
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome
                self.assertEqual(g.run_guard([sys.executable, '-c', child], usage,
                    interval=.02, grace=1, output=io.StringIO()), status)
                self.assertEqual(cleaned.read_text(), str(int(signal.SIGINT)))
                self.assertNotEqual(int(ready.read_text()), os.getpgrp())

    def test_preflight_prevents_spawn_for_quota_or_failure(self):
        g = importlib.import_module('strix_quota_guard')
        for value, status in [(80, 80), (95, 80), (g.GuardError('test failure'), 81)]:
            def usage():
                if isinstance(value, Exception):
                    raise value
                return value
            with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
                marker = Path(temp) / 'spawned'
                command = [sys.executable, '-c', 'from pathlib import Path; Path(%r).touch()' % str(marker)]
                self.assertEqual(g.run_guard(command, usage, output=io.StringIO()), status)
                self.assertFalse(marker.exists())


class UsageTests(unittest.TestCase):
    def test_weekly_window_selected_by_duration(self):
        g = importlib.import_module('strix_quota_guard')
        for weekly_key, other_key in [('primary_window', 'secondary_window'),
                                      ('secondary_window', 'primary_window')]:
            data = {'rate_limit': {
                weekly_key: {'limit_window_seconds': 604800, 'used_percent': 79.5},
                other_key: {'limit_window_seconds': 18000, 'used_percent': 99}}}
            self.assertEqual(g.weekly_percent(data), 79.5)

    def test_missing_malformed_or_ambiguous_weekly_usage_fails_closed(self):
        g = importlib.import_module('strix_quota_guard')
        window = {'limit_window_seconds': 604800, 'used_percent': 1}
        bad = [None, [], {}, {'rate_limit': {}}, {'rate_limit': []},
               {'rate_limit': {'primary_window': None}},
               {'rate_limit': {'primary_window': window, 'secondary_window': window}}]
        for value in [None, True, '80', -1, 101, 10**400, float('nan'), float('inf')]:
            bad.append({'rate_limit': {'primary_window': dict(window, used_percent=value)}})
        bad.append({'rate_limit': {'primary_window': {'limit_window_seconds': 604800}}})
        for data in bad:
            with self.subTest(data=data), self.assertRaises(g.GuardError):
                g.weekly_percent(data)


if __name__ == '__main__':
    unittest.main()
