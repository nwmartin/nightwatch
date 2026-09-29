import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from .core import initialize, config, tick, lock, allowed, finish

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


def setup(root):
    target = root/'.nightwatch/config.json'
    if target.exists() and input('Replace existing local configuration? [y/N] ').lower() != 'y':
        return
    directory = Path(input('Default working directory (existing projects folder): ').strip()).expanduser().resolve()
    binary = input(f'Codex executable [{shutil.which("codex") or "codex"}]: ').strip() or shutil.which('codex') or 'codex'
    binary = shutil.which(binary) or str(Path(binary).expanduser().resolve())
    if Path(binary).suffix.lower() in ('.cmd','.bat'):
        raise ValueError('Select the native codex.exe, rather than the npm .cmd wrapper. See README.md.')
    start = input('Start time, local HH:MM (e.g. 22:00): ').strip()
    end = input('End time, local HH:MM (e.g. 07:00): ').strip()
    allowed(start,end)
    poll = int(input('Check interval in seconds [60]: ').strip() or '60')
    model = input('Codex model (blank uses CLI default): ').strip()
    if not directory.is_dir() or poll < 1:
        raise ValueError('Choose an existing directory and positive polling interval.')
    help_result = subprocess.run([binary,'exec','--help'], capture_output=True,text=True,check=True)
    for flag in ('--ignore-user-config','--ignore-rules','--output-schema','--output-last-message'):
        if flag not in help_result.stdout:
            raise ValueError('Codex is missing '+flag+'. Update the CLI before setup.')
    subprocess.run([binary,'login','status'],check=True)
    c = dict(working_directory=str(directory),codex=binary,start=start,end=end,poll_seconds=poll,model=model)
    temp = target.with_suffix('.tmp')
    temp.write_text(json.dumps(c,indent=2)+'\n',encoding='utf-8')
    temp.replace(target)
    print('Local configuration saved. Queue is not started.')
    if sys.platform.startswith('linux') and input('Install reboot-persistent systemd user service? [y/N] ').lower()=='y':
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
        while not stop.exists():
            c = config(root)
            print(tick(root,c),flush=True)
            deadline = time.monotonic()+c['poll_seconds']
            while time.monotonic()<deadline and not stop.exists():
                time.sleep(min(0.5,max(0,deadline-time.monotonic())))
        print('Nightwatch stopped; no further tasks will be claimed.',flush=True)


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
        else:run(root)
    elif args.command=='once':print(tick(root,config(root)))
    elif args.command=='shutdown':
        (root/'.nightwatch/stop').touch()
        print('Shutdown requested. Any active task is allowed to finish; the daemon then exits.')
    elif args.command=='status':
        with lock(root,'daemon') as acquired:print('Daemon: '+('stopped' if acquired else 'running'))
        for name in ('todo','processing','done','needs_feedback'):
            print(name+': '+', '.join(p.name for p in sorted((root/name).iterdir()) if p.name!='.gitkeep'))
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
