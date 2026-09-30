"""A replaceable record of the latest local processing window."""
from datetime import datetime, time, timedelta
import json


def window(c, now=None):
    local_clock = now is None
    now = now or datetime.now()
    start, end = time.fromisoformat(c['start']), time.fromisoformat(c['end'])
    day = now.date()
    if start > end and now.time().replace(tzinfo=None) < end:
        day -= timedelta(days=1)
    first = datetime.combine(day, start, now.tzinfo)
    last = datetime.combine(day + timedelta(days=start > end), end, now.tzinfo)
    if local_clock:
        first, last = first.astimezone(), last.astimezone()
    return {'start': first.isoformat(), 'end': last.isoformat()}


def read_history(root):
    path = root/'.nightwatch/latest-window.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def save_history(root, history):
    path = root/'.nightwatch/latest-window.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(history, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    temporary.replace(path)


def begin_window(root, c, now=None):
    period = window(c, now)
    history = read_history(root)
    if history is None or history['window'] != period:
        history = {'window': period, 'tasks': []}
        save_history(root, history)
    return history


def print_history(root):
    history = read_history(root)
    if history is None:
        print('Latest window: no history recorded yet.')
        return
    period = history['window']
    print(f"Latest window: {period['start']} – {period['end']} (local time)")
    if not history['tasks']:
        print('No tasks ran in this window.')
    for task in history['tasks']:
        print(f"  {task['filename']}: {task['status']} | started {task['started_at']}")
        if task.get('finished_at'):
            print(f"    Finished {task['finished_at']} | report: {task['destination']}")
        else:
            print('    No finish recorded; task may still be running or was interrupted.')
