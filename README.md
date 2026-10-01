# Nightwatch

An unattended local Codex CLI worker for Markdown tasks. Python 3.10+; no Python packages required.

## Setup

Clone this repository, install a current Codex CLI, and authenticate with `codex login`.
From this directory run `./setup` on Linux, or `setup.cmd` on Windows (Python's `py` launcher required).
Setup asks for an existing default working directory, Codex executable, local start/end hours,
polling interval, remaining quota threshold, and optional model. Press Enter to accept the displayed defaults: the clone’s
parent directory, Codex from PATH, 22:00–07:00, a 60-second interval, a 5% quota threshold, and the CLI’s default model.
When replacing a configuration, saved values become the defaults. Invalid answers display an
error immediately and retry that question without losing earlier answers. Times use local HH:MM
(24-hour format); an hour such as 7:00 is accepted and saved as 07:00. Yes/no prompts accept
`y`/`yes` and `n`/`no`, defaulting to no. Each clone has its own ignored `.nightwatch/config.json`.
No machine-specific settings or credentials are committed. Equal start/end times are rejected.
An overnight window such as 22:00–07:00 works. Time follows the machine's local clock, including DST;
only task starts are restricted. Running tasks finish even after the window closes.

On Windows, select the native `codex.exe` (not an npm `codex.cmd` shim). For an npm installation,
locate it under that package's platform-specific vendor directory, or install the standalone
native CLI. Configure Codex's Windows sandbox interactively before unattended use. Nightwatch
never falls back to an unsandboxed runner. Unsupported CLI flags fail setup with an update message.

## Tasks

Write UTF-8 `.md` files in `todo/`:

```markdown
# Update the project documentation

In the widget project under the configured working directory, check that README
installation steps agree with the package scripts. Fix inaccuracies and report
what changed. Do not publish anything.
```

Files are claimed by oldest modification time (filename breaks ties). Editing a task places it
later in the queue. Save completely before moving files into `todo/`; use an editor outside the
queue or an atomic rename. Only regular, non-symlink Markdown files are selected. Queues are flat.

- `todo/`: pending prompts.
- `processing/`: claimed task. Any entry other than `.gitkeep` blocks new claims.
- `done/`: completed task with an appended dated Markdown report.
- `needs_feedback/`: questions, permission blockers, CLI failures, invalid results, or recovered interruptions.

Answer questions directly in the task, review partial changes, then move it back to `todo/`.
Every Codex invocation receives the task filename, its absolute `processing/` path,
original `todo/` path, queue state, Nightwatch directory, working directory, and run ID
as explicit context, followed by the complete Markdown content. Queue paths identify
the task; Nightwatch alone appends reports and moves queue files.
Requeued tasks start fresh Codex sessions with the entire Markdown history. No automatic retries.
Destination filename collisions get a unique suffix; old results are never overwritten.
Task files, configuration, transcripts, and runtime state are ignored by Git. Keep your own backups.
Queue placeholders are `.gitkeep`; all prompts and appended reports are Markdown. Internal protocol
and configuration files use JSON.

## Run and stop

```sh
./start --foreground        # Linux/macOS: stay attached overnight
./shutdown                 # From another terminal; finish the active task, then exit
./status                   # Show daemon, latest processing window, and queue contents
python3 -m nightwatch once # At most one task; observes schedule and shutdown marker
```

Windows: `start.cmd --foreground`, `shutdown.cmd`, and `status.cmd`.

`status` lists the tasks started in the latest configured time window, with their
outcomes (`completed`, `needs_feedback`, or `failed`), start/finish times, and paths
to their Markdown reports. An unfinished entry says `started`, which can mean a
task is running or execution was interrupted. The worker keeps this information
in `.nightwatch/latest-window.json`, replacing it on its first check inside a new
window, even if there are no tasks. Overnight windows share one record across
midnight; tasks that finish after the window closes stay in that window's record.
During the day, the previous window remains visible. Before the first worker check
after this feature is installed, status reports that no history has been recorded;
existing archived tasks are not backfilled. No database is needed.
Status reports `Daemon: running` or `Daemon: stopped` using the daemon's process lock,
then lists the tasks in each queue. It does not start or stop the daemon.
An interactive foreground terminal shows a waiting spinner when `todo/` is empty and
a filename spinner while Codex is working. Timestamped started/processed lines remain
in the console, including the destination queue and outcome. Outside-hours and blocked
processing states have distinct messages. Redirected output and service journals use
plain text and only log status changes.

Foreground mode works without installing any service. Keep the terminal open and prevent the
machine from sleeping if you want overnight work. Native Windows has no automatic service installer
in this version. Closing the terminal or shutting down the OS can interrupt execution.

A shutdown request remains in `.nightwatch/stop` until the next `start`/`run`, which clears it.
`once` respects this marker. Ctrl+C or SIGTERM asks the daemon to drain gracefully; Codex runs in
its own process group. No execution deadline is imposed: a hung task requires manual investigation.

Before claiming each eligible task, Nightwatch reads the authenticated account’s remaining quota
using `codex app-server` (`account/rateLimits/read`), without starting a model turn. It uses the
smaller remaining percentage of the reported primary and secondary quota windows. If that value
is **below 5%**, the worker exits and leaves pending tasks untouched; exactly 5% is allowed.
Setup lets you change this threshold (`quota_threshold_percent` in local configuration), from
greater than 0 through 100 percent. Existing configurations default to 5%.

An unavailable, invalid, or timed-out quota response also stops the worker before claiming work,
with a console message. Use a CLI supporting the app-server quota interface and an account that
reports percentage rate limits; accounts without those limits cannot run guarded tasks. Quota
reads have a 30-second timeout. Restart Nightwatch after quota recovers or after fixing the check.
A running task is allowed to finish and may consume more than the reserved percentage; this is
a check between tasks, not a hard cap during execution. Quota checking needs the Codex client’s
normal service connectivity, just as task execution does.

## Linux service and reboot survival

Setup optionally installs a per-clone systemd user service. You can also run
`python3 -m nightwatch install-service`. It enables startup but does not start processing until
`./start`. Once installed, `./start` starts the service; `./start --foreground` remains available.
OS locks prevent multiple workers from claiming concurrently. The service uses the setup Python
interpreter and absolute repository path; reinstall it if you move the clone.

For boot without logging in, run `loginctl enable-linger "$USER"`. This may require administrator
permission and keeps user services alive after logout. Setup displays this command rather than
silently changing account policy. `./shutdown` drains the service without disabling next-boot startup.

Find the unit name with `systemctl --user list-unit-files 'nightwatch-*'`.
Inspect it using `journalctl --user -u UNIT_NAME`. Disable startup with
`systemctl --user disable UNIT_NAME`; then use `./shutdown`. After it has stopped, delete the unit
from `~/.config/systemd/user/` and run `systemctl --user daemon-reload` to uninstall it.

## Permissions and Codex integration

Nightwatch uses `codex exec`, a structured final-response schema, `workspace-write`, and
`approval_policy="never"`. Changes inside the selected working directory are authorized; requests
for additional permissions or decisions must become feedback. User config and exec-policy rules
are ignored so broad personal approvals aren't inherited. Existing Codex authentication still applies.
Network access from sandboxed commands is explicitly disabled. The Codex client itself still needs
network access to contact the model service. No API key is stored by Nightwatch.

The OS sandbox is the execution boundary, not a prompt-based guarantee. Codex can normally read
outside the working directory and may write to permitted temporary directories. Project instructions
still apply. Choose your working directory deliberately; a projects directory permits edits across
its projects. If Nightwatch itself is under that directory, it is also inside that writable scope.
Use trusted tasks and review results. Model-reported completion is validated structurally, not proof
that the requested work was done correctly. Nightwatch does not roll back changes on failure.

See [official noninteractive Codex documentation](https://developers.openai.com/codex/noninteractive).
Setup checks the installed CLI for the integration flags. Execution transcripts are retained as
`.nightwatch/RUN_ID/transcript.md`; protocol results and schema are stored beside them. These can
contain task data. There is no automatic log cleanup.

## Interrupted work

Crashes and reboot leave the task in `processing/`, blocking duplicate execution. Check that no
orphaned Codex process is still working and review changes before running:

```sh
python3 -m nightwatch recover --reason "Execution interrupted; reviewed changes. Need to finish validation."
```

Recovery refuses while the worker lock is held and moves the task to `needs_feedback/` with the
reason appended. It never reruns work. An unexpected non-Markdown processing entry must be handled
manually. The queue lock protects this clone on a local filesystem, not separate clones or hosts.
Use local storage, not a shared network drive.

## Development

`python3 -m unittest discover -s tests -v`

Tests use temporary queues and a fake Codex executable; they incur no model usage and make no
external changes. Linux is locally tested. Windows-specific locking and launchers require Windows
validation; the CI matrix exercises the portable suite on Linux and Windows.

## Reasoning effort (exertion)

`./setup` asks for reasoning effort after the model. Choose `default` to leave the
Codex/model default unchanged, or `none`, `minimal`, `low`, `medium`, `high`,
`xhigh`, `max`, or `ultra`. Available levels depend on your model and CLI;
Nightwatch validates the spelling, not model compatibility. Unsupported combinations
are reported through the normal failure/feedback flow.

The selection is saved as `reasoning_effort` in `.nightwatch/config.json` and passed
as an explicit `model_reasoning_effort` Codex config override on every task. Existing
configurations without this field retain their previous default behavior. Re-running
setup preserves the previous selection; enter `default` to clear an override.

See [Codex configuration reference](https://developers.openai.com/codex/config-reference#model_reasoning_effort).
