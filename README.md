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
4. When the unit becomes inactive or failed, the pid exits, the path appears, or
   `max_wait_hours` runs out, the watcher atomically claims the watch and runs
   `hermes kanban complete <id>`. The gateway's built-in kanban notifier then wakes
   the subscribed session.

Completion happens at most once. The claim is a compare-and-set in SQLite, and a
failed `complete` is released and retried on the next tick.

Tools: `wake_me_when`, and `wake_list` (list or cancel pending watches).
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
  notify-subscribe, complete) and `systemctl [--user] show <unit>`. It runs no
  other commands.
- **Reads outside its own data.** It reads `/proc/<pid>/stat` for pid watches and
  checks that the given path exists for path watches.
- **Background process.** A daemon thread polls every 15 s in each Hermes process
  that loads the plugin.
- No network calls, no credentials, no telemetry, and it never updates itself.

## Tests

```
python -m pytest -q tests
hermes plugins validate . --install-deps
```

## License

MIT
