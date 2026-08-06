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

from sy_tools.config import ConfigError
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
    _record_dispatch(path, session_id)
    return None, warning


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
        return int(config_get(CAP_KEY)), None  # ty: ignore[invalid-argument-type]
    # OSError too: the resolver shells out to `git`, so a `git` missing from PATH raises through here.
    except (SystemExit, ConfigError, OSError) as exc:
        return None, _degraded(f"{CAP_KEY} could not be resolved ({exc})")


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


def _record_dispatch(path: Path, session_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # `datetime.UTC` is 3.11+ and a hook runs this on bare `python`, 3.9 on some machines.
    entry = {"session_id": session_id, "at": datetime.now(timezone.utc).isoformat()}  # noqa: UP017
    line = json.dumps(entry, separators=(",", ":"), sort_keys=True) + "\n"
    # O_APPEND keeps each small event write atomic on normal local filesystems.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


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
            )):
                globals()["LEDGER_ROOT"] = Path(tmp) / f"case-{index}"
                globals()["_resolved_cap"] = lambda: (_SELF_TEST_CAP, None)
                case()
            globals()["LEDGER_ROOT"] = Path(tmp) / "live-cap"
            globals()["_resolved_cap"] = live_cap
            _test_an_unresolvable_cap_allows_and_reports_it()
    finally:
        globals()["LEDGER_ROOT"], globals()["_resolved_cap"] = original_root, live_cap


def _event(session_id: str, tool: str = "Agent", subagent_type: str | None = GUARDED_AGENT_TYPE) -> dict:
    return {"session_id": session_id, "tool_name": tool, "tool_input": {"subagent_type": subagent_type}}


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
