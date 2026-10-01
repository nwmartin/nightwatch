"""Portable, single-consumer Markdown task queue."""
from contextlib import contextmanager
from datetime import datetime, time
import json
import math
import os
from pathlib import Path
import subprocess
import uuid
from .history import begin_window, save_history

QUEUES = ('todo', 'processing', 'done', 'needs_feedback')
SCHEMA = {'type': 'object', 'properties': {
    'status': {'type': 'string', 'enum': ['completed', 'needs_feedback', 'failed']},
    'report': {'type': 'string'},
}, 'required': ['status', 'report'], 'additionalProperties': False}


def initialize(root):
    for name in (*QUEUES, '.nightwatch'):
        (root/name).mkdir(exist_ok=True)


@contextmanager
def lock(root, name='worker'):
    """OS locks survive neither crashes nor reboot; never unlink the lock file."""
    with (root/'.nightwatch'/f'{name}.lock').open('a+b') as f:
        # Windows byte-range locks also block reads. Inspect file size without
        # touching the locked byte before attempting the nonblocking lock.
        if os.fstat(f.fileno()).st_size == 0:
            f.write(b'0'); f.flush()
        f.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            if os.name == 'nt':
                f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)


def allowed(start, end, now=None):
    current = (now or datetime.now()).time().replace(tzinfo=None)
    a, b = time.fromisoformat(start), time.fromisoformat(end)
    if a == b:
        raise ValueError('Start and end must differ; choose an explicit daily window.')
    return a <= current < b if a < b else current >= a or current < b


def quota_threshold(value):
    if isinstance(value, bool):
        raise ValueError('Quota threshold must be greater than 0 and at most 100 percent.')
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError('Quota threshold must be greater than 0 and at most 100 percent.') from None
    if not math.isfinite(value) or not 0 < value <= 100:
        raise ValueError('Quota threshold must be greater than 0 and at most 100 percent.')
    return value


REASONING_EFFORTS = ('default', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')


def reasoning_effort(value):
    if not isinstance(value, str) or value.strip().lower() not in REASONING_EFFORTS:
        raise ValueError('Choose a reasoning effort: ' + ', '.join(REASONING_EFFORTS))
    return value.strip().lower()


def config(root):
    c = json.loads((root/'.nightwatch/config.json').read_text(encoding='utf-8'))
    if not Path(c['working_directory']).is_absolute() or not Path(c['working_directory']).is_dir():
        raise ValueError('Working directory must be an existing absolute path.')
    allowed(c['start'], c['end'])
    if not isinstance(c['poll_seconds'], int) or c['poll_seconds'] < 1:
        raise ValueError('poll_seconds must be a positive integer.')
    if not Path(c['codex']).is_file():
        raise ValueError('Configured Codex executable does not exist.')
    c['reasoning_effort'] = reasoning_effort(c.get('reasoning_effort', 'default'))
    c['quota_threshold_percent'] = quota_threshold(c.get('quota_threshold_percent', 5))
    return c


def finish(root, task, status, report):
    destination = root/('done' if status == 'completed' else 'needs_feedback')/task.name
    if destination.exists():
        destination = destination.with_name(f'{destination.stem}-{uuid.uuid4().hex[:12]}.md')
    with task.open('a', encoding='utf-8') as f:
        f.write(f'\n\n---\n\n## Nightwatch · {datetime.now().astimezone().isoformat()} · {status}\n\n{report}\n')
        f.flush(); os.fsync(f.fileno())
    task.rename(destination)
    return destination


def run_codex(root, c, task, run_id):
    prompt = task.read_text(encoding='utf-8')
    context = {
        'task_filename': task.name,
        'task_file': str(task.resolve()),
        'original_todo_file': str((root/'todo'/task.name).resolve()),
        'queue_state': 'processing',
        'nightwatch_directory': str(root.resolve()),
        'working_directory': c['working_directory'],
        'run_id': run_id,
    }
    run = root/'.nightwatch'/run_id
    run.mkdir()
    schema = run/'schema.json'
    schema.write_text(json.dumps(SCHEMA), encoding='utf-8')
    output = run/'result.json'
    command = [c['codex'], 'exec', '--ignore-user-config', '--ignore-rules',
               '-c', 'approval_policy="never"', '--sandbox', 'workspace-write',
               '-c', 'sandbox_workspace_write.network_access=false',
               '--skip-git-repo-check', '--cd', c['working_directory'],
               '--output-schema', str(schema), '--output-last-message', str(output), '-']
    effort = reasoning_effort(c.get('reasoning_effort', 'default'))
    if effort != 'default':
        command[2:2] = ['-c', 'model_reasoning_effort=' + json.dumps(effort)]
    if c.get('model'):
        command[2:2] = ['--model', c['model']]
    instructions = '''You are running an unattended Nightwatch task. Work only in the configured
working directory. Do not manage or modify Nightwatch queue files. Perform the task,
validate the result, and return JSON matching the supplied schema. report is Markdown.
If information, permission, broader filesystem access, network access, or an external
side effect requiring approval is needed, stop and return needs_feedback with specific
questions, any partial changes, and what remains. Never bypass sandbox restrictions or
seek interactive approval. Return completed only if all requested work is complete.
Return failed for other failures. Previous Nightwatch reports are history; the user's
new answers may resolve earlier blockers. Do not silently repeat completed side effects.

The task file paths below identify the prompt being performed; they are context,
not permission to edit or move queue files. Nightwatch appends your report and moves
the task after you return. Use the configured working directory for task work, not
the processing directory. The complete task Markdown is included below, so you do
not need to reopen the task file.

TASK CONTEXT (JSON):
'''
    instructions += json.dumps(context, ensure_ascii=False, indent=2) + '\n\nTASK MARKDOWN:\n' 
    with (run/'transcript.md').open('w', encoding='utf-8') as log:
        log.write('# Codex execution transcript\n\n'); log.flush()
        result = subprocess.run(command, input=instructions+prompt, text=True,
                                encoding='utf-8', stdout=log, stderr=log,
                                start_new_session=os.name != 'nt',
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
    if result.returncode:
        raise RuntimeError(f'Codex exited with status {result.returncode}. Inspect {run}/transcript.md; changes may be partial.')
    data = json.loads(output.read_text(encoding='utf-8'))
    if set(data) != {'status', 'report'} or data['status'] not in ('completed','needs_feedback','failed') or not isinstance(data['report'], str) or not data['report'].strip():
        raise ValueError('Codex returned an invalid or empty outcome.')
    return data


def tick(root, c, runner=run_codex, now=None, on_event=None, quota_reader=None):
    with lock(root) as acquired:
        if not acquired:
            return 'busy'
        history = begin_window(root, c, now) if allowed(c['start'], c['end'], now) else None
        # Even unexpected non-Markdown files block the queue, but tracked placeholders do not.
        if any(p.name != '.gitkeep' for p in (root/'processing').iterdir()):
            return 'blocked: processing is not empty'
        if (root/'.nightwatch/stop').exists():
            return 'stopping'
        if not allowed(c['start'], c['end'], now):
            return 'outside permitted hours'
        tasks = [p for p in (root/'todo').iterdir() if p.suffix.lower()=='.md' and p.is_file() and not p.is_symlink()]
        if not tasks:
            return 'empty'
        from .quota import remaining_quota
        try:
            threshold = quota_threshold(c.get('quota_threshold_percent', 5))
            remaining = (quota_reader or remaining_quota)(c)
            if isinstance(remaining, bool) or not isinstance(remaining, (int, float)) or not math.isfinite(remaining) or not 0 <= remaining <= 100:
                raise ValueError('Invalid remaining quota percentage.')
        except Exception as exc:
            return f'quota stop: Cannot check remaining quota: {exc}'
        if remaining < threshold:
            return f'quota stop: Remaining quota {remaining:g}% is below {threshold:g}%.'
        # A quota read may take long enough for shutdown or the daily window to change.
        if (root/'.nightwatch/stop').exists():
            return 'stopping'
        if not allowed(c['start'], c['end'], now):
            return 'outside permitted hours'
        source = min(tasks, key=lambda p: (p.stat().st_mtime_ns, p.name))
        task = root/'processing'/source.name
        source.rename(task)
        if on_event:
            on_event('started', task)
        run_id = uuid.uuid4().hex
        entry = {'filename': task.name, 'run_id': run_id, 'status': 'started',
                 'started_at': (now or datetime.now().astimezone()).isoformat()}
        history['tasks'].append(entry)
        save_history(root, history)
        try:
            data = runner(root, c, task, run_id)
            outcome = data['status']
            destination = finish(root, task, outcome, data['report'])
        except Exception as exc:
            outcome = 'failed'
            destination = finish(root, task, 'failed', f'{type(exc).__name__}: {exc}\n\nInspect any partial changes before editing this task and moving it back to todo. No automatic retry was attempted.')
        entry.update(status=outcome, finished_at=datetime.now().astimezone().isoformat(),
                     destination=str(destination.relative_to(root)))
        save_history(root, history)
        if on_event:
            on_event('finished', destination, outcome)
        return str(destination)
