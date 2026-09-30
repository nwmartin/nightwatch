"""Read account quota through Codex's newline-delimited app-server protocol."""
import json
import math
from queue import Queue, Empty
import subprocess
from threading import Thread
import time


def quota_percent(snapshot):
    """Use the tightest primary/secondary window in the account snapshot."""
    limits = snapshot.get('rateLimits')
    if not isinstance(limits, dict):
        raise ValueError('Codex returned no rate-limit snapshot.')
    remaining = []
    for name in ('primary', 'secondary'):
        window = limits.get(name)
        if window is None:
            continue
        used = window.get('usedPercent') if isinstance(window, dict) else None
        if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used) or not 0 <= used <= 100:
            raise ValueError('Codex returned an invalid quota window.')
        remaining.append(100 - used)
    if not remaining:
        raise ValueError('Account has no percentage quota available; percentage checks require reported rate limits.')
    return min(remaining)


def remaining_quota(c, timeout=30):
    """No model turn is started. Bound reads and always reap the helper process."""
    process = subprocess.Popen([c['codex'], 'app-server', '--stdio'],
                               cwd=c['working_directory'], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, encoding='utf-8')
    messages = Queue()

    def read():
        try:
            for line in process.stdout:
                messages.put(line)
        finally:
            messages.put(None)

    reader = Thread(target=read, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout

    def send(message):
        process.stdin.write(json.dumps(message) + '\n')
        process.stdin.flush()

    def response(request_id):
        while True:
            if time.monotonic() >= deadline:
                raise ValueError('Codex quota check timed out.')
            try:
                line = messages.get(timeout=max(0, deadline - time.monotonic()))
            except Empty:
                raise ValueError('Codex quota check timed out.') from None
            if line is None:
                raise ValueError('Codex app-server exited before reporting quota.')
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError('Codex returned an invalid protocol message.')
            if message.get('id') != request_id:
                continue
            if 'error' in message:
                raise ValueError('Codex rejected the quota request; check authentication and app-server support.')
            if not isinstance(message.get('result'), dict):
                raise ValueError('Codex returned an invalid quota response.')
            return message['result']

    try:
        send({'id': 1, 'method': 'initialize', 'params': {
            'clientInfo': {'name': 'nightwatch', 'version': '1.0'}}})
        response(1)
        send({'method': 'initialized'})
        send({'id': 2, 'method': 'account/rateLimits/read'})
        return quota_percent(response(2))
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        reader.join(timeout=5)
        process.stdin.close()
        process.stdout.close()
