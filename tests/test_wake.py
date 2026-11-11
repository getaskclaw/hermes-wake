import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import sys as _s
hw = _s.modules["hermes_wake"]

core = hw.core

ORIGIN = {"platform": "telegram", "chat_id": "-100123", "thread_id": "77", "chat_type": "group",
          "user_id": "42", "session_key": "agent:main:telegram:group:-100123:77"}


class FakeRunner:
    """Records hermes kanban calls; answers like the real CLI."""

    def __init__(self, fail_complete=0, child=False):
        self.calls, self.fail_complete, self.child, self.n = [], fail_complete, child, 0

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        assert kw.get("env", {}).get("HERMES_WAKE_NO_WATCHER") == "1"
        if self.child:
            return SimpleNamespace(returncode=1, stdout="",
                                   stderr="kanban: delegate_task child contexts cannot mutate Kanban tasks via the CLI")
        if "create" in cmd and "boards" not in cmd:
            self.n += 1
            return SimpleNamespace(returncode=0, stdout=json.dumps({"id": f"t_{self.n}"}), stderr="")
        if "complete" in cmd and self.fail_complete:
            self.fail_complete -= 1
            return SimpleNamespace(returncode=1, stdout="", stderr="db locked")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def actions(self):
        return [c[c.index("kanban") + (3 if "--board" in c else 1)] for c in self.calls]


@pytest.fixture
def env(tmp_path):
    store = core.Store(tmp_path / "w.db")
    runner = FakeRunner()
    kb = core.KanbanCLI(board="hermes-wake", hermes=["hermes"], runner=runner)
    return store, kb, runner, tmp_path


def test_subscribe_args_carry_full_origin():
    a = core.subscribe_args("t_1", ORIGIN)
    assert a[:6] == ["notify-subscribe", "t_1", "--platform", "telegram", "--chat-id", "-100123"]
    assert a[a.index("--delivery-mode") + 1] == "wake"
    assert a[a.index("--thread-id") + 1] == "77"
    assert a[a.index("--chat-type") + 1] == "group"
    assert "--chat-type" not in core.subscribe_args("t", {**ORIGIN, "chat_type": "weird"})


def test_origin_capture_and_wakeable():
    envmap = {"HERMES_SESSION_PLATFORM": "telegram", "HERMES_SESSION_CHAT_ID": " 5 "}
    o = core.capture_origin(lambda n, d="": envmap.get(n, d))
    assert o == {"platform": "telegram", "chat_id": "5"}
    assert core.origin_is_wakeable(o)
    assert not core.origin_is_wakeable({"platform": "cli", "chat_id": "x"})
    assert not core.origin_is_wakeable({"platform": "telegram"})


def test_path_watch_full_cycle_is_idempotent(env):
    store, kb, runner, tmp = env
    target = tmp / "out.md"
    res = core.register_watch(store, kb, "path", str(target), "summarise", ORIGIN, 3600)
    assert res["kanban_task"] == "t_1"
    assert runner.actions() == ["boards", "create", "notify-subscribe"]
    create = runner.calls[1]
    assert create[create.index("--initial-status") + 1] == "blocked"
    assert "--assignee" not in create
    assert core.tick(store, kb) == []
    target.write_text("done")
    assert core.tick(store, kb) == []          # first sighting: wait for size to settle
    assert core.tick(store, kb) == [res["watch_id"]]
    comp = runner.calls[-1]
    assert comp[comp.index("kanban") + 3:comp.index("kanban") + 5] == ["complete", "t_1"]
    assert "summarise" in comp[comp.index("--summary") + 1]
    # second tick and a "restarted process" with a fresh Store object: no duplicate
    assert core.tick(store, kb) == []
    assert core.tick(core.Store(tmp / "w.db"), kb) == []
    assert runner.actions().count("complete") == 1


def test_concurrent_claim_only_one_wins(env):
    store, kb, runner, tmp = env
    (tmp / "f").write_text("x")
    core.register_watch(store, kb, "path", str(tmp / "f"), "", ORIGIN, 3600)
    w = store.active()[0]
    assert store.claim(w["id"], "pending", None)
    assert not store.claim(w["id"], "pending", None)


def test_failed_complete_is_retried(env):
    store, kb, runner, tmp = env
    runner.fail_complete = 1
    core.register_watch(store, kb, "path", str(tmp / "f"), "", ORIGIN, 3600)
    (tmp / "f").write_text("x")
    assert core.tick(store, kb) == []
    assert core.tick(store, kb) == []
    assert store.list()[0]["last_error"]
    assert len(core.tick(store, kb)) == 1
    assert runner.actions().count("complete") == 2


def test_stale_claim_is_recovered(env):
    store, kb, runner, tmp = env
    core.register_watch(store, kb, "path", str(tmp / "f"), "", ORIGIN, 3600)
    (tmp / "f").write_text("x")
    assert core.tick(store, kb) == []
    w = store.active()[0]
    store.claim(w["id"], "pending", None, now=time.time() - core.LEASE_S - 10)
    assert len(core.tick(store, kb)) == 1


def test_pid_watch(env):
    store, kb, runner, tmp = env
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        core.register_watch(store, kb, "pid", str(p.pid), "", ORIGIN, 3600)
        assert core.tick(store, kb) == []
    finally:
        p.kill(); p.wait()
    assert len(core.tick(store, kb)) == 1


def test_pid_reuse_detected(env):
    store, kb, runner, tmp = env
    w = {"kind": "pid", "target": str(os.getpid()), "pid_start": "0", "created_at": time.time(),
         "max_wait_s": 3600}
    assert "exited" in core.check(w)


def test_unit_states():
    def mk(props):
        return lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="\n".join(f"{k}={v}" for k, v in props.items()))
    w = {"kind": "unit", "target": "x.service", "created_at": time.time(), "max_wait_s": 3600}
    assert core.check(w, mk({"LoadState": "loaded", "ActiveState": "active"})) is None
    assert "failed" in core.check(w, mk({"LoadState": "loaded", "ActiveState": "failed", "Result": "exit-code"}))
    assert "unloaded" in core.check(w, mk({"LoadState": "not-found", "ActiveState": "inactive"}))
    with pytest.raises(ValueError):
        core.validate_target("unit", "gone.service", mk({"LoadState": "not-found"}))


def test_timeout_wakes_anyway(tmp_path):
    w = {"kind": "path", "target": str(tmp_path / "never"), "created_at": time.time() - 100, "max_wait_s": 60}
    assert "gave up" in core.check(w)


def test_bad_targets(env):
    store, kb, runner, tmp = env
    for kind, t in (("pid", "abc"), ("path", "rel/path"), ("pid", "999999999")):
        with pytest.raises(ValueError):
            core.register_watch(store, kb, kind, t, "", ORIGIN, 60)
    assert store.list(include_done=True) == [] and runner.calls == []


def test_subagent_rejected_cleanly(env, monkeypatch, tmp_path):
    store, _, _, tmp = env
    kb = core.KanbanCLI(hermes=["hermes"], runner=FakeRunner(child=True))
    with pytest.raises(core.SubagentError):
        core.register_watch(store, kb, "path", str(tmp / "x"), "", ORIGIN, 60)
    assert store.list(include_done=True) == []
    # tool-level message
    monkeypatch.setattr(hw, "_kanban", lambda: kb)
    monkeypatch.setattr(hw, "_store", lambda: store)
    monkeypatch.setattr(hw, "_getenv", lambda n, d="": {"HERMES_SESSION_PLATFORM": "telegram",
                                                       "HERMES_SESSION_CHAT_ID": "1"}.get(n, d))
    out = json.loads(hw.wake_me_when({"path": str(tmp / "x")}))
    assert not out["ok"] and "top-level agent" in out["error"]


def test_tool_happy_path_and_guards(env, monkeypatch):
    store, kb, runner, tmp = env
    monkeypatch.setattr(hw, "_kanban", lambda: kb)
    monkeypatch.setattr(hw, "_store", lambda: store)
    monkeypatch.setattr(hw._watcher, "start", lambda: None)
    monkeypatch.setattr(hw, "_getenv", lambda n, d="": {})
    assert "no messaging-gateway origin" in json.loads(hw.wake_me_when({"path": "/x"}))["error"]
    envmap = {"HERMES_SESSION_PLATFORM": "telegram", "HERMES_SESSION_CHAT_ID": "-100", "HERMES_SESSION_THREAD_ID": "9"}
    monkeypatch.setattr(hw, "_getenv", lambda n, d="": envmap.get(n, d))
    assert "exactly one" in json.loads(hw.wake_me_when({"path": "/x", "pid": 1}))["error"]
    out = json.loads(hw.wake_me_when({"path": str(tmp / "r.md"), "note": "n"}))
    assert out["ok"] and out["kanban_task"] == "t_1"
    lst = json.loads(hw.wake_list({}))["watches"]
    assert len(lst) == 1 and lst[0]["note"] == "n"
    assert json.loads(hw.wake_list({"cancel": lst[0]["id"]}))["ok"]
    assert json.loads(hw.wake_list({}))["watches"] == []


@pytest.mark.parametrize("cmd,hint", [
    ("systemd-run --user --unit=job sleep 99", True),
    ("nohup ./long.sh > log 2>&1 &", True),
    ("./x.sh &", True),
    ("setsid foo", True),
    ("ls -la", False),
    ("echo nohup", False),
    ("a && b", False),
])
def test_terminal_hint(cmd, hint):
    out = hw.terminal_hint(command=cmd, output="ok\n", returncode=0)
    assert (out is not None) == hint
    if hint:
        assert out.startswith("ok") and "wake_me_when" in out


def test_terminal_hint_skips_background_polls():
    assert hw.terminal_hint(command="nohup x &", output="", returncode=None) is None


def test_register_uses_public_ctx(monkeypatch):
    monkeypatch.setenv("HERMES_WAKE_NO_WATCHER", "1")
    seen = {"tools": [], "hooks": []}
    ctx = SimpleNamespace(
        get_config=lambda k, default=None: default,
        register_tool=lambda **kw: seen["tools"].append(kw["name"]),
        register_hook=lambda name, fn: seen["hooks"].append(name))
    hw.register(ctx)
    assert seen == {"tools": ["wake_me_when", "wake_list"], "hooks": ["transform_terminal_output"]}


def test_watcher_thread_ticks(env, monkeypatch):
    store, kb, runner, tmp = env
    core.register_watch(store, kb, "path", str(tmp / "f"), "", ORIGIN, 3600)
    (tmp / "f").write_text("x")
    w = core.Watcher(lambda: store, lambda: kb, interval=0.05)
    w.start(); w.start()
    deadline = time.time() + 5
    while time.time() < deadline and "complete" not in runner.actions():
        time.sleep(0.05)
    w.stop()
    assert runner.actions().count("complete") == 1


# ---------------------------------------------------------------- hardening: unit unknown states
def _sysctl(rc=0, out="", exc=None):
    def run(cmd, **kw):
        if exc:
            raise exc
        return SimpleNamespace(returncode=rc, stdout=out, stderr="")
    return run


@pytest.mark.parametrize("runner", [
    _sysctl(exc=OSError("no systemctl")),
    _sysctl(exc=subprocess.TimeoutExpired("systemctl", 20)),
    _sysctl(rc=1, out=""),
    _sysctl(rc=0, out=""),
    _sysctl(rc=1, out="LoadState=not-found\n"),   # failed call: output not trusted
])
def test_unit_unknown_keeps_watching(runner):
    w = {"kind": "unit", "target": "x.service", "created_at": time.time(), "max_wait_s": 3600}
    assert core.unit_state("x.service", runner) is None
    assert core.check(w, runner) is None
    with pytest.raises(ValueError, match="could not query"):
        core.validate_target("unit", "x.service", runner)


def test_unit_unknown_logs_once(caplog):
    core._warned_units.discard("y.service")
    with caplog.at_level("WARNING", logger="hermes_wake"):
        for _ in range(3):
            core.unit_state("y.service", _sysctl(exc=OSError("boom")))
    assert sum("y.service" in r.message for r in caplog.records) == 1


def test_unit_user_fails_system_answers():
    calls = []
    def run(cmd, **kw):
        calls.append(cmd)
        if "--user" in cmd:
            return SimpleNamespace(returncode=1, stdout="", stderr="no bus")
        return SimpleNamespace(returncode=0, stdout="LoadState=loaded\nActiveState=inactive\nResult=success\n")
    w = {"kind": "unit", "target": "x.service", "created_at": time.time(), "max_wait_s": 3600}
    assert "inactive" in core.check(w, run)


def test_unit_unknown_tick_does_not_complete(env):
    store, kb, runner, tmp = env
    ok = _sysctl(out="LoadState=loaded\nActiveState=active\n")
    core.register_watch(store, kb, "unit", "x.service", "", ORIGIN, 3600, runner=ok)
    assert core.tick(store, kb, runner=_sysctl(exc=subprocess.TimeoutExpired("s", 20))) == []
    assert "complete" not in runner.actions()


# ---------------------------------------------------------------- hardening: path freshness
def test_stale_preexisting_path_does_not_fire(env):
    store, kb, runner, tmp = env
    f = tmp / "old.md"
    f.write_text("stale report")
    core.register_watch(store, kb, "path", str(f), "", ORIGIN, 3600)
    for _ in range(3):
        assert core.tick(store, kb) == []
    st = os.stat(f)
    f.write_text("fresh report!")
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert core.tick(store, kb) == []
    assert len(core.tick(store, kb)) == 1


def test_path_waits_for_size_to_settle(env):
    store, kb, runner, tmp = env
    f = tmp / "grow.log"
    core.register_watch(store, kb, "path", str(f), "", ORIGIN, 3600)
    f.write_text("a")
    assert core.tick(store, kb) == []
    f.write_text("abc")                     # still being written
    assert core.tick(store, kb) == []
    assert len(core.tick(store, kb)) == 1   # unchanged across two ticks
    assert "appeared (3 bytes)" in store.list(include_done=True)[0]["outcome"]


def test_path_removed_resets(tmp_path):
    f = tmp_path / "f"
    w = {"kind": "path", "target": str(f), "path_initial": None, "created_at": time.time(), "max_wait_s": 3600}
    f.write_text("x")
    assert core.check(w) is None and w["path_seen"]
    f.unlink()
    assert core.check(w) is None and "path_seen" not in w


# ---------------------------------------------------------------- hardening: wake_list scoping
def _as_chat(monkeypatch, chat, thread=""):
    m = {"HERMES_SESSION_PLATFORM": "telegram", "HERMES_SESSION_CHAT_ID": chat, "HERMES_SESSION_THREAD_ID": thread}
    monkeypatch.setattr(hw, "_getenv", lambda n, d="": m.get(n, d))


def test_wake_list_scoped_and_cancel_archives(env, monkeypatch):
    store, kb, runner, tmp = env
    monkeypatch.setattr(hw, "_kanban", lambda: kb)
    monkeypatch.setattr(hw, "_store", lambda: store)
    monkeypatch.setattr(hw._watcher, "start", lambda: None)
    _as_chat(monkeypatch, "-100", "9")
    a = json.loads(hw.wake_me_when({"path": str(tmp / "a")}))
    _as_chat(monkeypatch, "-100", "10")       # other topic, same group
    b = json.loads(hw.wake_me_when({"path": str(tmp / "b")}))
    _as_chat(monkeypatch, "-200")
    assert json.loads(hw.wake_list({}))["watches"] == []
    out = json.loads(hw.wake_list({"cancel": a["watch_id"]}))
    assert not out["ok"]                         # cannot cancel another chat's watch
    _as_chat(monkeypatch, "-100", "10")
    assert [w["id"] for w in json.loads(hw.wake_list({}))["watches"]] == [b["watch_id"]]
    out = json.loads(hw.wake_list({"cancel": b["watch_id"]}))
    assert out["ok"] and out["kanban_cleanup"] == ["notify-unsubscribe", "archive"]
    unsub = [c for c in runner.calls if "notify-unsubscribe" in c][0]
    assert unsub[unsub.index("--thread-id") + 1] == "10" and b["kanban_task"] in unsub
    arch = [c for c in runner.calls if "archive" in c][0]
    assert arch[-1] == b["kanban_task"]
    assert json.loads(hw.wake_list({}))["watches"] == []
    _as_chat(monkeypatch, "-100", "9")
    assert [w["id"] for w in json.loads(hw.wake_list({}))["watches"]] == [a["watch_id"]]
    # a cancelled watch is never completed
    (tmp / "b").write_text("x"); core.tick(store, kb); core.tick(store, kb)
    assert not [c for c in runner.calls if "complete" in c and b["kanban_task"] in c]


def test_wake_list_requires_origin(monkeypatch):
    monkeypatch.setattr(hw, "_getenv", lambda n, d="": d)
    assert not json.loads(hw.wake_list({}))["ok"]
