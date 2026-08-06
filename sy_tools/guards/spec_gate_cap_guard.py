#!/usr/bin/env python3
"""PreToolUse guard: a per-session ceiling on how many times `sy:spec-gate` may be dispatched.

A spec review that keeps surfacing new problems can iterate without end, each round paying for a full
review pass and none of them reaching a disposition. `spec.max_spec_gate_rounds` bounds that: the
cap-th dispatch of a session is the last one allowed, the next one is denied, and the deny reason names
the only three ways out (raise the cap explicitly, sign off with the open findings named as accepted
residual risk, or reconsider the approach) so the session stops and asks instead of retrying. The
budget is per session, not per plan or per ticket.

The ledger is one file per session at ~/.claude/shipyard/spec-gate-dispatch-count/<session-id>.jsonl,
one JSON line per allowed dispatch, appended with `O_APPEND` so concurrent hook processes cannot lose a
line. Sessions never share a file, so one session's rounds can never be spent by another.

Failing to reach a decision here allows the dispatch. That is the opposite of `secret_guard.py`'s
fail-closed stance next door, deliberately: this hook gates the only agent that can review and
disposition a spec, so a wrong deny stops the work outright, while a missed count costs one extra
review round that the user still sees happen. Every fail-open path therefore reports itself in the
hook's `systemMessage` — enforcement that has quietly stopped applying is the failure worth naming, and
an exit-0 `PreToolUse` hook's stderr reaches no human. A corrupted or unreadable ledger reads as zero
prior dispatches by the same rule: never as a high count, which would deny on the strength of a file
this guard could not read.

Commands:
  (no args)   read Claude Code hook JSON from stdin; deny an over-cap sy:spec-gate dispatch
  self-test
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from sy_tools.config import get as config_get

AGENT_TOOL_NAMES = {"Agent", "Task"}
GUARDED_AGENT_TYPE = "spec-gate"
CAP_KEY = "spec.max_spec_gate_rounds"
LEDGER_ROOT = Path.home() / ".claude" / "shipyard" / "spec-gate-dispatch-count"


def emit(reason: str | None, warning: str | None) -> None:
    """The hook's one JSON object on stdout: the deny decision, the degraded-enforcement warning, or both.

    `warning` goes in the top-level `systemMessage` field, which per Claude Code's hook-output contract
    is surfaced to the user on an allow decision too. Writing nothing at all is the allow.
    """
    payload: dict = {}
    if reason is not None:
        payload["hookSpecificOutput"] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    if warning is not None:
        payload["systemMessage"] = warning
    if payload:
        print(json.dumps(payload))


def decision(event: dict) -> tuple[str | None, str | None]:
    """The deny reason and the degraded-enforcement warning for one tool call; either may be None.

    Records every `sy:spec-gate` dispatch it allows, and denies the cap+1-th of a session and each one
    after it. Anything that is not a genuine `spec-gate` dispatch allows silently and writes nothing.
    """
    tool = event.get("tool_name")
    if not isinstance(tool, str) or tool not in AGENT_TOOL_NAMES:
        return None, None
    args = event.get("tool_input")
    if not isinstance(args, dict):
        return None, None
    if _normalized_agent_type(args.get("subagent_type")) != GUARDED_AGENT_TYPE:
        return None, None
    cap, warning = _resolved_cap()
    if cap is None:
        return None, warning
    session_id = str(event.get("session_id") or "").strip()
    path = _ledger_path(session_id)
    count, warning = _prior_dispatches(path)
    if count >= cap:
        return _deny_reason(count, cap), warning
    return None, _record_dispatch(path, session_id) or warning


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "self-test":
        _self_test()
        print("spec_gate_cap_guard self-test passed")
        return
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise TypeError(f"the hook event is a JSON {type(event).__name__}, not an object")
        reason, warning = decision(event)
    # SystemExit is no Exception subclass and would otherwise escape; sy_tools.config can raise it.
    except (SystemExit, Exception) as exc:
        emit(None, _degraded(f"this dispatch could not be evaluated ({exc!r})"))
        return
    emit(reason, warning)


def _normalized_agent_type(agent_type: object) -> str:
    if not agent_type:
        return ""
    return str(agent_type).split(":")[-1].strip()


def _resolved_cap() -> tuple[int | None, str | None]:
    try:
        value = int(config_get(CAP_KEY))  # ty: ignore[invalid-argument-type]
    # Every failure, not an enumeration: `config.get` is a flat-dict lookup that applies no schema, so a
    # dict-, None- or text-shaped value raises TypeError/ValueError here and must reach this same
    # fail-open rather than main()'s generic backstop. SystemExit is no Exception subclass and
    # sy_tools.config can raise it; OSError arrives because the resolver shells out to `git`.
    except (SystemExit, Exception) as exc:
        return None, _unusable_cap(repr(exc))
    if value < 1:
        # schema.json's `"minimum": 1` binds on write, never on this read: a cap below 1 would deny
        # every dispatch of the session including the first, so it is no cap at all.
        return None, _unusable_cap(f"{value} is not a positive number of rounds")
    return value, None


def _ledger_path(session_id: str) -> Path:
    safe = "".join(ch for ch in session_id if ch.isalnum() or ch in "-_")
    if not safe:
        raise ValueError("session_id has no safe characters")
    return LEDGER_ROOT / f"{safe}.jsonl"


def _prior_dispatches(path: Path) -> tuple[int, str | None]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0, None
    except (OSError, UnicodeDecodeError) as exc:
        return 0, _unreadable_ledger(path, exc)
    count = 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            return 0, _unreadable_ledger(path, exc)
        if not isinstance(entry, dict):
            return 0, _unreadable_ledger(path, f"line {count + 1} is a JSON {type(entry).__name__}")
        count += 1
    return count, None


def _record_dispatch(path: Path, session_id: str) -> str | None:
    """Spend one round of the session's budget; returns the warning if the ledger could not be written.

    A read-only `$HOME`, a full disk or a permission fault leaves the round uncounted, which is its own
    reportable failure: the dispatch was evaluated and allowed, only the accounting was lost.
    """
    # `datetime.UTC` is 3.11+ and a hook runs this on bare `python`, 3.9 on some machines.
    entry = {"session_id": session_id, "at": datetime.now(timezone.utc).isoformat()}  # noqa: UP017
    line = json.dumps(entry, separators=(",", ":"), sort_keys=True) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # O_APPEND keeps each small event write atomic on normal local filesystems.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as exc:
        return _uncounted_round(path, exc)
    return None


def _deny_reason(count: int, cap: int) -> str:
    return (
        f"spec-gate dispatch cap reached: this session has already dispatched sy:spec-gate {count} "
        f"time(s) and {CAP_KEY} is {cap}. Do not retry this dispatch and do not route around it. Stop "
        "and put three options to the user, whose choice is the only way on: (i) continue iterating "
        f"anyway — raise {CAP_KEY} for this run explicitly, never a silent default — then retry; "
        "(ii) proceed to sign-off with the current undispositioned findings named as accepted residual "
        "risk; (iii) reconsider the approach from scratch."
    )


def _degraded(detail: str) -> str:
    return f"spec_gate_cap_guard: {detail}, so {CAP_KEY} is not being enforced for this dispatch."


def _unusable_cap(detail: str) -> str:
    return _degraded(f"{CAP_KEY} could not be resolved and validated as a positive integer ({detail})")


def _uncounted_round(path: Path, detail: object) -> str:
    """Deliberately unlike `_degraded`: the cap held for this call and stops holding for every later one."""
    return (
        f"spec_gate_cap_guard: dispatch allowed but this round was not counted (writing the ledger "
        f"{path} failed: {detail}), so {CAP_KEY} will not bind again for this session."
    )


def _unreadable_ledger(path: Path, detail: object) -> str:
    return _degraded(
        f"the dispatch ledger {path} could not be read ({detail}) and counts as zero prior dispatches"
    )


_SELF_TEST_CAP = 3


def _self_test() -> None:
    """Every case, run from the `self-test` argv and from the pytest module of the same name.

    `LEDGER_ROOT` and the resolved cap are both swapped for temporaries, so no case touches the real
    ledger and none depends on the `spec.max_spec_gate_rounds` of whatever checkout it runs in — which
    is also what makes the unresolvable-cap case reachable at all.
    """
    # Imported here rather than at module scope: the hook path pays every import on each Agent call.
    import tempfile

    live_cap, original_root = _resolved_cap, LEDGER_ROOT
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for index, case in enumerate((
                _test_a_dispatch_that_is_not_spec_gate_allows_at_any_count,
                _test_both_agent_tool_names_and_namespaced_types_are_recognized,
                _test_the_cap_th_dispatch_is_the_last_one_allowed,
                _test_a_corrupted_ledger_counts_as_zero_prior_dispatches,
                _test_two_sessions_do_not_share_one_budget,
                _test_the_hook_json_output_is_the_deny_contract_claude_code_reads,
                _test_a_ledger_that_cannot_be_written_allows_an_uncounted_round,
            )):
                globals()["LEDGER_ROOT"] = Path(tmp) / f"case-{index}"
                globals()["_resolved_cap"] = lambda: (_SELF_TEST_CAP, None)
                case()
            globals()["_resolved_cap"] = live_cap
            for index, case in enumerate((
                _test_a_cap_below_one_fails_open_instead_of_locking_the_session_out,
                _test_an_unresolvable_cap_allows_and_reports_it,
            )):
                globals()["LEDGER_ROOT"] = Path(tmp) / f"live-cap-{index}"
                case()
    finally:
        globals()["LEDGER_ROOT"], globals()["_resolved_cap"] = original_root, live_cap


def _event(session_id: str, tool: str = "Agent", subagent_type: str | None = GUARDED_AGENT_TYPE) -> dict:
    return {"session_id": session_id, "tool_name": tool, "tool_input": {"subagent_type": subagent_type}}


def _hook_output(event: dict) -> dict:
    """`main()` driven the way Claude Code drives it: the event on stdin, the parsed payload from stdout.

    Nothing below `decision()` can assert the JSON key names Claude Code actually reads, and a hook that
    prints nothing is read as an allow — so a misspelling anywhere in `emit()` is a silent allow.
    """
    # Imported here rather than at module scope: the hook path pays every import on each Agent call.
    import contextlib
    import io

    saved_stdin, saved_argv = sys.stdin, sys.argv
    captured = io.StringIO()
    try:
        sys.argv = [saved_argv[0]]  # the hook's stdin path, not the self-test this may be running inside
        sys.stdin = io.StringIO(json.dumps(event))
        with contextlib.redirect_stdout(captured):
            main()
    finally:
        sys.stdin, sys.argv = saved_stdin, saved_argv
    printed = captured.getvalue()
    return json.loads(printed) if printed.strip() else {}


def _test_a_dispatch_that_is_not_spec_gate_allows_at_any_count() -> None:
    path = _ledger_path("other")
    for _ in range(_SELF_TEST_CAP + 5):
        _record_dispatch(path, "other")
    spent = _prior_dispatches(path)[0]
    for tool, subagent_type in (
        ("Agent", "gate"),
        ("Task", "sy:hunt"),
        ("Agent", "spec-gate-review"),
        ("Task", "specgate"),
        ("Bash", "spec-gate"),
        ("Agent", None),
    ):
        got = decision(_event("other", tool, subagent_type))
        assert got == (None, None), f"{tool}/{subagent_type} is not a spec-gate dispatch: {got!r}"
    assert _prior_dispatches(path)[0] == spent, "a dispatch this guard does not gate must not be recorded"


def _test_both_agent_tool_names_and_namespaced_types_are_recognized() -> None:
    spellings = [("Agent", "spec-gate"), ("Task", "spec-gate"), ("Agent", "sy:spec-gate"), ("Task", "sy:spec-gate")]
    for tool, subagent_type in spellings[:_SELF_TEST_CAP]:
        reason, _ = decision(_event("mixed", tool, subagent_type))
        assert reason is None, f"{tool}/{subagent_type} within the cap must allow: {reason!r}"
    tool, subagent_type = spellings[_SELF_TEST_CAP]
    reason, _ = decision(_event("mixed", tool, subagent_type))
    assert reason is not None, f"{tool}/{subagent_type} must count against the same budget as the others"


def _test_the_cap_th_dispatch_is_the_last_one_allowed() -> None:
    path = _ledger_path("capped")
    for n in range(1, _SELF_TEST_CAP + 1):
        reason, warning = decision(_event("capped"))
        assert reason is None, f"dispatch {n} of {_SELF_TEST_CAP} must allow: {reason!r}"
        assert warning is None, f"dispatch {n} of {_SELF_TEST_CAP} must not warn: {warning!r}"
        assert _prior_dispatches(path)[0] == n, f"dispatch {n} must be recorded exactly once"
    for extra in (1, 2):
        reason, _ = decision(_event("capped"))
        assert reason is not None, f"dispatch {_SELF_TEST_CAP + extra} is past the cap and must deny"
        for named in (f"{_SELF_TEST_CAP} time(s)", CAP_KEY, "raise", "residual risk", "from scratch"):
            assert named in reason, f"the deny reason must name {named!r}: {reason!r}"
        assert _prior_dispatches(path)[0] == _SELF_TEST_CAP, "a denied dispatch must not consume a round"


def _test_a_corrupted_ledger_counts_as_zero_prior_dispatches() -> None:
    path = _ledger_path("corrupt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(['{"session_id":"corrupt"}'] * 9 + ["not json at all"]), encoding="utf-8")
    count, warning = _prior_dispatches(path)
    assert count == 0, f"a corrupted ledger must read as zero prior dispatches, not {count}"
    assert warning and str(path) in warning, f"the dropped count must be reported: {warning!r}"
    reason, warning = decision(_event("corrupt"))
    assert reason is None, f"a ledger this guard cannot read must never deny: {reason!r}"
    assert warning and CAP_KEY in warning, f"the dropped enforcement must be reported: {warning!r}"
    count, warning = _prior_dispatches(_ledger_path("corrupt").parent)
    assert count == 0, f"an unreadable ledger path must read as zero prior dispatches, not {count}"
    assert warning, "and must report that it could not be read"


def _test_two_sessions_do_not_share_one_budget() -> None:
    for n in range(1, _SELF_TEST_CAP + 1):
        reason, _ = decision(_event("session-a"))
        assert reason is None, f"session-a dispatch {n} must allow: {reason!r}"
    assert decision(_event("session-a"))[0] is not None, "session-a must be capped once its rounds are spent"
    reason, _ = decision(_event("session-b"))
    assert reason is None, f"a second session starts with a full budget: {reason!r}"
    assert _prior_dispatches(_ledger_path("session-a"))[0] == _SELF_TEST_CAP, "session-a's count is its own"
    assert _prior_dispatches(_ledger_path("session-b"))[0] == 1, "session-b's count is its own"


def _test_the_hook_json_output_is_the_deny_contract_claude_code_reads() -> None:
    """The whole hook, stdin to stdout: an in-budget allow prints nothing, the over-cap one denies."""
    for n in range(1, _SELF_TEST_CAP + 1):
        payload = _hook_output(_event("emitted"))
        assert payload == {}, f"dispatch {n} of {_SELF_TEST_CAP} must print nothing at all: {payload!r}"
    payload = _hook_output(_event("emitted"))
    decided = payload.get("hookSpecificOutput")
    assert isinstance(decided, dict), f"the over-cap dispatch must emit a hook decision: {payload!r}"
    assert decided.get("hookEventName") == "PreToolUse", payload
    assert decided.get("permissionDecision") == "deny", f"the over-cap dispatch must deny: {payload!r}"
    reason = decided.get("permissionDecisionReason") or ""
    for named in (f"{_SELF_TEST_CAP} time(s)", f"{CAP_KEY} is {_SELF_TEST_CAP}"):
        assert named in reason, f"the emitted reason must name {named!r}: {payload!r}"
    assert "systemMessage" not in payload, f"a deny this guard reached is not degraded: {payload!r}"


def _test_a_ledger_that_cannot_be_written_allows_an_uncounted_round() -> None:
    """An unwritable ledger allows, and says the round went uncounted rather than that nothing was decided.

    `LEDGER_ROOT` is made a regular file, so both the read and the `O_APPEND` write raise `OSError` the
    way a read-only `$HOME` or a full disk would. The warning has to be the top-level `systemMessage`:
    an exit-0 `PreToolUse` hook's stderr reaches no human.
    """
    LEDGER_ROOT.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_ROOT.write_text("", encoding="utf-8")
    for n in range(1, _SELF_TEST_CAP + 2):
        payload = _hook_output(_event("unwritable"))
        assert "hookSpecificOutput" not in payload, f"dispatch {n} must not deny on a write fault: {payload!r}"
        warning = payload.get("systemMessage") or ""
        assert "not counted" in warning, f"the lost round must be named as lost: {payload!r}"
        assert str(_ledger_path("unwritable")) in warning, f"and must name the ledger: {warning!r}"
        assert CAP_KEY in warning, f"and the enforcement it costs: {warning!r}"
        assert "could not be evaluated" not in warning, f"the dispatch *was* evaluated: {warning!r}"


def _test_a_cap_below_one_fails_open_instead_of_locking_the_session_out() -> None:
    """`config.get` is a flat-dict read, so `schema.json`'s `"minimum": 1` never binds here.

    A resolved cap of `0` denies from the very first dispatch onward for the rest of the session, which
    includes `/sy:spec`'s own mandatory spec-gate pass — an unusable cap must therefore fail open like an
    unresolvable one. The malformed shapes are the same read with no schema behind it.
    """
    original = config_get
    try:
        for value in (0, -3, None, {"rounds": 2}, "some", [2]):
            globals()["config_get"] = lambda _key, _value=value: _value
            cap, warning = _resolved_cap()
            assert cap is None, f"a cap of {value!r} must not be trusted as {cap!r}"
            assert warning and CAP_KEY in warning, f"the dropped enforcement must be reported: {warning!r}"
            reason, decided_warning = decision(_event("below-one"))
            assert reason is None, f"a cap of {value!r} must never deny a dispatch: {reason!r}"
            assert decided_warning == warning, f"and must report the same drop: {decided_warning!r}"
            assert not LEDGER_ROOT.exists(), "and must not spend a round it cannot bound"
        globals()["config_get"] = lambda _key: 1
        assert _resolved_cap() == (1, None), "a cap of exactly 1 is the smallest usable one, not a fault"
    finally:
        globals()["config_get"] = original


def _test_an_unresolvable_cap_allows_and_reports_it() -> None:
    """The live resolver, pointed at a directory that is no checkout, so the refusal is real."""
    import tempfile

    from sy_tools import config as sy_config

    saved_pointer = os.environ.get("CLAUDE_PROJECT_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["CLAUDE_PROJECT_DIR"] = str(Path(tmp) / "not-a-checkout")
        sy_config.reset_cache()
        try:
            cap, warning = _resolved_cap()
            assert cap is None, f"an unresolvable config must not yield a cap: {cap!r}"
            assert warning and CAP_KEY in warning, f"the drop must be reported: {warning!r}"
            reason, warning = decision(_event("unresolved"))
            assert reason is None, f"an unresolvable cap must never deny a dispatch: {reason!r}"
            assert warning and CAP_KEY in warning, f"the dropped enforcement must be reported: {warning!r}"
            assert not LEDGER_ROOT.exists(), "and must not spend a round it cannot bound"
        finally:
            if saved_pointer is None:
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
            else:
                os.environ["CLAUDE_PROJECT_DIR"] = saved_pointer
            sy_config.reset_cache()


if __name__ == "__main__":
    main()
