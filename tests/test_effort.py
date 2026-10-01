import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from nightwatch.core import run_codex, reasoning_effort


class EffortTests(unittest.TestCase):
    def test_validation(self):
        self.assertEqual(reasoning_effort(' HIGH '), 'high')
        for value in ('typo', None, 3):
            with self.assertRaises(ValueError): reasoning_effort(value)

    def test_override_and_backwards_compatible_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'.nightwatch').mkdir()
            task=root/'task.md'; task.write_text('Test')
            def execute(command, **kwargs):
                Path(command[command.index('--output-last-message')+1]).write_text(json.dumps({'status':'completed','report':'Done'}))
                return subprocess.CompletedProcess(command, 0)
            for i, effort in enumerate((None, 'default', 'high')):
                c={'codex':'codex','working_directory':directory}
                if effort is not None: c['reasoning_effort']=effort
                with patch('nightwatch.core.subprocess.run', side_effect=execute) as runner:
                    run_codex(root,c,task,str(i))
                arguments=runner.call_args.args[0]
                overrides=[a for a in arguments if a.startswith('model_reasoning_effort=')]
                self.assertEqual(overrides, ['model_reasoning_effort="high"'] if effort=='high' else [])
