"""Live terminal status, with quiet, timestamped output for redirected logs."""
from datetime import datetime
import shutil
import sys
import threading


def clean(text):
    return ''.join(c if c.isprintable() else ' ' for c in str(text))


class Console:
    def __init__(self, stream=None):
        self.stream = stream if stream is not None else sys.stdout
        self.tty = self.stream.isatty()
        self.message = ''
        self.width = 0
        self.mutex = threading.Lock()
        self.stop = threading.Event()
        self.thread = None

    def __enter__(self):
        if self.tty:
            self.thread = threading.Thread(target=self.animate, daemon=True)
            self.thread.start()
        return self

    def clear(self):
        if self.width:
            self.stream.write('\r' + ' ' * self.width + '\r')
            self.width = 0

    def log(self, message):
        with self.mutex:
            self.clear()
            self.stream.write(f'[{datetime.now():%Y-%m-%d %H:%M:%S}] {clean(message)}\n')
            self.stream.flush()

    def status(self, message):
        message = clean(message)
        with self.mutex:
            if message == self.message:
                return
            self.message = message
            if not self.tty:
                self.stream.write(f'[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n')
                self.stream.flush()

    def event(self, name, path, outcome=None):
        if name == 'started':
            self.log(f'Started: {path.name}')
            self.status(f'Processing {path.name}')
        else:
            with self.mutex:
                self.message = ''
            self.log(f'Processed: {path.name} -> {path.parent.name} ({outcome})')

    def animate(self):
        index = 0
        while not self.stop.wait(0.1):
            with self.mutex:
                if not self.message:
                    continue
                self.clear()
                columns = max(1, shutil.get_terminal_size((80, 24)).columns - 1)
                line = ('|/-\\'[index % 4] + ' ' + self.message)[:columns]
                self.stream.write('\r' + line)
                self.stream.flush()
                self.width = len(line)
                index += 1

    def __exit__(self, *args):
        self.stop.set()
        if self.thread:
            self.thread.join()
        with self.mutex:
            self.clear()
            self.stream.flush()
