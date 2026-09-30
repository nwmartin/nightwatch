import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from nightwatch.core import config, quota_threshold
from nightwatch.quota import quota_percent, remaining_quota
from nightwatch.__main__ import run


class QuotaTests(unittest.TestCase):
    def test_tightest_reported_window(self):
        self.assertEqual(quota_percent({'rateLimits': {
            'primary': {'usedPercent': 40}, 'secondary': {'usedPercent': 98}}}), 2)
        self.assertEqual(quota_percent({'rateLimits': {
            'primary': None, 'secondary': {'usedPercent': 95}}}), 5)

    def test_invalid_or_unavailable_quota(self):
        for value in (None, {}, {'primary': {'usedPercent': True}},
                      {'primary': {'usedPercent': -1}},
                      {'primary': {'usedPercent': 101}},
                      {'primary': {'usedPercent': float('nan')}},
                      {'primary': {'usedPercent': '95'}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                quota_percent({'rateLimits': value})

    def test_threshold_validation(self):
        self.assertEqual(quota_threshold('5.5'), 5.5)
        for value in (True, None, 0, -1, 101, 'bad', 'nan', 'inf'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                quota_threshold(value)

    def test_old_config_defaults_to_five(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'.nightwatch').mkdir()
            binary = root/'codex'
            binary.touch()
            settings = dict(working_directory=str(root), codex=str(binary),
                            start='22:00', end='07:00', poll_seconds=60)
            (root/'.nightwatch/config.json').write_text(json.dumps(settings))
            self.assertEqual(config(root)['quota_threshold_percent'], 5)
            settings['quota_threshold_percent'] = 101
            (root/'.nightwatch/config.json').write_text(json.dumps(settings))
            with self.assertRaises(ValueError):
                config(root)

    def helper(self, lines):
        process = MagicMock()
        process.stdout = io.StringIO(lines)
        process.stdin = io.StringIO()
        process.poll.return_value = None
        return process

    def test_protocol_handshake_notifications_and_cleanup(self):
        process = self.helper('\n'.join(json.dumps(message) for message in (
            {'id': 1, 'result': {}}, {'method': 'notification'},
            {'id': 2, 'result': {'rateLimits': {'primary': {'usedPercent': 96}}}})) + '\n')
        captured = []
        process.stdin.close = lambda: captured.extend(
            json.loads(line) for line in process.stdin.getvalue().splitlines())
        with patch('nightwatch.quota.subprocess.Popen', return_value=process) as popen:
            self.assertEqual(remaining_quota({'codex': '/codex', 'working_directory': '/project'}), 4)
        self.assertEqual([m['method'] for m in captured],
                         ['initialize', 'initialized', 'account/rateLimits/read'])
        self.assertEqual(popen.call_args.kwargs['cwd'], '/project')
        process.terminate.assert_called_once()
        process.wait.assert_called_once()

    def test_protocol_failures_cleanup(self):
        for lines in ('', 'bad json\n', '{"id":1,"error":{}}\n'):
            process = self.helper(lines)
            with patch('nightwatch.quota.subprocess.Popen', return_value=process):
                with self.assertRaises(ValueError):
                    remaining_quota({'codex': '/codex', 'working_directory': '/project'})
            process.terminate.assert_called_once()

    @unittest.skipIf(os.name == 'nt', 'Fake executable uses a Unix shebang')
    def test_real_subprocess_handshake(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory)/'fake-codex'
            binary.write_text('#!' + sys.executable + '\n' + '''import json, sys
assert sys.argv[1:] == ['app-server', '--stdio']
initialize = json.loads(sys.stdin.readline())
assert initialize['method'] == 'initialize'
print(json.dumps({'id': initialize['id'], 'result': {}}), flush=True)
assert json.loads(sys.stdin.readline())['method'] == 'initialized'
request = json.loads(sys.stdin.readline())
assert request['method'] == 'account/rateLimits/read'
print(json.dumps({'id': request['id'], 'result': {
    'rateLimits': {'primary': {'usedPercent': 97}}}}), flush=True)
sys.stdin.read()
''')
            binary.chmod(0o755)
            self.assertEqual(remaining_quota({'codex': str(binary),
                                             'working_directory': directory}), 3)

    def test_timeout_kills_unresponsive_helper(self):
        process = self.helper('')
        process.wait.side_effect = [subprocess.TimeoutExpired('codex', 5), None]
        with patch('nightwatch.quota.subprocess.Popen', return_value=process), \
             patch('nightwatch.quota.Queue') as queue:
            from queue import Empty
            queue.return_value.get.side_effect = Empty
            with self.assertRaisesRegex(ValueError, 'timed out'):
                remaining_quota({'codex': '/codex', 'working_directory': '/project'}, timeout=0)
        process.kill.assert_called_once()

    def test_daemon_exits_after_quota_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'.nightwatch').mkdir()
            with patch('nightwatch.__main__.config', return_value={}), \
                 patch('nightwatch.__main__.tick', return_value='quota stop: Remaining quota 4% is below 5%.') as tick, \
                 patch('nightwatch.__main__.signal.signal'), \
                 patch('nightwatch.__main__.Console') as console:
                run(root)
            tick.assert_called_once()
            messages = str(console.return_value.__enter__.return_value.log.call_args_list)
            self.assertIn('quota stop:', messages)


if __name__ == '__main__':
    unittest.main()
