import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from nightwatch.__main__ import setup


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)
        self.root = self.parent / 'nightwatch'
        (self.root / '.nightwatch').mkdir(parents=True)
        self.target = self.root / '.nightwatch/config.json'
        self.prompts = []

    def wizard(self, answers, runner=None):
        answers = iter(answers)

        def answer(prompt):
            self.prompts.append(prompt)
            return next(answers)

        help_text = '--ignore-user-config --ignore-rules --output-schema --output-last-message'
        # Keep the Linux-only service question in the scripted wizard on every OS.
        with patch('nightwatch.__main__.sys.platform', 'linux'), \
             patch('builtins.input', side_effect=answer), \
             patch('builtins.print') as output, \
             patch('nightwatch.__main__.shutil.which',
                   side_effect=lambda value: '/native/codex' if value in ('codex', '/native/codex') else None), \
             patch('nightwatch.__main__.subprocess.run', side_effect=runner,
                   return_value=subprocess.CompletedProcess([], 0, stdout=help_text)), \
             patch('nightwatch.__main__.install_service') as service:
            setup(self.root)
        service.assert_not_called()
        return output

    def saved(self):
        return json.loads(self.target.read_text())

    def test_blank_answers_use_defaults(self):
        self.wizard([''] * 9)
        self.assertEqual(self.saved(), dict(working_directory=str(self.parent),
                         codex='/native/codex', start='22:00', end='07:00',
                         poll_seconds=60, quota_threshold_percent=5, model='', reasoning_effort='default'))

    def test_invalid_answers_retry_only_current_question(self):
        output = self.wizard([
            str(self.parent / 'missing'), '', 'codex.cmd', '',
            'tomorrow', '24:00', '22:61', '22:00',
            'bad', '22:00', '7:00', 'soon', '0', '-1', '30',
            'bad', '0', '101', 'nan', '8.5', 'chosen-model', 'high', 'maybe', 'no',
        ])
        saved = self.saved()
        self.assertEqual((saved['start'], saved['end'], saved['poll_seconds'], saved['model']),
                         ('22:00', '07:00', 30, 'chosen-model'))
        self.assertEqual(sum('Invalid answer:' in str(call) for call in output.call_args_list), 15)
        self.assertEqual(saved['quota_threshold_percent'], 8.5)
        self.assertEqual(sum('Default working directory' in p for p in self.prompts), 2)
        self.assertEqual(sum('Start time' in p for p in self.prompts), 4)

    def test_existing_settings_are_defaults(self):
        previous = dict(working_directory=str(self.root), codex='/native/codex',
                        start='09:00', end='17:00', poll_seconds=15, quota_threshold_percent=12, model='saved-model', reasoning_effort='xhigh')
        self.target.write_text(json.dumps(previous))
        self.wizard(['yes'] + [''] * 9)
        self.assertEqual(self.saved(), previous)

    def test_declining_replacement_leaves_file_untouched(self):
        self.target.write_text('original configuration')
        self.wizard([''])
        self.assertEqual(self.target.read_text(), 'original configuration')
        self.assertEqual(len(self.prompts), 1)

    def test_cli_failure_retries_before_time_questions(self):
        help_text = '--ignore-user-config --ignore-rules --output-schema --output-last-message'
        results = iter([
            FileNotFoundError('Missing executable'),
            subprocess.CompletedProcess([], 0, stdout='unsupported CLI'),
            subprocess.CompletedProcess([], 0, stdout=help_text),
            subprocess.CalledProcessError(1, ['codex', 'login', 'status']),
            subprocess.CompletedProcess([], 0, stdout=help_text),
            subprocess.CompletedProcess([], 0),
        ])

        def runner(*args, **kwargs):
            result = next(results)
            if isinstance(result, Exception):
                raise result
            return result

        self.wizard([''] * 12, runner)
        self.assertEqual(sum('Codex executable' in p for p in self.prompts), 4)
        self.assertEqual(sum('Start time' in p for p in self.prompts), 1)
        self.assertEqual(self.saved()['start'], '22:00')

    def test_cancelled_setup_preserves_existing_config(self):
        previous = dict(working_directory=str(self.parent), start='22:00', end='07:00')
        self.target.write_text(json.dumps(previous))
        with patch('builtins.input', side_effect=['yes', EOFError()]):
            with self.assertRaises(EOFError):
                setup(self.root)
        self.assertEqual(self.saved(), previous)
        self.assertFalse(self.target.with_suffix('.tmp').exists())
