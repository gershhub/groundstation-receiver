"""Offline TLE regressions; no receiver, radio hardware, or AWS side effects."""

import ast
import configparser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import tle

VALID = (ROOT / 'test/fixtures/noaa.txt').read_bytes()
NAMES = tle.NOAA_SATELLITES
INVALID_QUERY = b'Invalid query: "GROUP=noaa&FORMAT=tle" (GROUP=noaa not found)\r\n'


def response(content=VALID, status=200):
    result = MagicMock(status_code=status, content=content)
    result.__enter__.return_value = result
    return result


def checked_line(line):
    """Recompute checksum to exercise catalog validation independently."""
    checksum = sum(int(char) if char.isdigit() else 1 if char == '-' else 0
                   for char in line[:68]) % 10
    return line[:68] + str(checksum)


class ParseTests(unittest.TestCase):
    def test_records_keep_original_predict_input(self):
        records = tle.parse_tles(VALID, NAMES)
        lines = VALID.decode('ascii').splitlines()
        for index, name in enumerate(NAMES):
            self.assertEqual(records[name], lines[index * 3:index * 3 + 3])

    def test_windows_line_endings_padding_and_zero_prefix(self):
        lines = VALID.decode('ascii').splitlines()
        for index in range(0, len(lines), 3):
            lines[index] = '0 ' + lines[index] + '   '
        content = ('\r\n'.join(lines) + '\r\n').encode('ascii')
        self.assertEqual(tle.parse_tles(content, NAMES), tle.parse_tles(VALID, NAMES))

    def test_exact_names_exclude_debris_and_other_noaa_numbers(self):
        lines = VALID.decode('ascii').splitlines()
        extras = '\n'.join(['NOAA 15 DEB'] + lines[1:3] + ['NOAA 1'] + lines[1:3]) + '\n'
        self.assertEqual(tle.parse_tles(extras.encode() + VALID, NAMES),
                         tle.parse_tles(VALID, NAMES))
        with self.assertRaisesRegex(tle.TLEError, 'Missing TLE for NOAA 1$'):
            tle.parse_tles(VALID, ['NOAA 1'])

    def test_rejects_invalid_or_incomplete_downloads(self):
        lines = VALID.splitlines(keepends=True)
        invalid = [b'', INVALID_QUERY, b'<html>Error</html>\n', b'\xff',
                   b''.join(lines[:-1]), b''.join(lines[:3]),
                   VALID + b''.join(lines[:3]), VALID.replace(b'1 25338', b'2 25338', 1),
                   VALID.replace(b'26284.28656142', b'26284.28656143', 1)]
        for content in invalid:
            with self.subTest(content=content[:40]), self.assertRaises(tle.TLEError):
                tle.parse_tles(content, NAMES)

    def test_rejects_catalog_mismatch_with_valid_checksums(self):
        original = VALID.decode('ascii').splitlines()
        for indices in ((2,), (1, 2)):
            lines = list(original)
            for index in indices:
                lines[index] = checked_line(lines[index][:2] + '12345' + lines[index][7:])
            with self.subTest(indices=indices), self.assertRaisesRegex(tle.TLEError, 'catalog'):
                tle.parse_tles(('\n'.join(lines) + '\n').encode(), NAMES)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = Path(self.directory.name) / 'noaa.txt'
        self.cache.write_bytes(VALID)

    def test_success_replaces_whole_cache_and_preserves_permissions(self):
        # A CRLF source is still valid and is retained byte for byte on disk.
        fresh = VALID.replace(b'\n', b'\r\n')
        self.cache.chmod(0o640)
        with patch('tle.requests.get', return_value=response(fresh)) as get:
            result = tle.refresh_tles(self.cache, NAMES, 'https://mirror.invalid/noaa.txt')
        self.assertTrue(result.updated)
        self.assertEqual(result.records, tle.parse_tles(VALID, NAMES))
        self.assertEqual(self.cache.read_bytes(), fresh)
        self.assertEqual(self.cache.stat().st_mode & 0o777, 0o640)
        self.assertEqual(list(self.cache.parent.iterdir()), [self.cache])
        get.assert_called_once_with('https://mirror.invalid/noaa.txt',
                                    timeout=(5, 20), allow_redirects=False)

    def test_first_download_creates_cache_directory(self):
        cache = self.cache.parent / 'new directory/noaa.txt'
        with patch('tle.requests.get', return_value=response()):
            result = tle.refresh_tles(cache, NAMES)
        self.assertTrue(result.updated)
        self.assertEqual(cache.read_bytes(), VALID)
        self.assertEqual(cache.stat().st_mode & 0o777, 0o644)

    def test_http_errors_and_redirects_do_not_retry_or_change_cache(self):
        for status in (301, 403, 404, 429, 503):
            with self.subTest(status=status), \
                    patch('tle.requests.get', return_value=response(status=status)) as get, \
                    self.assertLogs(level='WARNING') as logs:
                result = tle.refresh_tles(self.cache, NAMES)
            self.assertFalse(result.updated)
            self.assertEqual(result.records, tle.parse_tles(VALID, NAMES))
            self.assertEqual(self.cache.read_bytes(), VALID)
            self.assertIn('HTTP {}'.format(status), '\n'.join(logs.output))
            get.assert_called_once()

    def test_network_failures_use_cache(self):
        for error in (tle.requests.ConnectionError('offline'),
                      tle.requests.ConnectTimeout('connect timeout'),
                      tle.requests.ReadTimeout('read timeout')):
            with self.subTest(error=error), patch('tle.requests.get', side_effect=error) as get, \
                    self.assertLogs(level='WARNING'):
                result = tle.refresh_tles(self.cache, NAMES)
            self.assertFalse(result.updated)
            self.assertEqual(result.records, tle.parse_tles(VALID, NAMES))
            self.assertEqual(self.cache.read_bytes(), VALID)
            get.assert_called_once()

    def test_http_200_errors_partial_and_corrupt_tles_preserve_cache(self):
        for content in (INVALID_QUERY, b''.join(VALID.splitlines(keepends=True)[:3]),
                        VALID.replace(b'26284.28656142', b'26284.28656143', 1)):
            with self.subTest(content=content[:40]), \
                    patch('tle.requests.get', return_value=response(content)), \
                    self.assertLogs(level='WARNING'):
                result = tle.refresh_tles(self.cache, NAMES)
            self.assertFalse(result.updated)
            self.assertEqual(self.cache.read_bytes(), VALID)
            self.assertEqual(result.records, tle.parse_tles(VALID, NAMES))

    def test_atomic_write_failures_preserve_cache_and_remove_temporary_file(self):
        for operation in ('fsync', 'replace'):
            with self.subTest(operation=operation), \
                    patch('tle.requests.get', return_value=response(VALID.replace(b'\n', b'\r\n'))), \
                    patch('tle.os.' + operation, side_effect=OSError('disk failure')), \
                    self.assertLogs(level='WARNING'):
                result = tle.refresh_tles(self.cache, NAMES)
            self.assertFalse(result.updated)
            self.assertEqual(self.cache.read_bytes(), VALID)
            self.assertEqual(list(self.cache.parent.iterdir()), [self.cache])

    def test_no_usable_cache_raises_clear_error(self):
        for cached in (None, INVALID_QUERY, b''.join(VALID.splitlines(keepends=True)[:3])):
            if cached is None:
                self.cache.unlink()
            else:
                self.cache.write_bytes(cached)
            with self.subTest(cached=cached), \
                    patch('tle.requests.get', return_value=response(INVALID_QUERY)), \
                    self.assertLogs(level='WARNING'), \
                    self.assertRaisesRegex(tle.TLEError, 'no usable cache'):
                tle.refresh_tles(self.cache, NAMES)
            self.assertEqual(self.cache.read_bytes() if self.cache.exists() else None, cached)


class ReceiverIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Run actual TLE wrapper/scheduling statements without importing the
        # receiver's top-level configuration, AWS, or hardware libraries.
        cls.tree = ast.parse((ROOT / 'groundstation.py').read_text())
        wrapper = next(node for node in cls.tree.body
                       if isinstance(node, ast.FunctionDef) and node.name == 'updateTLE')
        cls.scope = {'refresh_tles': tle.refresh_tles}
        exec(compile(ast.Module(body=[wrapper], type_ignores=[]), 'groundstation.py', 'exec'), cls.scope)
        cls.daily = next(node for node in ast.walk(cls.tree)
                         if isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                         and isinstance(node.test.left, ast.Name)
                         and node.test.left.id == 'tleLastUpdated')

    def test_config_default_matches_verified_source(self):
        config = configparser.ConfigParser()
        config.read(ROOT / 'groundstation.cfg')
        self.assertEqual(config['TLE']['tleUrl'], tle.DEFAULT_TLE_URL)

    def test_daily_refresh_on_utc_rollover_and_same_day_in_next_month(self):
        for before, after in ((datetime(2026, 10, 11, tzinfo=timezone.utc), datetime(2026, 10, 12, tzinfo=timezone.utc)),
                              (datetime(2026, 10, 31, tzinfo=timezone.utc), datetime(2026, 11, 1, tzinfo=timezone.utc)),
                              (datetime(2026, 9, 11, tzinfo=timezone.utc), datetime(2026, 10, 11, tzinfo=timezone.utc))):
            with self.subTest(before=before, after=after), tempfile.TemporaryDirectory() as directory:
                cache = Path(directory) / 'noaa.txt'
                satellites = [SimpleNamespace(identifier=name, TLE=None) for name in NAMES]
                clock = MagicMock()
                clock.now.return_value = after
                scope = dict(self.scope, datetime=clock, timezone=timezone,
                             tleLastUpdated=before.date(), satellites=satellites,
                             tlePath=str(cache), tleUrl='https://mirror.invalid/noaa.txt')
                with patch('tle.requests.get', return_value=response()) as get:
                    exec(compile(ast.Module(body=[self.daily], type_ignores=[]), 'groundstation.py', 'exec'), scope)
                get.assert_called_once_with(scope['tleUrl'], timeout=(5, 20), allow_redirects=False)
                self.assertEqual(scope['tleLastUpdated'], after.date())
                self.assertEqual([sat.TLE for sat in satellites],
                                 [tle.parse_tles(VALID, NAMES)[name] for name in NAMES])
                # Further passes the same day must not hammer the provider.
                with patch('tle.requests.get') as get:
                    exec(compile(ast.Module(body=[self.daily], type_ignores=[]), 'groundstation.py', 'exec'), scope)
                get.assert_not_called()

    def test_failure_does_not_partially_mutate_satellites(self):
        satellites = [SimpleNamespace(identifier=name, TLE=['previous']) for name in NAMES]
        with tempfile.TemporaryDirectory() as directory, \
                patch('tle.requests.get', return_value=response(INVALID_QUERY)), \
                self.assertLogs(level='WARNING'), self.assertRaises(tle.TLEError):
            self.scope['updateTLE'](satellites, str(Path(directory) / 'missing.txt'), tle.DEFAULT_TLE_URL)
        self.assertEqual([sat.TLE for sat in satellites], [['previous']] * 3)

    def test_predict_utility_loads_same_records_on_success_and_fallback(self):
        tree = ast.parse((ROOT / 'predictFuture.py').read_text())
        load = next(node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == 'TLE' for target in node.targets))
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / 'noaa.txt'
            scope = dict(refresh_tles=tle.refresh_tles, NOAA_SATELLITES=NAMES,
                         tleFilePath=str(cache), tleUrl=tle.DEFAULT_TLE_URL)
            for content in (VALID, INVALID_QUERY):
                with patch('tle.requests.get', return_value=response(content)):
                    exec(compile(ast.Module(body=[load], type_ignores=[]), 'predictFuture.py', 'exec'), scope)
                self.assertEqual(scope['TLE'], tle.parse_tles(VALID, NAMES))


class ShellIntegrationTests(unittest.TestCase):
    """Real shell -> Python -> HTTP -> cache flow against a loopback fixture."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='tle test ')
        self.addCleanup(self.directory.cleanup)
        self.cache = Path(self.directory.name) / 'noaa.txt'
        self.cache.write_bytes(VALID)
        self.status, self.content = 200, VALID
        self.paths = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.paths.append(self.path)
                self.send_response(owner.status)
                self.send_header('Location', '/should-not-follow')
                self.send_header('Content-Length', str(len(owner.content)))
                self.end_headers()
                self.wfile.write(owner.content)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def run_script(self):
        url = 'http://127.0.0.1:{}/?NAME=NOAA&FORMAT=TLE'.format(self.server.server_port)
        env = dict(os.environ, TLEDIR=self.directory.name, PYTHON=sys.executable)
        return subprocess.run(['/bin/bash', str(ROOT / 'updateTLE.sh'), '--url', url],
                              cwd=self.directory.name, env=env, capture_output=True,
                              text=True, timeout=10)

    def test_success_from_other_directory_with_spaces(self):
        self.content = VALID.replace(b'\n', b'\r\n')
        completed = self.run_script()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('Cached new TLE', completed.stderr)
        self.assertEqual(self.cache.read_bytes(), self.content)
        self.assertEqual(self.paths, ['/?NAME=NOAA&FORMAT=TLE'])

    def test_http_200_error_keeps_cache_and_reports_failure(self):
        self.content = INVALID_QUERY
        completed = self.run_script()
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertIn('Using cached TLE', completed.stderr)
        self.assertNotIn('Cached new TLE', completed.stderr)
        self.assertEqual(self.cache.read_bytes(), VALID)
        self.assertEqual(len(self.paths), 1)

    def test_redirect_does_not_follow_or_retry(self):
        self.status = 301
        completed = self.run_script()
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertIn('HTTP 301', completed.stderr)
        self.assertEqual(self.cache.read_bytes(), VALID)
        self.assertEqual(self.paths, ['/?NAME=NOAA&FORMAT=TLE'])

    def test_missing_cache_and_bad_response_fail_without_creating_file(self):
        self.cache.unlink()
        self.content = INVALID_QUERY
        completed = self.run_script()
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertIn('no usable cache', completed.stderr)
        self.assertFalse(self.cache.exists())
        self.assertEqual(len(self.paths), 1)


if __name__ == '__main__':
    unittest.main()
