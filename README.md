# hermes-wake

An agent in a chat (Telegram, Matrix, Discord, ...) often starts long work outside
the chat, such as `systemd-run`, `nohup` or a script that writes a report, and says
"I'll report back". Nothing re-enters the conversation when that work ends, so the
human has to ask "any updates?".

With hermes-wake, the agent calls one tool right after it launches the work:

```
wake_me_when(unit="build-42.service", note="read /srv/out/report.md and summarise it")
wake_me_when(pid=81234, note="check the exit status in /tmp/job.log")
wake_me_when(path="/srv/out/report.md")
```

When the work ends, the agent gets a new turn in the same chat (and the same forum
topic or thread) with the note and the outcome. It needs no polling and no further
calls.

![demo](demo/wake-demo.gif)

Full-length demo video: [demo/wake-demo-720.mp4](demo/wake-demo-720.mp4)

## Install

```
hermes plugins install getaskclaw/hermes-wake
hermes plugins enable hermes-wake
```

Restart the gateway (or start a new session with `/new`); the `wake_me_when` and
`wake_list` tools appear in the next session.

## Why not a skill or the built-in `notify_on_complete`?

The built-in `notify_on_complete` only works for processes the agent started with its
own terminal tool inside the gateway process. Work launched through `systemd-run`,
`nohup`/`setsid`, a cron job, or on another host is detached from that tool, so nothing
notices when it ends. A skill cannot help either: a skill is instructions the agent reads
during a turn, and it cannot start a new turn in a chat on its own. Waking a session
needs code that keeps running between turns, which is what this plugin adds.

## How it works

1. `wake_me_when` reads the current session's origin (platform, chat id, thread id,
   chat type, user id) through `gateway.session_context.get_session_env`.
2. It parks a **blocked, unassigned** card on the kanban board `hermes-wake`, so the
   dispatcher never runs it. It then subscribes the origin with
   `hermes kanban notify-subscribe <id> --delivery-mode wake`.
3. The watch is saved in SQLite under `<HERMES_HOME>/plugin-data/hermes-wake/`.
   A daemon thread in every Hermes process that loads the plugin checks pending
   watches every 15 s. Because the gateway loads the plugin at boot, watches
   survive a gateway restart.
4. When the unit becomes inactive or failed (or is unloaded), the pid exits, the path
   is created (or changed, if it already existed at registration) and its size has
   held still for two polls, or
   `max_wait_hours` runs out, the watcher atomically claims the watch and runs
   `hermes kanban complete <id>`. The gateway's built-in kanban notifier then wakes
   the subscribed session.

Completion happens at most once. The claim is a compare-and-set in SQLite, and a
failed `complete` is released and retried on the next tick.

If `systemctl` fails, times out, or prints nothing usable, the unit state counts as
unknown and the watch keeps waiting (a warning is logged once per unit). Only a
successful `systemctl show` reporting `LoadState=not-found` or `ActiveState=inactive|failed`
ends a unit watch.

Tools: `wake_me_when`, and `wake_list` (list or cancel pending watches). `wake_list`
only shows and cancels watches registered from the current chat (same platform,
chat id and thread id). Cancelling unsubscribes the chat from the parked card and
archives it, so no orphan subscription is left behind.
Hook: `transform_terminal_output`. When a terminal command contains `systemd-run`,
`nohup` or `setsid`, or ends with `&`, the hook adds a one-line hint after the
output that suggests `wake_me_when`. The output itself is never changed. Turn it
off with `terminal_hint: false`.

## Requirements and limits

- The wake is delivered by a running messaging gateway with `kanban.notify_in_gateway`
  on (the default). In CLI or TUI sessions there is no chat to wake, so the tool
  returns an error.
- `delegate_task` subagents cannot change the kanban board, so they cannot register
  wakes. The tool says so; the parent agent should make the call.
- The watcher runs only while some Hermes process with the plugin loaded is alive,
  normally the gateway. If no such process is running, the completion is late, not
  lost: it fires on the next start.
- In one rare case a wake can fire twice. That needs a process to die after
  `kanban complete` succeeds but before it records the result, and then a 5-minute
  lease expires.

## Settings (`plugins.entries.hermes-wake.settings`)

| key | default | meaning |
|---|---|---|
| `board` | `hermes-wake` | kanban board for parked cards |
| `hermes_bin` | `""` | hermes executable; default `hermes` on PATH |
| `terminal_hint` | `true` | add the hint after detached terminal commands |

## Disclosure

- **Shell commands.** The plugin runs `hermes kanban ...` (board create, create,
  notify-subscribe, notify-unsubscribe, complete, archive) and `systemctl [--user] show <unit>`. It runs no
  other commands.
- **Reads outside its own data.** It reads `/proc/<pid>/stat` for pid watches and
  stats the given path (existence, mtime, size) for path watches.
- **Background process.** A daemon thread polls every 15 s in each Hermes process
  that loads the plugin.
- No network calls, no credentials, no telemetry, and it never updates itself.

## Live evidence

On 2026-11-11 the plugin was tested end to end in a real Telegram forum topic:

- A transient unit `wake-e2e.service` running `sleep 60` was started with `systemd-run`.
- The agent called `wake_me_when(unit="wake-e2e.service", ...)` at 11:20:06 UTC.
- At 11:21:03 UTC the gateway log showed
  `kanban notifier: woke agent for t_7c9537b8 on telegram`.
- At 11:21:07 UTC the agent replied in the same topic without any human prompt.

Six further real wakes were delivered the same day on Matrix.

## Tests

```
python -m pytest -q tests
hermes plugins validate . --install-deps
```

## Author

[BlueBirdBack](https://github.com/BlueBirdBack) - https://github.com/getaskclaw/hermes-wake

## License

MIT
