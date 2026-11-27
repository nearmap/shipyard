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
def test_a_live_session_is_reported_with_its_registry_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(live_sessions.time, "time", lambda: 1_767_225_690.9)
    record = _row()
    if sys.platform == "linux":
        record["procStart"] = _proc_start(os.getpid())
    _register(tmp_path / "home", "1.json", record)
    session = _only(live_sessions.snapshot(tmp_path / "home", tmp_path / "scratch"))
    assert session == {
        "pid": os.getpid(), "session_id": "s1", "name": "fix the parser", "state": "busy",
        "state_since": "2026-01-01T00:01:00Z", "state_seconds": 30, "started_at": "2026-01-01T00:00:00Z",
        "entrypoint": "cli",
        "cwd": "/work/repo",
        "liveness": "pid-and-start-time" if sys.platform == "linux" else "pid-only",
        "stale_copy_of": None, "command": None, "ticket": None, "ship": None, "last_step": None,
    }


@POSIX
def test_the_time_in_a_state_is_measured_from_one_clock_read(tmp_path, monkeypatch):
    """At 00:01:30.9 a state set at 00:01:00 is 30 s old; a state set later is 0 s old, and none is `None`."""
    monkeypatch.setattr(live_sessions.time, "time", lambda: 1_767_225_690.9)
    home = tmp_path / "home"
    _register(home, "a.json", _row("old"))
    _register(home, "b.json", _row("future", statusUpdatedAt=1_767_225_700_000))
    _register(home, "c.json", _row("none", statusUpdatedAt=None))
    _register(home, "d.json", _row("huge", statusUpdatedAt=10**400))
    result = live_sessions.snapshot(home, tmp_path / "scratch")
    assert result["generated_at"] == "2026-01-01T00:01:30Z", result
    seconds = {row["session_id"]: row["state_seconds"] for row in result["sessions"]}
    assert seconds == {"old": 30, "future": 0, "none": None, "huge": None}, result


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


@LINUX
def test_an_unreadable_machine_id_applies_no_domain_filter(tmp_path, monkeypatch):
    real = Path.read_text

    def refusing(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == Path("/etc/machine-id"):
            raise PermissionError(self)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", refusing)
    _register(tmp_path / "home", "1.json", _row("foreign", pidDomain="linux:another-machine:pid:[1]"))
    result = live_sessions.snapshot(tmp_path / "home", tmp_path / "scratch")
    assert _only(result)["session_id"] == "foreign", result
    assert result["skipped"]["foreign_domain"] == 0, result


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
        _typed("/sy:spec", "PROJ-1"),
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
    assert result["warnings"] == ["conversation file s1.jsonl unreadable: FileNotFoundError"], result


@POSIX
def test_a_session_id_that_is_not_a_plain_id_is_never_globbed(tmp_path):
    home = tmp_path / "home"
    _register(home, "1.json", _row("*"))
    _transcript(home, "anything", [_typed("/sy:ship", "PROJ-1")])
    assert _only(live_sessions.snapshot(home, tmp_path / "scratch"))["ticket"] is None


@POSIX
def test_a_typed_doit_clears_the_typed_ticket_so_the_owned_state_names_it(tmp_path):
    home, scratch = tmp_path / "home", tmp_path / "scratch"
    _register(home, "1.json", _row())
    _transcript(home, "s1", [_typed("/sy:spec", "PROJ-1"), _typed("/sy:doit", "add a thing")])
    _ship_state(scratch, "PROJ-1", "ship_session_id: other\nphase_active: BUILD\n")
    _ship_state(scratch, "PROJ-9", "ship_session_id: s1\nphase_active: GATE\npr: 4\n")
    session = _only(live_sessions.snapshot(home, scratch))
    assert session["ticket"] == "PROJ-9" and session["ship"]["phase_active"] == "GATE", session


@POSIX
def test_a_ship_typed_after_doit_names_the_ticket_again(tmp_path):
    home, scratch = tmp_path / "home", tmp_path / "scratch"
    _register(home, "1.json", _row())
    _transcript(home, "s1", [_typed("/sy:doit", "x"), _typed("/sy:ship", "PROJ-3")])
    _ship_state(scratch, "PROJ-3", "phase_active: BUILD\n")
    assert _only(live_sessions.snapshot(home, scratch))["ticket"] == "PROJ-3"


@POSIX
def test_owned_states_order_by_the_moment_not_the_spelling(tmp_path):
    home, scratch = tmp_path / "home", tmp_path / "scratch"
    _register(home, "1.json", _row())
    _transcript(home, "s1", [_says({"type": "text", "text": "hello"})])
    own = "ship_session_id: s1\n"
    # 01:00Z as an offset spelling sorts after 02:00Z as text, but is earlier in time.
    _ship_state(scratch, "PROJ-1", own + "ship_session_started_at: '2026-11-27T12:00:00+11:00'\n")
    _ship_state(scratch, "PROJ-2", own + "ship_session_started_at: '2026-11-27T02:00:00Z'\n")
    _ship_state(scratch, "PROJ-3", own + "ship_session_started_at: 2026-11-26\n")
    _ship_state(scratch, "PROJ-4", own + "ship_session_started_at: soon\n")
    assert _only(live_sessions.snapshot(home, scratch))["ticket"] == "PROJ-2"


@POSIX
def test_an_unquoted_stamp_orders_as_a_datetime(tmp_path):
    home, scratch = tmp_path / "home", tmp_path / "scratch"
    _register(home, "1.json", _row())
    _transcript(home, "s1", [_says({"type": "text", "text": "hello"})])
    own = "ship_session_id: s1\n"
    _ship_state(scratch, "PROJ-1", own + "ship_session_started_at: '2026-11-27T04:00:00+00:00'\n")
    _ship_state(scratch, "PROJ-2", own + "ship_session_started_at: 2026-11-27T05:00:00Z\n")
    _ship_state(scratch, "PROJ-3", own + "ship_session_started_at: 2026-11-27T13:30:00+11:00\n")
    assert _only(live_sessions.snapshot(home, scratch))["ticket"] == "PROJ-2"


@LINUX
def test_liveness_reads_the_start_time_from_a_literal_proc_stat(monkeypatch):
    stat = b"4242 (a) b) S 1 4242 4242 0 -1 4194560 100 0 0 0 1 2 0 0 20 0 1 0 987654 1000000 200\n"
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: stat if str(self) == "/proc/4242/stat" else real(self))
    monkeypatch.setattr(live_sessions.os, "kill", lambda pid, sig: None)
    assert live_sessions._liveness(4242, 987654) == "pid-and-start-time"
    assert live_sessions._liveness(4242, 987655) is None


def _ship_state(scratch: Path, ticket: str, body: str) -> None:
    (scratch / ticket).mkdir(parents=True)
    (scratch / ticket / "ship-state.yaml").write_text(f"task: {ticket}\n{body}", encoding="utf-8")


def _raw_state(scratch: Path, relative: str, body: str) -> None:
    (scratch / relative).mkdir(parents=True)
    (scratch / relative / "ship-state.yaml").write_text(body, encoding="utf-8")


@POSIX
def test_a_typed_ticket_outranks_a_state_the_session_owns(tmp_path):
    home, scratch = tmp_path / "home", tmp_path / "scratch"
    _register(home, "1.json", _row())
    _transcript(home, "s1", [_typed("/sy:spec", "PROJ-2")])
    _ship_state(scratch, "PROJ-1", "ship_session_id: s1\nphase_active: GATE\n")
    _ship_state(scratch, "PROJ-2", "ship_session_id: other\nphase_active: BUILD\ngate_rounds_total: 1\n")
    session = _only(live_sessions.snapshot(home, scratch))
    assert session["ticket"] == "PROJ-2", session
    assert session["ship"] == {"phase_active": "BUILD", "gate_rounds_total": 1, "pr": None}, session


@POSIX
def test_a_spec_goal_in_words_names_no_ticket_and_keeps_the_owned_state(tmp_path):
    home, scratch = tmp_path / "home", tmp_path / "scratch"
    _register(home, "1.json", _row())
    _transcript(home, "s1", [_typed("/sy:spec", "make the status table wider")])
    _ship_state(scratch, "PROJ-1", "ship_session_id: s1\nphase_active: GATE\n")
    session = _only(live_sessions.snapshot(home, scratch))
    assert session["ticket"] == "PROJ-1", session
    assert session["ship"]["phase_active"] == "GATE", session


@POSIX
def test_a_state_in_a_nested_task_directory_joins_by_its_task_field(tmp_path):
    home, scratch = tmp_path / "home", tmp_path / "scratch"
    url = "https://tracker.example/acme/repo/issues/7"
    _register(home, "1.json", _row())
    _transcript(home, "s1", [_typed("/sy:ship", url)])
    nested = scratch / Path(url)
    nested.mkdir(parents=True)
    (nested / "ship-state.yaml").write_text(f"task: {url}\nphase_active: GATE\n", encoding="utf-8")
    session = _only(live_sessions.snapshot(home, scratch))
    assert (session["ticket"], session["ship"]["phase_active"]) == (url, "GATE"), session


@POSIX
def test_a_stray_state_file_is_not_a_task_state_and_gives_no_warning(tmp_path):
    home, scratch = tmp_path / "home", tmp_path / "scratch"
    _register(home, "1.json", _row())
    _transcript(home, "s1", [_typed("/sy:ship", "PROJ-1")])
    _ship_state(scratch, "PROJ-1", "phase_active: GATE\n")
    _raw_state(scratch, "shipyard/tmpx/s/T", "phase_active: [unclosed\n")
    _raw_state(scratch, "PROJ-1/backup", "task: PROJ-1\nphase_active: BUILD\n")
    _raw_state(scratch, "PROJ-0", "task: PROJ-1\nphase_active: BUILD\n")
    result = live_sessions.snapshot(home, scratch)
    assert _only(result)["ship"]["phase_active"] == "GATE", result
    assert result["warnings"] == [], result


@POSIX
def test_a_state_without_a_string_task_is_not_joined(tmp_path):
    home, scratch = tmp_path / "home", tmp_path / "scratch"
    _register(home, "1.json", _row())
    _transcript(home, "s1", [_typed("/sy:ship", "42")])
    _raw_state(scratch, "42", "ship_session_id: s1\ntask: 42\nphase_active: GATE\n")
    result = live_sessions.snapshot(home, scratch)
    session = _only(result)
    assert (session["ticket"], session["ship"], result["warnings"]) == ("42", None, []), result


@POSIX
def test_a_state_with_an_empty_or_missing_task_is_not_joined_by_its_owner(tmp_path):
    home, scratch = tmp_path / "home", tmp_path / "scratch"
    _register(home, "1.json", _row())
    _transcript(home, "s1", [_says({"type": "text", "text": "hello"})])
    _raw_state(scratch, "7", "ship_session_id: s1\ntask: ''\nphase_active: GATE\n")
    _raw_state(scratch, "8", "ship_session_id: s1\nphase_active: GATE\n")
    result = live_sessions.snapshot(home, scratch)
    session = _only(result)
    assert (session["ticket"], session["ship"], result["warnings"]) == (None, None, []), result


@POSIX
def test_an_unreadable_state_directly_under_a_scheme_directory_warns_once(tmp_path):
    home, scratch = tmp_path / "home", tmp_path / "scratch"
    _register(home, "1.json", _row())
    _transcript(home, "s1", [_says({"type": "text", "text": "hello"})])
    _raw_state(scratch, "https:", "phase_active: [unclosed\n")
    result = live_sessions.snapshot(home, scratch)
    assert result["warnings"] == ["ship state https: unreadable: ParserError"], result


@POSIX
def test_a_typed_ticket_with_no_state_has_no_ship_progress(tmp_path):
    home, scratch = tmp_path / "home", tmp_path / "scratch"
    _register(home, "1.json", _row())
    _transcript(home, "s1", [_typed("/sy:spec", "PROJ-9")])
    _ship_state(scratch, "PROJ-1", "ship_session_id: s1\nphase_active: GATE\n")
    session = _only(live_sessions.snapshot(home, scratch))
    assert (session["ticket"], session["ship"]) == ("PROJ-9", None), session


@POSIX
def test_with_no_typed_ticket_the_newest_owned_state_wins(tmp_path):
    home, scratch = tmp_path / "home", tmp_path / "scratch"
    _register(home, "1.json", _row())
    _transcript(home, "s1", [_says({"type": "text", "text": "hello"})])
    own = "ship_session_id: s1\n"
    _ship_state(scratch, "PROJ-1", own + "ship_session_started_at: '2026-11-27T01:00:00Z'\nphase_active: GATE\n")
    _ship_state(scratch, "PROJ-2", own + "ship_session_started_at: '2026-11-27T02:00:00Z'\nphase_active: BUILD\n")
    session = _only(live_sessions.snapshot(home, scratch))
    assert session["ticket"] == "PROJ-2", session
    assert session["ship"]["phase_active"] == "BUILD", session


@POSIX
def test_ship_state_falls_back_to_the_command_ticket_and_skips_a_bad_file(tmp_path):
    home, scratch = tmp_path / "home", tmp_path / "scratch"
    _register(home, "1.json", _row())
    _transcript(home, "s1", [_typed("/sy:ship", "PROJ-2")])
    _ship_state(scratch, "PROJ-1", "phase_active: [unclosed\n")
    _ship_state(scratch, "PROJ-2", "phase_active: BUILD\ngate_rounds_total: 0\n")
    _raw_state(scratch, "PROJ-3", "- a list\n")
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
def test_a_ship_state_file_that_vanishes_is_one_warning(tmp_path, monkeypatch):
    home, scratch = tmp_path / "home", tmp_path / "scratch"
    _register(home, "1.json", _row())
    _transcript(home, "s1", [_typed("/sy:ship", "PROJ-1")])
    _ship_state(scratch, "PROJ-1", "ship_session_id: s1\nphase_active: GATE\n")
    gone = scratch / "PROJ-1" / "ship-state.yaml"
    real = Path.read_text

    def vanishing(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == gone:
            raise FileNotFoundError(self)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", vanishing)
    result = live_sessions.snapshot(home, scratch)
    session = _only(result)
    assert session["ticket"] == "PROJ-1" and session["ship"] is None, session
    assert result["warnings"] == ["ship state PROJ-1 unreadable: FileNotFoundError"], result


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
