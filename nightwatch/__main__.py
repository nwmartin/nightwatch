import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from .console import Console
from .core import initialize, config, tick, lock, allowed, finish, quota_threshold
from .history import print_history

ROOT = Path(__file__).resolve().parent.parent


def service_name(root):
    return 'nightwatch-' + hashlib.sha256(str(root).encode()).hexdigest()[:12] + '.service'


def quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def install_service(root):
    if not sys.platform.startswith('linux') or not shutil.which('systemctl'):
        raise ValueError('User services require Linux/systemd. Use start --foreground on this platform.')
    target = Path.home()/'.config/systemd/user'/service_name(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f'''[Unit]
Description=Nightwatch Markdown task queue

[Service]
Type=simple
Environment={quote("PATH=" + os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"))}
WorkingDirectory={str(root).replace('%', '%%')}
ExecStart={quote(sys.executable)} -m nightwatch run
Restart=on-failure
RestartSec=10
KillMode=mixed
TimeoutStopSec=infinity

[Install]
WantedBy=default.target
''', encoding='utf-8')
    subprocess.run(['systemctl','--user','daemon-reload'],check=True)
    subprocess.run(['systemctl','--user','enable',service_name(root)],check=True)
    print('Service installed and enabled; not started. Run ./start when ready.')
    print('For boot without login: loginctl enable-linger ' + os.environ.get('USER','YOUR_USERNAME'))
    print('That command may require administrator authorization. It keeps your user services running after logout.')


def ask(prompt, default, validate):
    """Validate one answer at a time, keeping previous answers on a retry."""
    while True:
        answer = input(f'{prompt} [{default}]: ').strip() or str(default)
        try:
            return validate(answer)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            print(f'Invalid answer: {exc} Please try again.')


def yes_no(prompt):
    def validate(answer):
        if answer.lower() in ('y', 'yes'):
            return True
        if answer.lower() in ('n', 'no'):
            return False
        raise ValueError('Enter yes or no.')
    return ask(prompt, 'N', validate)


def setup(root):
    target = root/'.nightwatch/config.json'
    previous = {}
    if target.exists():
        if not yes_no('Replace existing local configuration?'):
            return
        try:
            previous = json.loads(target.read_text(encoding='utf-8'))
            if not isinstance(previous, dict):
                previous = {}
        except (ValueError, OSError):
            print('Existing configuration could not be read; using setup defaults.')

    def directory_value(answer):
        directory = Path(answer).expanduser().resolve()
        if not directory.is_dir():
            raise ValueError('Choose an existing working directory.')
        return directory

    def binary_value(answer):
        binary = shutil.which(answer) or str(Path(answer).expanduser().resolve())
        if Path(binary).suffix.lower() in ('.cmd', '.bat'):
            raise ValueError('Select the native codex.exe, rather than the npm .cmd wrapper. See README.md.')
        help_result = subprocess.run([binary, 'exec', '--help'], capture_output=True, text=True, check=True)
        for flag in ('--ignore-user-config', '--ignore-rules', '--output-schema', '--output-last-message'):
            if flag not in help_result.stdout:
                raise ValueError('Codex is missing '+flag+'. Update the CLI or choose another executable.')
        subprocess.run([binary, 'login', 'status'], check=True)
        return binary

    def time_value(answer):
        if not re.fullmatch(r'[0-9]{1,2}:[0-9]{2}', answer):
            raise ValueError('Enter a local time in HH:MM format, such as 22:00 or 07:00.')
        hour, minute = map(int, answer.split(':'))
        if hour > 23 or minute > 59:
            raise ValueError('Hours must be 0–23 and minutes 0–59.')
        return f'{hour:02d}:{minute:02d}'

    def end_value(answer):
        end = time_value(answer)
        allowed(start, end)
        return end

    def poll_value(answer):
        try:
            poll = int(answer)
        except ValueError:
            raise ValueError('Enter a positive whole number of seconds.') from None
        if poll < 1:
            raise ValueError('Enter a positive whole number of seconds.')
        return poll

    directory = ask('Default working directory (existing projects folder)', previous.get('working_directory', root.parent), directory_value)
    binary = ask('Codex executable', previous.get('codex', shutil.which('codex') or 'codex'), binary_value)
    start = ask('Start time, local HH:MM', previous.get('start', '22:00'), time_value)
    end = ask('End time, local HH:MM', previous.get('end', '07:00'), end_value)
    poll = ask('Check interval in seconds', previous.get('poll_seconds', 60), poll_value)
    threshold = ask('Stop below remaining quota (%)', previous.get('quota_threshold_percent', 5), quota_threshold)
    model = ask('Codex model (blank uses CLI default)', previous.get('model', ''), lambda answer: answer)
    c = dict(working_directory=str(directory), codex=binary, start=start, end=end, poll_seconds=poll, quota_threshold_percent=threshold, model=model)
    temp = target.with_suffix('.tmp')
    temp.write_text(json.dumps(c, indent=2)+'\n', encoding='utf-8')
    temp.replace(target)
    print('Local configuration saved. Queue is not started.')
    if sys.platform.startswith('linux') and yes_no('Install reboot-persistent systemd user service?'):
        install_service(root)


def run(root):
    with lock(root,'daemon') as acquired:
        if not acquired:
            print('Nightwatch is already running.'); return
        stop = root/'.nightwatch/stop'
        stop.unlink(missing_ok=True)
        def request_stop(*args):
            stop.touch()
        signal.signal(signal.SIGTERM,request_stop)
        signal.signal(signal.SIGINT,request_stop)
        # Child starts in a separate process group so Ctrl+C requests a graceful stop.
        with Console() as console:
            console.log('Nightwatch started. Ctrl+C or ./shutdown stops after the current task.')
            while not stop.exists():
                c = config(root)
                result = tick(root, c, on_event=console.event)
                if result.startswith('quota stop:'):
                    console.log(result)
                    break
                messages = {
                    'empty': 'Waiting for tasks',
                    'busy': 'Waiting for another worker',
                    'blocked: processing is not empty': 'Blocked: processing contains an unfinished task; inspect before recovery',
                    'outside permitted hours': f"Waiting for permitted hours ({c['start']}–{c['end']} local)",
                    'stopping': 'Shutting down',
                }
                console.status(messages.get(result, 'Waiting for next queue check'))
                deadline = time.monotonic()+c['poll_seconds']
                while time.monotonic()<deadline and not stop.exists():
                    time.sleep(min(0.5,max(0,deadline-time.monotonic())))
            console.log('Nightwatch stopped; no further tasks will be claimed.')



def main():
    parser=argparse.ArgumentParser(description='Nightwatch local Markdown task queue')
    parser.add_argument('command',choices=['setup','start','run','once','shutdown','status','recover','install-service'])
    parser.add_argument('--foreground',action='store_true')
    parser.add_argument('--reason',default='Interrupted execution. Inspect partial changes, answer any blockers, then requeue manually.')
    args=parser.parse_args()
    root=ROOT
    initialize(root)
    if args.command=='setup':setup(root)
    elif args.command=='install-service':config(root);install_service(root)
    elif args.command in ('run','start'):
        config(root)
        unit=Path.home()/'.config/systemd/user'/service_name(root)
        if args.command=='start' and not args.foreground and sys.platform.startswith('linux') and unit.exists():
            subprocess.run(['systemctl','--user','start',service_name(root)],check=True)
            print('Nightwatch service started. Use ./start --foreground for the live console when the service is stopped.')
            print('Service logs: journalctl --user -f -u ' + service_name(root))
        else:run(root)
    elif args.command=='once':print(tick(root,config(root)))
    elif args.command=='shutdown':
        (root/'.nightwatch/stop').touch()
        print('Shutdown requested. Any active task is allowed to finish; the daemon then exits.')
    elif args.command=='status':
        with lock(root,'daemon') as acquired:print('Daemon: '+('stopped' if acquired else 'running'))
        print_history(root)
        for name in ('todo','processing','done','needs_feedback'):
            with os.scandir(root/name) as entries:
                count = sum(1 for entry in entries if entry.name != '.gitkeep')
            print(f'{name}: {count}')
    elif args.command=='recover':
        with lock(root) as acquired:
            if not acquired:raise ValueError('Worker is active; cannot recover.')
            tasks=[p for p in (root/'processing').iterdir() if p.is_file() and not p.is_symlink() and p.suffix.lower()=='.md']
            if not tasks:print('No Markdown tasks to recover.')
            for task in tasks:print(finish(root,task,'needs_feedback',args.reason))


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,KeyError,subprocess.SubprocessError) as exc:
        print('Nightwatch: '+str(exc),file=sys.stderr)
        sys.exit(1)
