from contextlib import redirect_stdout
from datetime import datetime
import io
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from nightwatch.core import initialize, tick
from nightwatch.history import read_history, print_history, window


class HistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        initialize(self.root)
        self.c = {'start': '22:00', 'end': '07:00'}

    def run_task(self, name, now, outcome='completed', runner=None):
        (self.root/'todo'/name).write_text('Task', encoding='utf-8')
        return tick(self.root, self.c,
                    runner=runner or (lambda *a: {'status': outcome, 'report': 'Result'}),
                    now=now, quota_reader=lambda c: 100)

    def test_overnight_accumulates_outcomes_and_resets_empty_next_window(self):
        self.run_task('first.md', datetime(2026, 1, 1, 23))
        self.run_task('second.md', datetime(2026, 1, 2, 2), 'needs_feedback')
        def fail(*a):
            raise RuntimeError('Failure')
        self.run_task('third.md', datetime(2026, 1, 2, 3), runner=fail)
        history = read_history(self.root)
        self.assertEqual(history['window'], {'start': '2026-01-01T22:00:00', 'end': '2026-01-02T07:00:00'})
        self.assertEqual([t['status'] for t in history['tasks']], ['completed', 'needs_feedback', 'failed'])
        for task in history['tasks']:
            self.assertTrue((self.root/task['destination']).exists())
            self.assertTrue(task['run_id'])
            self.assertTrue(task['finished_at'])
        tick(self.root, self.c, now=datetime(2026, 1, 2, 12))
        self.assertEqual(read_history(self.root), history)
        tick(self.root, self.c, now=datetime(2026, 1, 2, 22))
        self.assertEqual(read_history(self.root)['tasks'], [])

    def test_interruption_keeps_started_entry(self):
        def interrupt(*a):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_task('interrupted.md', datetime(2026, 1, 1, 23), runner=interrupt)
        self.assertEqual(read_history(self.root)['tasks'][0]['status'], 'started')
        output = io.StringIO()
        with redirect_stdout(output):
            print_history(self.root)
        self.assertIn('interrupted.md: started', output.getvalue())
        self.assertIn('was interrupted', output.getvalue())

    def test_status_missing_empty_and_completed(self):
        output = io.StringIO()
        with redirect_stdout(output):
            print_history(self.root)
            tick(self.root, self.c, now=datetime(2026, 1, 1, 23))
            print_history(self.root)
            self.run_task('example.md', datetime(2026, 1, 1, 23))
            print_history(self.root)
        text = output.getvalue()
        self.assertIn('no history recorded yet', text)
        self.assertIn('No tasks ran', text)
        self.assertIn('example.md: completed', text)
        self.assertIn(f"report: {Path('done') / 'example.md'}", text)

    def test_daytime_window(self):
        self.assertEqual(window({'start': '09:00', 'end': '17:00'}, datetime(2026, 1, 1, 10)),
                         {'start': '2026-01-01T09:00:00', 'end': '2026-01-01T17:00:00'})

    def test_cli_status_shows_history_without_changing_it(self):
        from nightwatch.__main__ import main
        self.run_task('cli.md', datetime(2026, 1, 1, 23))
        path = self.root/'.nightwatch/latest-window.json'
        original = path.read_bytes()
        output = io.StringIO()
        with patch('nightwatch.__main__.ROOT', self.root), \
             patch('sys.argv', ['nightwatch', 'status']), redirect_stdout(output):
            main()
        self.assertIn('Daemon: stopped', output.getvalue())
        self.assertIn('cli.md: completed', output.getvalue())
        self.assertEqual(path.read_bytes(), original)

    def test_finish_after_window_preserves_claim_window(self):
        late = datetime(2026, 1, 2, 8).astimezone()
        with patch('nightwatch.core.datetime') as clock:
            clock.now.return_value = late
            self.run_task('late.md', datetime(2026, 1, 2, 6))
        self.assertEqual(read_history(self.root)['window']['start'], '2026-01-01T22:00:00')
        self.assertEqual(read_history(self.root)['tasks'][0]['status'], 'completed')
        self.assertEqual(read_history(self.root)['tasks'][0]['finished_at'], late.isoformat())


if __name__ == '__main__':
    unittest.main()
