"""hermes-wake core: persistent watch store, completion conditions, kanban CLI bridge, watcher.

No Hermes imports here, so the module is unit-testable on its own. Kanban is driven only
through the documented ``hermes kanban`` CLI (user-guide/features/kanban.md).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

BOARD = "hermes-wake"
POLL_S = 15.0
LEASE_S = 300.0          # a claim older than this is considered abandoned (process died)
DEFAULT_MAX_WAIT_S = 24 * 3600

ORIGIN_ENV = {
    "platform": "HERMES_SESSION_PLATFORM",
    "chat_id": "HERMES_SESSION_CHAT_ID",
    "thread_id": "HERMES_SESSION_THREAD_ID",
    "chat_type": "HERMES_SESSION_CHAT_TYPE",
    "user_id": "HERMES_SESSION_USER_ID",
    "user_id_alt": "HERMES_SESSION_USER_ID_ALT",
    "parent_chat_id": "HERMES_SESSION_PARENT_CHAT_ID",
    "scope_id": "HERMES_SESSION_SCOPE_ID",
    "profile": "HERMES_SESSION_PROFILE",
    "session_id": "HERMES_SESSION_ID",
    "session_key": "HERMES_SESSION_KEY",
}
NON_MESSAGING = {"", "api_server", "cli", "codex", "desktop", "gateway", "kanban", "local",
                 "tool", "tui", "webhook"}
CHAT_TYPES = ("dm", "group", "channel", "thread")
CHILD_DENIED = "delegate_task child"
logger = logging.getLogger("hermes_wake")
_warned_units: set = set()


class KanbanError(RuntimeError):
    pass


class SubagentError(KanbanError):
    pass


# --------------------------------------------------------------------------- origin
def capture_origin(getenv: Callable[[str, str], str]) -> dict:
    o = {k: (getenv(v, "") or "").strip() for k, v in ORIGIN_ENV.items()}
    return {k: v for k, v in o.items() if v}


def origin_is_wakeable(origin: dict) -> bool:
    return bool(origin.get("chat_id")) and origin.get("platform", "").lower() not in NON_MESSAGING


# --------------------------------------------------------------------------- kanban CLI
def hermes_cmd(configured: str = "") -> list:
    if configured:
        return [configured]
    found = shutil.which("hermes")
    return [found] if found else [sys.executable, "-m", "hermes_cli.main"]


def child_env() -> dict:
    """Env for our own ``hermes`` subprocesses: never start a nested watcher there."""
    return {**os.environ, "HERMES_WAKE_NO_WATCHER": "1"}


class KanbanCLI:
    """Thin wrapper over the public ``hermes kanban`` CLI. ``runner`` is injectable for tests."""

    def __init__(self, board: str = BOARD, hermes: Optional[list] = None, runner=subprocess.run):
        self.board = board
        self.hermes = hermes or hermes_cmd()
        self.runner = runner

    def _run(self, args: list, board: bool = True) -> str:
        cmd = [*self.hermes, "kanban", *(["--board", self.board] if board else []), *args]
        r = self.runner(cmd, capture_output=True, text=True, timeout=60, env=child_env())
        out = ((r.stderr or "") + (r.stdout or "")).strip()
        if r.returncode != 0:
            if CHILD_DENIED in out:
                raise SubagentError(out)
            raise KanbanError(f"hermes kanban {args[0]} failed rc={r.returncode}: {out[:300]}")
        return r.stdout or ""

    def ensure_board(self) -> None:
        cmd = [*self.hermes, "kanban", "boards", "create", self.board, "--name", "Hermes wake"]
        r = self.runner(cmd, capture_output=True, text=True, timeout=60, env=child_env())
        out = ((r.stderr or "") + (r.stdout or ""))
        if r.returncode != 0 and CHILD_DENIED in out:
            raise SubagentError(out.strip())
        if r.returncode != 0 and "exist" not in out.lower():
            raise KanbanError(f"board create failed: {out.strip()[:300]}")

    def create_parked(self, watch_id: str, title: str) -> str:
        out = self._run(["create", title[:120], "--initial-status", "blocked",
                         "--created-by", "hermes-wake", "--idempotency-key", f"hermes-wake:{watch_id}",
                         "--json", "--body",
                         "Parked wake card owned by the hermes-wake plugin. Never dispatched; the "
                         "plugin completes it when the watched work ends, which wakes the origin session."])
        try:
            return str(json.loads(out)["id"])
        except (ValueError, KeyError, TypeError) as exc:
            raise KanbanError(f"unexpected create output: {out[:200]}") from exc

    def subscribe_wake(self, task_id: str, origin: dict) -> None:
        self._run(subscribe_args(task_id, origin))

    def unsubscribe(self, task_id: str, origin: dict) -> None:
        a = ["notify-unsubscribe", task_id, "--platform", origin["platform"], "--chat-id", origin["chat_id"]]
        if origin.get("thread_id"):
            a += ["--thread-id", origin["thread_id"]]
        self._run(a)

    def archive(self, task_id: str) -> None:
        self._run(["archive", task_id])

    def complete(self, task_id: str, summary: str, metadata: dict) -> None:
        self._run(["complete", task_id, "--force", "--summary", summary[:1500],
                   "--metadata", json.dumps(metadata)])


def subscribe_args(task_id: str, origin: dict) -> list:
    a = ["notify-subscribe", task_id, "--platform", origin["platform"], "--chat-id", origin["chat_id"],
         "--delivery-mode", "wake"]
    for key, flag in (("thread_id", "--thread-id"), ("user_id", "--user-id"),
                      ("user_id_alt", "--user-id-alt"), ("chat_type", "--chat-type"),
                      ("parent_chat_id", "--parent-chat-id"), ("scope_id", "--guild-id"),
                      ("profile", "--notifier-profile")):
        v = origin.get(key)
        if not v or (key == "chat_type" and v not in CHAT_TYPES):
            continue
        a += [flag, v]
    return a


# --------------------------------------------------------------------------- conditions
def proc_start_time(pid: int) -> Optional[str]:
    """Field 22 of /proc/<pid>/stat; guards against PID reuse. None if the pid is gone."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return raw.rsplit(")", 1)[1].split()[19]


def _warn_once(unit: str, msg: str) -> None:
    if unit not in _warned_units:
        _warned_units.add(unit)
        logger.warning("hermes-wake: %s", msg)


def unit_state(unit: str, runner=subprocess.run, scope: str = "") -> Optional[dict]:
    """ActiveState/LoadState/Result/ExecMainStatus, trying the user manager then the system one.

    Returns None ("unknown, keep watching") unless some systemctl call succeeded (rc 0) and
    printed a LoadState. A failed, empty or timed-out query never means "finished".
    ``scope`` ("user"/"system") pins the query to the manager that owned the unit at
    registration, so a hiccup in that manager can't be masked by "not-found" from the other.
    """
    answered = None
    errors = []
    scopes = {"user": [["--user"]], "system": [[]]}.get(scope, [["--user"], []])
    for scope in scopes:
        try:
            r = runner(["systemctl", *scope, "show", unit, "-p",
                        "LoadState,ActiveState,SubState,Result,ExecMainStatus"],
                       capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{'user' if scope else 'system'}: {exc}")
            continue
        if getattr(r, "returncode", 1) != 0:
            errors.append(f"{'user' if scope else 'system'}: rc={r.returncode}")
            continue
        props = dict(line.split("=", 1) for line in (r.stdout or "").splitlines() if "=" in line)
        if not props.get("LoadState"):
            errors.append(f"{'user' if scope else 'system'}: no LoadState in output")
            continue
        props["scope"] = "user" if scope else "system"
        if props["LoadState"] == "loaded":
            return props
        answered = answered or props
    if answered is None:
        _warn_once(unit, f"systemctl show {unit} gave no usable answer ({'; '.join(errors)}); "
                         "treating the unit as still running")
    return answered


def _unit_outcome(target: str, s: Optional[dict]) -> Optional[str]:
    if s is None:
        return None
    if s.get("LoadState") == "not-found":
        return f"unit {target} finished and was unloaded"
    if s.get("LoadState") == "loaded" and s.get("ActiveState") in ("inactive", "failed"):
        return (f"unit {target} {s.get('ActiveState')} (result={s.get('Result', '?')}, "
                f"exit={s.get('ExecMainStatus', '?')})")
    return None


def path_stat(target: str) -> Optional[list]:
    try:
        st = os.stat(target)
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size]


def check(watch: dict, runner=subprocess.run, now: Optional[float] = None) -> Optional[str]:
    """Return a human outcome string when the watched work is over, else None.

    Path watches may update ``watch["path_seen"]`` (persisted by ``tick``): a path fires only
    when it is new or changed since registration and its mtime+size held still for two ticks.
    """
    now = time.time() if now is None else now
    kind, target = watch["kind"], watch["target"]
    if kind == "pid":
        st = proc_start_time(int(target))
        if st is None or st != watch.get("pid_start"):
            return f"process {target} exited"
    elif kind == "path":
        cur = path_stat(target)
        baseline = watch.get("path_initial")  # [mtime_ns, size] at registration, or None
        if cur is not None and cur != baseline:
            if watch.get("path_seen") == cur:
                return f"{target} {'changed' if baseline else 'appeared'} ({cur[1]} bytes)"
            watch["path_seen"] = cur
        else:
            watch.pop("path_seen", None)
    elif kind == "unit":
        out = _unit_outcome(target, unit_state(target, runner, watch.get("unit_scope", "")))
        if out:
            return out
    if now - watch["created_at"] > watch["max_wait_s"]:
        return f"gave up waiting after {int(watch['max_wait_s'])}s ({kind} {target} still running)"
    return None


def validate_target(kind: str, target: str, runner=subprocess.run) -> dict:
    """Pre-flight; returns extra fields to persist, raises ValueError on a bad target."""
    if kind == "pid":
        if not re.fullmatch(r"\d+", target):
            raise ValueError("pid must be an integer")
        st = proc_start_time(int(target))
        if st is None:
            raise ValueError(f"pid {target} is not running")
        return {"pid_start": st}
    if kind == "unit":
        if not re.fullmatch(r"[\w@.:\-\\]+", target):
            raise ValueError("invalid unit name")
        s = unit_state(target, runner)
        if s is None:
            raise ValueError(f"could not query systemd for {target} (systemctl failed or timed out)")
        if s.get("LoadState") != "loaded":
            raise ValueError(f"unit {target} is not loaded (already finished, or wrong name?)")
        return {"unit_scope": s.get("scope", "")}
    if kind == "path":
        if not os.path.isabs(target):
            raise ValueError("path must be absolute")
        return {"path_initial": path_stat(target)}
    raise ValueError(f"unknown kind {kind}")


# --------------------------------------------------------------------------- store
class Store:
    """SQLite watch table. State machine: pending -> claimed -> done (claim is atomic)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS watches (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL, note TEXT,
                extra TEXT NOT NULL DEFAULT '{}', origin TEXT NOT NULL, task_id TEXT,
                state TEXT NOT NULL DEFAULT 'pending', claimed_at REAL, created_at REAL NOT NULL,
                max_wait_s REAL NOT NULL, outcome TEXT, last_error TEXT, attempts INTEGER DEFAULT 0)""")

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def add(self, kind, target, note, extra, origin, max_wait_s, task_id=None, now=None) -> str:
        wid = uuid.uuid4().hex[:12]
        with self._conn() as c:
            c.execute("INSERT INTO watches (id,kind,target,note,extra,origin,task_id,created_at,max_wait_s)"
                      " VALUES (?,?,?,?,?,?,?,?,?)",
                      (wid, kind, target, note, json.dumps(extra), json.dumps(origin), task_id,
                       time.time() if now is None else now, float(max_wait_s)))
        return wid

    def set_task(self, wid, task_id):
        with self._conn() as c:
            c.execute("UPDATE watches SET task_id=? WHERE id=?", (task_id, wid))

    def set_extra(self, wid, updates: dict):
        with self._conn() as c:
            r = c.execute("SELECT extra FROM watches WHERE id=?", (wid,)).fetchone()
            if r is None:
                return
            extra = json.loads(r["extra"] or "{}")
            extra.update(updates)
            c.execute("UPDATE watches SET extra=? WHERE id=?", (json.dumps(extra), wid))

    def delete(self, wid):
        with self._conn() as c:
            c.execute("DELETE FROM watches WHERE id=?", (wid,))

    def get(self, wid) -> Optional[dict]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM watches WHERE id=?", (wid,)).fetchone()
        return _row(r) if r else None

    def active(self, now=None) -> list:
        """Pending watches plus claims whose lease expired (their process died mid-complete)."""
        now = time.time() if now is None else now
        with self._conn() as c:
            rows = c.execute("SELECT * FROM watches WHERE task_id IS NOT NULL AND (state='pending' OR "
                             "(state='claimed' AND claimed_at < ?))", (now - LEASE_S,)).fetchall()
        return [_row(r) for r in rows]

    def list(self, include_done=False) -> list:
        q = "SELECT * FROM watches" + ("" if include_done else " WHERE state!='done'")
        with self._conn() as c:
            return [_row(r) for r in c.execute(q + " ORDER BY created_at").fetchall()]

    def claim(self, wid, prev_state, prev_claimed_at, now=None) -> bool:
        now = time.time() if now is None else now
        with self._conn() as c:
            cur = c.execute("UPDATE watches SET state='claimed', claimed_at=? WHERE id=? AND state=? "
                            "AND claimed_at IS ?", (now, wid, prev_state, prev_claimed_at))
            return cur.rowcount == 1

    def finish(self, wid, outcome):
        with self._conn() as c:
            c.execute("UPDATE watches SET state='done', outcome=? WHERE id=?", (outcome, wid))

    def release(self, wid, error):
        with self._conn() as c:
            c.execute("UPDATE watches SET state='pending', claimed_at=NULL, last_error=?, "
                      "attempts=attempts+1 WHERE id=?", (error[:500], wid))

    def cancel(self, wid) -> bool:
        with self._conn() as c:
            return c.execute("UPDATE watches SET state='done', outcome='cancelled' WHERE id=? AND "
                             "state='pending'", (wid,)).rowcount == 1


def _row(r) -> dict:
    d = dict(r)
    d["origin"] = json.loads(d["origin"])
    d.update(json.loads(d.pop("extra") or "{}"))
    return d


# --------------------------------------------------------------------------- registration + ticks
def register_watch(store: Store, kanban: KanbanCLI, kind: str, target: str, note: str,
                   origin: dict, max_wait_s: float, runner=subprocess.run) -> dict:
    extra = validate_target(kind, target, runner)
    wid = store.add(kind, target, note, extra, origin, max_wait_s)
    try:
        kanban.ensure_board()
        tid = kanban.create_parked(wid, f"wake: {kind} {target}" + (f" - {note}" if note else ""))
        kanban.subscribe_wake(tid, origin)
    except Exception:
        store.delete(wid)
        raise
    store.set_task(wid, tid)
    return {"watch_id": wid, "kanban_task": tid, "board": kanban.board}


def completion_summary(w: dict, outcome: str) -> str:
    return (f"[hermes-wake] {outcome}\n"
            + (f"note: {w['note']}\n" if w.get("note") else "")
            + f"watch: {w['kind']} {w['target']} (id {w['id']})\n"
            "The background work you asked to be woken for has ended. Check its results and "
            "report back in this conversation.")


def tick(store: Store, kanban: KanbanCLI, runner=subprocess.run, now=None, log=None) -> list:
    """One pass. Returns the watch ids completed in this pass. Safe to run in many processes."""
    done = []
    for w in store.active(now):
        seen = w.get("path_seen")
        outcome = check(w, runner, now)
        if w.get("path_seen") != seen:
            store.set_extra(w["id"], {"path_seen": w.get("path_seen")})
        if outcome is None:
            continue
        if not store.claim(w["id"], w["state"], w["claimed_at"], now):
            continue          # another process got it first
        try:
            kanban.complete(w["task_id"], completion_summary(w, outcome),
                            {"hermes_wake": w["id"], "kind": w["kind"], "target": w["target"]})
        except Exception as exc:  # retried on the next tick
            store.release(w["id"], str(exc))
            if log:
                log(f"hermes-wake: complete {w['task_id']} failed: {exc}")
            continue
        store.finish(w["id"], outcome)
        done.append(w["id"])
    return done


class Watcher:
    """Daemon thread that runs ``tick`` every POLL_S seconds in whatever process loaded the plugin."""

    def __init__(self, store_factory, kanban_factory, interval=POLL_S, log=None):
        self.store_factory, self.kanban_factory = store_factory, kanban_factory
        self.interval, self.log = interval, log
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, name="hermes-wake-watcher", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(self.interval):
            try:
                tick(self.store_factory(), self.kanban_factory(), log=self.log)
            except Exception as exc:  # never kill the thread
                if self.log:
                    self.log(f"hermes-wake: tick failed: {exc}")
