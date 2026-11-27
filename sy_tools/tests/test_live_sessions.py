"""`live_sessions.snapshot` against a synthetic config home and scratch root built here.

Live rows use this test process's own PID, and its parent's, so the liveness checks run for real.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from sy_tools import live_sessions

LINUX = pytest.mark.skipif(sys.platform != "linux", reason="reads /proc")
POSIX = pytest.mark.skipif(os.name != "posix", reason="signal 0 liveness is POSIX only")


def _proc_start(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def _register(home: Path, filename: str, record: Any) -> Path:
    sessions = home / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    path = sessions / filename
    path.write_text(record if isinstance(record, str) else json.dumps(record), encoding="utf-8")
    return path


def _row(session_id: str = "s1", pid: Any = None, **fields: Any) -> dict[str, Any]:
    return {
        "pid": os.getpid() if pid is None else pid,
        "sessionId": session_id,
        "cwd": "/work/repo",
        "startedAt": 1_767_225_600_000,
        "name": "fix the parser",
        "status": "busy",
        "statusUpdatedAt": 1_767_225_660_000,
        "entrypoint": "cli",
        **fields,
    }


def _transcript(home: Path, session_id: str, records: list[Any], project: str = "-work-repo") -> Path:
    directory = home / "projects" / project
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_bytes(b"".join(r if isinstance(r, bytes) else json.dumps(r).encode() + b"\n" for r in records))
    return path


def _typed(name: str, args: str, **extra: Any) -> dict[str, Any]:
    content = f"<command-message>{name[1:]}</command-message>\n<command-name>{name}</command-name>"
    if args:
        content += f"\n<command-args>{args}</command-args>"
    return {"type": "user", "turnOrigin": "human", "message": {"role": "user", "content": content}, **extra}


def _says(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def _only(result: dict[str, Any]) -> dict[str, Any]:
    assert len(result["sessions"]) == 1, result
    return result["sessions"][0]


@POSIX
def test_a_live_session_is_reported_with_its_registry_fields(tmp_path):
    record = _row()
    if sys.platform == "linux":
        record["procStart"] = _proc_start(os.getpid())
    _register(tmp_path / "home", "1.json", record)
    session = _only(live_sessions.snapshot(tmp_path / "home", tmp_path / "scratch"))
    assert session == {
        "pid": os.getpid(), "session_id": "s1", "name": "fix the parser", "state": "busy",
        "state_since": "2026-01-01T00:01:00Z", "started_at": "2026-01-01T00:00:00Z", "entrypoint": "cli",
        "cwd": "/work/repo",
        "liveness": "pid-and-start-time" if sys.platform == "linux" else "pid-only",
        "stale_copy_of": None, "command": None, "ticket": None, "ship": None, "last_step": None,
    }


@POSIX
def test_only_json_files_in_the_registry_are_opened(tmp_path, monkeypatch):
    """The `*.key` siblings are secrets: one that is unreadable must not even be attempted."""
    home = tmp_path / "home"
    _register(home, "1.json", _row())
    key = home / "sessions" / ".1.key"
    key.write_text("secret", encoding="utf-8")
    key.chmod(0)
    opened: list[str] = []
    real_open = io.open

    def recording_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(io, "open", recording_open)
    result = live_sessions.snapshot(home, tmp_path / "scratch")
    registry = [path for path in opened if path.startswith(str(home / "sessions"))]
    assert registry == [str(home / "sessions" / "1.json")], opened
    assert result["warnings"] == [], result


@POSIX
def test_malformed_registry_rows_are_skipped_and_counted(tmp_path):
    home = tmp_path / "home"
    _register(home, "a.json", "{not json")
    _register(home, "b.json", ["a", "list"])
    _register(home, "c.json", _row(pid="123"))
    _register(home, "d.json", _row(pid=True))
    _register(home, "e.json", _row(pid=0))
    _register(home, "f.json", {"pid": os.getpid(), "futureField": {"x": 1}})
    (home / "sessions" / "g.json").write_bytes(b"\xff\xfe")
    result = live_sessions.snapshot(home, tmp_path / "scratch")
    assert result["skipped"]["invalid"] == 6, result
    session = _only(result)
    assert session["session_id"] is None and session["name"] is None and session["started_at"] is None, session


@POSIX
def test_a_registry_file_that_vanishes_mid_read_is_one_warning(tmp_path, monkeypatch):
    home = tmp_path / "home"
    gone = _register(home, "a.json", _row("gone"))
    _register(home, "b.json", _row("kept"))
    real = Path.read_bytes

    def vanishing(self: Path) -> bytes:
        if self == gone:
            raise FileNotFoundError(self)
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", vanishing)
    result = live_sessions.snapshot(home, tmp_path / "scratch")
    assert _only(result)["session_id"] == "kept", result
    assert result["warnings"] == ["registry file a.json unreadable: FileNotFoundError"], result


@POSIX
def test_a_registry_row_with_an_int_too_long_to_convert_is_skipped(tmp_path):
    home = tmp_path / "home"
    _register(home, "a.json", '{"pid": 1, "startedAt": ' + "9" * 5000 + "}")
    _register(home, "b.json", _row("kept"))
    result = live_sessions.snapshot(home, tmp_path / "scratch")
    assert _only(result)["session_id"] == "kept", result
    assert result["skipped"]["invalid"] == 1, result


def test_a_missing_registry_is_an_empty_list_and_nothing_is_created(tmp_path):
    home, scratch = tmp_path / "home", tmp_path / "scratch"
    result = live_sessions.snapshot(home, scratch)
    assert result["sessions"] == [] and len(result["warnings"]) == 1, result
    assert not home.exists() and not scratch.exists()


@POSIX
def test_a_missing_scratch_root_is_not_created(tmp_path):
    home, scratch = tmp_path / "home", tmp_path / "scratch"
    _register(home, "1.json", _row())
    _transcript(home, "s1", [_typed("/sy:ship", "PROJ-1")])
    assert _only(live_sessions.snapshot(home, scratch))["ticket"] == "PROJ-1"
    assert not scratch.exists()


@POSIX
def test_a_dead_pid_is_not_shown(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    _register(tmp_path / "home", "1.json", _row(pid=child.pid))
    result = live_sessions.snapshot(tmp_path / "home", tmp_path / "scratch")
    assert result["sessions"] == [] and result["skipped"]["dead"] == 1, result


@LINUX
def test_a_reused_pid_is_not_shown(tmp_path):
    """The PID is alive, but its start time is not the registered one: another process holds it now."""
    _register(tmp_path / "home", "1.json", _row(procStart="1"))
    result = live_sessions.snapshot(tmp_path / "home", tmp_path / "scratch")
    assert result["sessions"] == [] and result["skipped"]["dead"] == 1, result


@LINUX
def test_a_proc_stat_file_that_vanishes_counts_as_dead(tmp_path, monkeypatch):
    _register(tmp_path / "home", "1.json", _row(procStart=_proc_start(os.getpid())))
    real = Path.read_bytes

    def vanishing(self: Path) -> bytes:
        if str(self).startswith("/proc/"):
            raise FileNotFoundError(self)
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", vanishing)
    result = live_sessions.snapshot(tmp_path / "home", tmp_path / "scratch")
    assert result["sessions"] == [] and result["skipped"]["dead"] == 1, result


@LINUX
def test_a_foreign_pid_domain_is_not_shown(tmp_path):
    home = tmp_path / "home"
    _register(home, "1.json", _row("foreign", pidDomain="linux:another-machine:pid:[1]"))
    _register(home, "2.json", _row("unstamped"))
    result = live_sessions.snapshot(home, tmp_path / "scratch")
    assert _only(result)["session_id"] == "unstamped", result
    assert result["skipped"]["foreign_domain"] == 1, result


@POSIX
def test_macos_checks_the_pid_only_and_has_no_domain_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(live_sessions, "sys", SimpleNamespace(platform="darwin"))
    _register(tmp_path / "home", "1.json", _row(procStart="1", pidDomain="linux:another-machine:pid:[1]"))
    assert _only(live_sessions.snapshot(tmp_path / "home", tmp_path / "scratch"))["liveness"] == "pid-only"


def test_windows_never_calls_kill_and_marks_rows_unchecked(tmp_path, monkeypatch):
    """On Windows signal 0 is `CTRL_C_EVENT`, so a liveness probe would interrupt the session."""
    kills: list[tuple[Any, ...]] = []

    class _Windows:
        name = "nt"

        def kill(self, *args: Any) -> None:
            kills.append(args)

        def __getattr__(self, attribute: str) -> Any:
            return getattr(os, attribute)

    monkeypatch.setattr(live_sessions, "os", _Windows())
    monkeypatch.setattr(live_sessions, "sys", SimpleNamespace(platform="win32"))
    _register(tmp_path / "home", "1.json", _row(pid=4242))
    assert _only(live_sessions.snapshot(tmp_path / "home", tmp_path / "scratch"))["liveness"] == "unchecked"
    assert kills == []


@POSIX
def test_the_older_of_two_live_rows_with_one_session_id_is_a_stale_copy(tmp_path):
    home = tmp_path / "home"
    _register(home, "old.json", _row(pid=os.getppid(), startedAt=1_000))
    _register(home, "new.json", _row(pid=os.getpid(), startedAt=2_000))
    by_pid = {s["pid"]: s for s in live_sessions.snapshot(home, tmp_path / "scratch")["sessions"]}
    assert by_pid[os.getppid()]["stale_copy_of"] == os.getpid(), by_pid
    assert by_pid[os.getpid()]["stale_copy_of"] is None, by_pid


@POSIX
def test_a_start_time_too_large_for_a_float_still_orders_the_copies(tmp_path):
    home = tmp_path / "home"
    _register(home, "old.json", _row(pid=os.getppid(), startedAt=1_000))
    _register(home, "new.json", _row(pid=os.getpid(), startedAt=10**400))
    by_pid = {s["pid"]: s for s in live_sessions.snapshot(home, tmp_path / "scratch")["sessions"]}
    assert by_pid[os.getppid()]["stale_copy_of"] == os.getpid(), by_pid
    assert by_pid[os.getpid()]["started_at"] is None, by_pid


@POSIX
def test_commands_come_only_from_human_typed_command_records(tmp_path):
    """Command tags quoted in a tool result, an assistant reply or an unmarked record are not commands."""
    home = tmp_path / "home"
    _register(home, "1.json", _row())
    quoted = _typed("/sy:ship", "PROJ-9")["message"]["content"]
    _transcript(home, "s1", [
        _typed("/sy:spec", "PROJ-1 tighten the plan"),
        _typed("/sy:help", "foo"),
        _typed("/sy:status", ""),
        {"type": "user", "turnOrigin": "human",
         "message": {"content": [{"type": "tool_result", "content": quoted}]}},
        _says({"type": "text", "text": quoted}),
        {"type": "user", "message": {"content": quoted}},
        {"type": "user", "turnOrigin": "tool", "message": {"content": quoted}},
        {"type": "user", "turnOrigin": "human", "message": {"content": "see " + quoted}},
    ])
    session = _only(live_sessions.snapshot(home, tmp_path / "scratch"))
    assert session["command"] == "/sy:status", session
    assert session["ticket"] == "PROJ-1", session


@POSIX
def test_a_ship_without_arguments_clears_an_earlier_ticket(tmp_path):
    home = tmp_path / "home"
    _register(home, "1.json", _row())
    _transcript(home, "s1", [_typed("/sy:spec", "PROJ-1"), _typed("/sy:ship", "")])
    session = _only(live_sessions.snapshot(home, tmp_path / "scratch"))
    assert session["command"] == "/sy:ship" and session["ticket"] is None, session


@POSIX
def test_an_undecodable_command_line_is_a_warning(tmp_path):
    home = tmp_path / "home"
    _register(home, "1.json", _row())
    _transcript(home, "s1", [_typed("/sy:ship", "PROJ-1"), b"<command-name>/sy:ship \xff\xfe\n"])
    result = live_sessions.snapshot(home, tmp_path / "scratch")
    assert _only(result)["ticket"] == "PROJ-1", result
    assert result["warnings"] == ["undecodable line 2 in s1.jsonl"], result


@POSIX
def test_a_command_line_with_an_int_too_long_to_convert_is_not_a_command(tmp_path):
    home = tmp_path / "home"
    _register(home, "1.json", _row())
    huge = b'{"type": "user", "n": ' + b"9" * 5000 + b', "x": "<command-name>/sy:ship PROJ-9"}\n'
    _transcript(home, "s1", [_typed("/sy:ship", "PROJ-1"), huge])
    result = live_sessions.snapshot(home, tmp_path / "scratch")
    assert _only(result)["command"] == "/sy:ship PROJ-1", result
    assert result["warnings"] == [], result


@POSIX
def test_a_last_line_nested_too_deep_to_decode_is_passed_over(tmp_path, monkeypatch):
    """The decoder's `RecursionError` is simulated: some Python versions decode a nesting this deep, and a
    deeper one does not fit in the 64 KiB tail."""
    home = tmp_path / "home"
    _register(home, "1.json", _row())
    _transcript(home, "s1", [_says({"type": "text", "text": "final"}), b"[" * 10_000 + b"]" * 10_000 + b"\n"])
    real = json.loads

    def shallow(text: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(text, str) and text.startswith("[["):
            raise RecursionError("maximum recursion depth exceeded while decoding a JSON array")
        return real(text, *args, **kwargs)

    monkeypatch.setattr(live_sessions.json, "loads", shallow)
    result = live_sessions.snapshot(home, tmp_path / "scratch")
    assert _only(result)["last_step"] == "says: final", result
    assert result["warnings"] == [], result


@POSIX
@pytest.mark.parametrize(("blocks", "expected"), [
    ([{"type": "tool_use", "name": "AskUserQuestion", "input": {"questions": [{"question": "Merge now?"}]}}],
     "asks: Merge now?"),
    ([{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}], "runs Bash"),
    ([{"type": "text", "text": "\nDone.\nSecond line."}], "says: Done."),
    ([{"type": "text", "text": "y" * 300}], "says: " + "y" * 300),
    ([{"type": "text", "text": "earlier"}, {"type": "thinking", "thinking": "..."}], "says: earlier"),
])
def test_the_last_step_describes_the_last_assistant_block(tmp_path, blocks, expected):
    home = tmp_path / "home"
    _register(home, "1.json", _row())
    _transcript(home, "s1", [_says({"type": "text", "text": "older"}), _says(*blocks), {"type": "user"}])
    assert _only(live_sessions.snapshot(home, tmp_path / "scratch"))["last_step"] == expected


@POSIX
def test_a_torn_last_line_and_a_long_file_still_give_a_last_step(tmp_path):
    home = tmp_path / "home"
    _register(home, "1.json", _row())
    padding = [_says({"type": "text", "text": "z" * 1000}) for _ in range(100)]
    _transcript(home, "s1", [*padding, _says({"type": "text", "text": "final"}), b'{"type": "assistant", "mess'])
    result = live_sessions.snapshot(home, tmp_path / "scratch")
    assert _only(result)["last_step"] == "says: final", result
    assert result["warnings"] == [], result


@POSIX
def test_several_conversation_files_use_the_newest_with_a_warning(tmp_path):
    home = tmp_path / "home"
    _register(home, "1.json", _row())
    old = _transcript(home, "s1", [_typed("/sy:ship", "PROJ-1")], project="-work-a")
    _transcript(home, "s1", [_typed("/sy:ship", "PROJ-2")], project="-work-b")
    os.utime(old, (1, 1))
    result = live_sessions.snapshot(home, tmp_path / "scratch")
    assert _only(result)["ticket"] == "PROJ-2", result
    assert result["warnings"] == ["2 conversation files for s1: the newest is used"], result


@POSIX
def test_a_conversation_file_that_vanishes_is_a_warning(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _register(home, "1.json", _row())
    gone = _transcript(home, "s1", [_typed("/sy:ship", "PROJ-1")])
    real = Path.open

    def vanishing(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == gone:
            raise FileNotFoundError(self)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", vanishing)
    result = live_sessions.snapshot(home, tmp_path / "scratch")
    assert _only(result)["command"] is None, result
    assert result["warnings"] == ["conversation file s1.jsonl unreadable: FileNotFoundError"] * 2, result


@POSIX
def test_a_session_id_that_is_not_a_plain_id_is_never_globbed(tmp_path):
    home = tmp_path / "home"
    _register(home, "1.json", _row("*"))
    _transcript(home, "anything", [_typed("/sy:ship", "PROJ-1")])
    assert _only(live_sessions.snapshot(home, tmp_path / "scratch"))["ticket"] is None


def _ship_state(scratch: Path, ticket: str, body: str) -> None:
    (scratch / ticket).mkdir(parents=True)
    (scratch / ticket / "ship-state.yaml").write_text(body, encoding="utf-8")


@POSIX
def test_ship_state_joins_on_the_owning_session_before_the_command_ticket(tmp_path):
    home, scratch = tmp_path / "home", tmp_path / "scratch"
    _register(home, "1.json", _row())
    _transcript(home, "s1", [_typed("/sy:spec", "PROJ-2")])
    _ship_state(scratch, "PROJ-1", "ship_session_id: s1\nphase_active: GATE\ngate_rounds_total: 2\npr: 7\n")
    _ship_state(scratch, "PROJ-2", "ship_session_id: other\nphase_active: BUILD\n")
    session = _only(live_sessions.snapshot(home, scratch))
    assert session["ticket"] == "PROJ-1", session
    assert session["ship"] == {"phase_active": "GATE", "gate_rounds_total": 2, "pr": 7}, session


@POSIX
def test_ship_state_falls_back_to_the_command_ticket_and_skips_a_bad_file(tmp_path):
    home, scratch = tmp_path / "home", tmp_path / "scratch"
    _register(home, "1.json", _row())
    _transcript(home, "s1", [_typed("/sy:ship", "PROJ-2")])
    _ship_state(scratch, "PROJ-1", "phase_active: [unclosed\n")
    _ship_state(scratch, "PROJ-2", "phase_active: BUILD\ngate_rounds_total: 0\n")
    _ship_state(scratch, "PROJ-3", "- a list\n")
    result = live_sessions.snapshot(home, scratch)
    session = _only(result)
    assert session["ticket"] == "PROJ-2", session
    assert session["ship"] == {"phase_active": "BUILD", "gate_rounds_total": 0, "pr": None}, session
    assert result["warnings"] == [
        "ship state PROJ-1 unreadable: ParserError", "ship state PROJ-3 is not a mapping",
    ], result


@POSIX
def test_a_ship_state_with_a_date_that_does_not_exist_is_one_warning(tmp_path):
    home, scratch = tmp_path / "home", tmp_path / "scratch"
    _register(home, "1.json", _row())
    _transcript(home, "s1", [_typed("/sy:ship", "PROJ-2")])
    _ship_state(scratch, "PROJ-1", "ship_session_id: s1\nstarted: 2026-02-30\n")
    _ship_state(scratch, "PROJ-2", "phase_active: BUILD\n")
    result = live_sessions.snapshot(home, scratch)
    assert _only(result)["ship"] == {"phase_active": "BUILD", "gate_rounds_total": None, "pr": None}, result
    assert result["warnings"] == ["ship state PROJ-1 unreadable: ValueError"], result


@POSIX
def test_no_ship_state_keeps_the_command_ticket_without_building_a_path(tmp_path):
    home, scratch = tmp_path / "home", tmp_path / "scratch"
    _register(home, "1.json", _row())
    _transcript(home, "s1", [_typed("/sy:ship", "../PROJ-1")])
    _ship_state(scratch, "PROJ-1", "ship_session_id: other\n")
    session = _only(live_sessions.snapshot(home, scratch))
    assert session["ticket"] == "../PROJ-1" and session["ship"] is None, session


@pytest.mark.parametrize(("value", "expected"), [("/work/claude", Path("/work/claude")), ("", None), (None, None)])
def test_claude_home_follows_claude_config_dir(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", value)
    assert live_sessions.claude_home() == (expected or Path.home() / ".claude")
