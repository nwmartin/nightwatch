import io
import time
import unittest
from pathlib import Path
from nightwatch.console import Console


class Terminal(io.StringIO):
    def isatty(self):
        return True


class ConsoleTests(unittest.TestCase):
    def test_redirected_status_is_not_repeated(self):
        output = io.StringIO()
        with Console(output) as console:
            console.status('Waiting for tasks')
            console.status('Waiting for tasks')
            console.event('started', Path('processing/example.md'))
            console.event('finished', Path('needs_feedback/example.md'), 'failed')
        text = output.getvalue()
        self.assertEqual(text.count('Waiting for tasks'), 1)
        self.assertNotIn('\r', text)
        self.assertIn('Processed: example.md -> needs_feedback (failed)', text)

    def test_spinner_runs_while_worker_is_busy(self):
        output = Terminal()
        with Console(output) as console:
            console.status('Waiting for tasks')
            time.sleep(.25)
            console.event('started', Path('processing/example.md'))
            time.sleep(.25)
            console.event('finished', Path('done/example.md'), 'completed')
        text = output.getvalue()
        self.assertIn('Waiting for tasks', text)
        self.assertIn('Processing example.md', text)
        self.assertIn('Processed: example.md -> done (completed)', text)
        self.assertFalse(console.thread.is_alive())

    def test_control_characters_are_removed(self):
        output = io.StringIO()
        with Console(output) as console:
            console.log('task\n\x1bname.md')
        self.assertNotIn('\x1b', output.getvalue())
        self.assertEqual(output.getvalue().count('\n'), 1)
