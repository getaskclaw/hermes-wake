"""hermes-wake: wake the originating chat session when out-of-band background work ends.

Public surfaces used: ctx.register_tool, ctx.register_hook("transform_terminal_output"),
ctx.get_config, gateway.session_context.get_session_env, plugins.plugin_storage.plugin_data_dir,
and the documented ``hermes kanban`` CLI (notify-subscribe --delivery-mode wake / complete).
"""
from __future__ import annotations

import json
import logging
import os
import re

try:
    from . import core
except ImportError:  # imported as a bare file (pytest collecting the repo root); see conftest.py
    from hermes_wake import core

logger = logging.getLogger(__name__)
PLUGIN = "hermes-wake"

try:  # public seam (developer-guide/plugins/public-api.md)
    from gateway.session_context import get_session_env as _getenv
except Exception:  # pragma: no cover - outside a Hermes install
    def _getenv(name, default=""):
        return os.environ.get(name, default)


def _data_dir():
    try:
        from plugins.plugin_storage import plugin_data_dir
        return plugin_data_dir(PLUGIN)
    except Exception:  # pragma: no cover
        from pathlib import Path
        p = Path(os.path.expanduser("~/.hermes/plugin-data")) / PLUGIN
        p.mkdir(parents=True, exist_ok=True)
        return p


_settings = {"board": core.BOARD, "hermes_bin": "", "terminal_hint": True}


def _store():
    return core.Store(_data_dir() / "watches.db")


def _kanban():
    return core.KanbanCLI(board=_settings["board"], hermes=core.hermes_cmd(_settings["hermes_bin"]))


_watcher = core.Watcher(_store, _kanban, log=logger.warning)

WAKE_ME_WHEN = {
    "name": "wake_me_when",
    "description": (
        "Ask to be woken in THIS conversation when background work you started outside the chat "
        "ends (a systemd-run unit, a nohup/setsid process, or a result file appearing). Call it "
        "right after launching the work, instead of promising to 'check back later'. Exactly one "
        "of unit, pid or path is required. When the work ends you get a new turn in this chat "
        "carrying `note`; verify the results then and report. Only works in a messaging-gateway "
        "session and from the top-level agent (not from delegate_task subagents)."),
    "parameters": {
        "type": "object",
        "properties": {
            "unit": {"type": "string", "description": "systemd unit name (user or system), e.g. job-42.service"},
            "pid": {"type": "integer", "description": "process id to wait for"},
            "path": {"type": "string", "description": "absolute path; wake when it is created, or changed if it already exists, and its size has settled"},
            "note": {"type": "string", "description": "what to do when woken, e.g. 'read /tmp/out.md and summarise'"},
            "max_wait_hours": {"type": "number", "description": "give up and wake anyway after this long (default 24)"},
        },
    },
}

WAKE_LIST = {
    "name": "wake_list",
    "description": ("List hermes-wake watches registered from THIS chat that are still pending "
                    "(optionally cancel one by id; cancelling also archives its kanban card)."),
    "parameters": {
        "type": "object",
        "properties": {"cancel": {"type": "string", "description": "watch id to cancel"}},
    },
}


def _err(msg, **kw):
    return json.dumps({"ok": False, "error": msg, **kw})


def wake_me_when(args, **_kw):
    args = args or {}
    given = [(k, args.get(k)) for k in ("unit", "pid", "path") if args.get(k) not in (None, "")]
    if len(given) != 1:
        return _err("pass exactly one of unit, pid, path")
    kind, target = given[0][0], str(given[0][1]).strip()
    origin = core.capture_origin(_getenv)
    if not core.origin_is_wakeable(origin):
        return _err("no messaging-gateway origin in this session (HERMES_SESSION_PLATFORM/CHAT_ID); "
                    "there is no chat to wake. Check on the work yourself instead.")
    try:
        hours = float(args.get("max_wait_hours") or 24)
    except (TypeError, ValueError):
        return _err("max_wait_hours must be a number")
    try:
        res = core.register_watch(_store(), _kanban(), kind, target, str(args.get("note") or "")[:500],
                                  origin, max(60.0, hours * 3600))
    except ValueError as exc:
        return _err(str(exc))
    except core.SubagentError:
        return _err("subagents (delegate_task children) cannot create kanban cards, so they cannot "
                    "register wakes. Return the unit/pid/path to your parent and let the top-level "
                    "agent call wake_me_when.")
    except core.KanbanError as exc:
        return _err(f"kanban registration failed: {exc}")
    _watcher.start()
    return json.dumps({"ok": True, **res, "watching": f"{kind} {target}",
                       "message": "You will be woken in this conversation when it ends. "
                                  "No need to poll or promise a check-back time."})


def _same_chat(a: dict, b: dict) -> bool:
    return all((a.get(k) or "") == (b.get(k) or "") for k in ("platform", "chat_id", "thread_id"))


def wake_list(args, **_kw):
    """List or cancel watches, scoped to the caller's chat (platform + chat_id + thread_id)."""
    args = args or {}
    origin = core.capture_origin(_getenv)
    if not core.origin_is_wakeable(origin):
        return _err("no messaging-gateway origin in this session; wake_list only shows watches "
                    "registered from the current chat")
    store = _store()
    mine = [w for w in store.list() if _same_chat(w["origin"], origin)]
    if args.get("cancel"):
        wid = str(args["cancel"])
        w = next((w for w in mine if w["id"] == wid), None)
        if w is None or not store.cancel(wid):
            return _err(f"no pending watch {wid} in this chat")
        cleanup = []
        if w.get("task_id"):
            kb = _kanban()
            for step, fn in (("notify-unsubscribe", lambda: kb.unsubscribe(w["task_id"], w["origin"])),
                             ("archive", lambda: kb.archive(w["task_id"]))):
                try:
                    fn()
                    cleanup.append(step)
                except Exception as exc:
                    logger.warning("hermes-wake: %s %s failed: %s", step, w["task_id"], exc)
                    cleanup.append(f"{step} failed: {exc}")
        return json.dumps({"ok": True, "cancelled": wid, "kanban_task": w.get("task_id"),
                           "kanban_cleanup": cleanup})
    rows = [{k: w.get(k) for k in ("id", "kind", "target", "note", "state", "task_id", "last_error")}
            for w in mine]
    return json.dumps({"ok": True, "watches": rows})


_BG = re.compile(r"(^|[\s;&|(])(systemd-run|nohup|setsid)\s|&\s*(disown\s*)?$")


def terminal_hint(command="", output="", returncode=None, **_kw):
    """Append a one-line nudge when the agent launches detached work. Never changes the output."""
    if not _settings["terminal_hint"] or returncode is None or not command:
        return None
    if "wake_me_when" in output or not _BG.search(command.strip()):
        return None
    return (output.rstrip("\n") + "\n\n[hermes-wake] This looks like detached background work. "
            "If you will report on it later, call wake_me_when(unit=... | pid=... | path=..., "
            "note=...) now so this chat is woken when it ends.")


def register(ctx):
    for key in ("board", "hermes_bin", "terminal_hint"):
        try:
            val = ctx.get_config(key, default=_settings[key])
        except Exception:
            val = _settings[key]
        if val is not None:
            _settings[key] = val
    ctx.register_tool(name="wake_me_when", toolset="hermes_wake", schema=WAKE_ME_WHEN,
                      handler=wake_me_when, description=WAKE_ME_WHEN["description"], emoji="⏰")
    ctx.register_tool(name="wake_list", toolset="hermes_wake", schema=WAKE_LIST,
                      handler=wake_list, description=WAKE_LIST["description"], emoji="⏰")
    ctx.register_hook("transform_terminal_output", terminal_hint)
    # Resume persisted watches (survives gateway restarts). Cheap: one SQLite select per tick.
    if os.environ.get("HERMES_WAKE_NO_WATCHER") != "1":
        _watcher.start()
