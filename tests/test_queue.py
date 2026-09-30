from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from nightwatch.core import initialize, tick, allowed, lock, run_codex


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);initialize(self.root)
        self.c={'start':'22:00','end':'07:00'}
        self.now=datetime(2026,1,1,23)
        quota = patch('nightwatch.quota.remaining_quota', return_value=100)
        self.quota = quota.start()
        self.addCleanup(quota.stop)
    def task(self,name='task.md',text='Do something'):
        p=self.root/'todo'/name;p.write_text(text);return p
    def run_task(self,runner=None):
        return tick(self.root,self.c,runner or (lambda *args:{'status':'completed','report':'Validated.'}),self.now)
    def test_low_quota_leaves_task_unclaimed(self):
        source = self.task()
        original = source.read_bytes()
        self.quota.return_value = 4
        runner = MagicMock()
        self.assertIn('below 5%', self.run_task(runner))
        runner.assert_not_called()
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(list((self.root/'processing').iterdir()), [])

    def test_exact_threshold_is_allowed(self):
        self.task()
        self.quota.return_value = 5
        self.run_task()
        self.assertTrue((self.root/'done/task.md').exists())

    def test_custom_threshold_and_quota_failure(self):
        source = self.task()
        self.c['quota_threshold_percent'] = 10
        self.quota.return_value = 9
        self.assertIn('below 10%', self.run_task())
        self.quota.side_effect = ValueError('Unavailable')
        self.assertIn('Cannot check remaining quota: Unavailable', self.run_task())
        self.assertTrue(source.exists())

    def test_no_quota_request_without_eligible_work(self):
        self.assertEqual(self.run_task(), 'empty')
        self.task()
        tick(self.root, self.c, now=datetime(2026,1,1,12))
        (self.root/'.nightwatch/stop').touch()
        self.run_task()
        self.quota.assert_not_called()

    def test_shutdown_during_quota_read_prevents_claim(self):
        source = self.task()
        def quota(c):
            (self.root/'.nightwatch/stop').touch()
            return 100
        self.quota.side_effect = quota
        self.assertEqual(self.run_task(), 'stopping')
        self.assertTrue(source.exists())

    def test_hours(self):
        for hour,expected in [(0,True),(6,True),(7,False),(21,False),(22,True)]:
            self.assertEqual(allowed('22:00','07:00',datetime(2026,1,1,hour)),expected)
        self.assertTrue(allowed('09:00','17:00',datetime(2026,1,1,9)))
        self.assertFalse(allowed('09:00','17:00',datetime(2026,1,1,17)))
        with self.assertRaises(ValueError):allowed('09:00','09:00')
    def test_oldest_and_report(self):
        old=self.task('old.md');new=self.task('new.md')
        os.utime(old,(1,1));os.utime(new,(2,2))
        self.run_task()
        self.assertTrue(new.exists());self.assertIn('Validated.',(self.root/'done/old.md').read_text())
    def test_processing_blocks(self):
        self.task();(self.root/'processing/unexpected.txt').touch()
        self.assertIn('blocked',self.run_task())
    def test_failures_and_feedback(self):
        self.task()
        def fail(*args):raise RuntimeError('permission unavailable')
        self.run_task(fail)
        self.assertIn('permission unavailable',(self.root/'needs_feedback/task.md').read_text())
        self.task('question.md')
        self.run_task(lambda *a:{'status':'needs_feedback','report':'Which repository?'})
        self.assertIn('Which repository?',(self.root/'needs_feedback/question.md').read_text())
    def test_collision_preserves_prior_result(self):
        (self.root/'done/task.md').write_text('prior')
        self.task();self.run_task()
        self.assertEqual((self.root/'done/task.md').read_text(),'prior')
        self.assertEqual(len(list((self.root/'done').glob('*.md'))),2)
    def test_window_and_stop_prevent_claim(self):
        self.task()
        self.assertEqual(tick(self.root,self.c,now=datetime(2026,1,1,12)),'outside permitted hours')
        (self.root/'.nightwatch/stop').touch()
        self.assertEqual(self.run_task(),'stopping')
    def test_cross_process_lock(self):
        code='from pathlib import Path; from nightwatch.core import lock\nwith lock(Path('+repr(str(self.root))+')) as acquired: print(acquired)'
        with lock(self.root) as acquired:
            self.assertTrue(acquired)
            output=subprocess.check_output([sys.executable,'-c',code],text=True)
            self.assertEqual(output.strip(),'False')
        with lock(self.root) as acquired:self.assertTrue(acquired)
    def test_contended_windows_lock_does_not_read_locked_byte(self):
        handle = MagicMock()
        handle.fileno.return_value = 123
        handle.read.side_effect = PermissionError('byte is locked by another process')
        handle.__enter__.return_value = handle
        windows = MagicMock()
        windows.locking.side_effect = OSError('lock unavailable')
        with patch('nightwatch.core.os.name', 'nt'), patch.dict(sys.modules, {'msvcrt': windows}), \
             patch.object(Path, 'open', return_value=handle), \
             patch('nightwatch.core.os.fstat', return_value=type('Stat', (), {'st_size': 1})()):
            with lock(self.root) as acquired:
                self.assertFalse(acquired)
        handle.read.assert_not_called()
        windows.locking.assert_called_once()

    def test_interruption_preserves_claim(self):
        self.task()
        def crash(*args):raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):self.run_task(crash)
        self.assertTrue((self.root/'processing/task.md').exists())
    def test_symlinks_ignored(self):
        if os.name=='nt':self.skipTest('Windows symlinks require special privileges')
        (self.root/'todo/link.md').symlink_to(self.root/'elsewhere.md')
        (self.root/'elsewhere.md').write_text('outside')
        self.assertEqual(self.run_task(),'empty')
    @unittest.skipIf(os.name=='nt','Executable fake uses a Unix shebang')
    def test_codex_subprocess_contract(self):
        fake=self.root/'fake-codex'
        fake.write_text('#!'+sys.executable+'\nimport sys,json\nfrom pathlib import Path\na=sys.argv\nassert "--sandbox" in a and a[a.index("--sandbox")+1]=="workspace-write"\nassert \'approval_policy="never"\' in a\nprompt=sys.stdin.read()\nassert "TASK MARKDOWN:" in prompt\nPath(a[a.index("--output-last-message")+1]).with_name("prompt.md").write_text(prompt,encoding="utf-8")\nPath(a[a.index("--output-last-message")+1]).write_text(json.dumps({"status":"completed","report":"Fake validation passed."}))\n')
        fake.chmod(0o755)
        c=dict(self.c,codex=str(fake),working_directory=str(self.root))
        self.task()
        tick(self.root,c,now=self.now)
        self.assertIn('Fake validation passed.',(self.root/'done/task.md').read_text())
        captured=next((self.root/'.nightwatch').glob('*/prompt.md')).read_text(encoding='utf-8')
        context_text, markdown=captured.split('TASK CONTEXT (JSON):\n',1)[1].split('\n\nTASK MARKDOWN:\n',1)
        context=json.loads(context_text)
        self.assertEqual(context['task_filename'],'task.md')
        self.assertEqual(context['task_file'],str(self.root/'processing/task.md'))
        self.assertEqual(context['original_todo_file'],str(self.root/'todo/task.md'))
        self.assertEqual(context['working_directory'],str(self.root))
        self.assertEqual(context['queue_state'],'processing')
        self.assertEqual(markdown,'Do something')
        self.assertTrue(context['run_id'])


    @unittest.skipIf(os.name=='nt','Executable fake uses a Unix shebang')
    def test_bad_codex_result_routes_to_feedback(self):
        fake=self.root/'fake-codex'
        fake.write_text('#!'+sys.executable+'\nimport sys\nfrom pathlib import Path\na=sys.argv\nPath(a[a.index("--output-last-message")+1]).write_text("not json")\n')
        fake.chmod(0o755)
        c=dict(self.c,codex=str(fake),working_directory=str(self.root))
        self.task()
        tick(self.root,c,now=self.now)
        self.assertIn('JSONDecodeError',(self.root/'needs_feedback/task.md').read_text())
    def test_edits_move_task_later(self):
        a=self.task('a.md');b=self.task('b.md')
        os.utime(a,(3,3));os.utime(b,(2,2))
        self.run_task()
        self.assertTrue((self.root/'done/b.md').exists())
    def test_finish_after_window_closes(self):
        self.task()
        def runner(*args):
            (self.root/'.nightwatch/stop').touch()
            return {'status':'completed','report':'Finished active work after shutdown request.'}
        self.run_task(runner)
        self.assertTrue((self.root/'done/task.md').exists())

if __name__=='__main__':unittest.main()
