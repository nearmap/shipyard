#!/usr/bin/env python3
"""PreToolUse guard: a `pr_comment` call must name the session it is actually running in.

Threat model: an agent stamping a pull-request comment with the wrong session. The likely way is not
malice but staleness: an id copied out of earlier context, a transcript, or a parent's prompt after a
`/clear` has moved the live session on, which would mark the comment as written by a session that
never wrote it. The MCP server cannot catch that itself, because it outlives a `/clear` and so its own
environment can be stale too. The hook payload's `session_id` is Claude Code's own word for the live
session, so a `session` argument that differs from it is denied, with the reason naming the fix.

Fail-closed on a mismatch, and on a `session` argument that is absent or not a string. Fail-open,
deliberately, when the payload carries no `session_id` or cannot be parsed: then there is nothing to
compare against, `pr_comment` still refuses any value that is not a UUID, and a wrong deny would stop
every pull-request reply. Each fail-open path reports itself in `systemMessage`, because an exit-0
`PreToolUse` hook's stderr reaches no human.

Commands:
  (no args)   read Claude Code hook JSON from stdin; deny a pr_comment whose session is not this session
  self-test
"""
from __future__ import annotations

import json
import sys

GUARDED_TOOLS = {"mcp__plugin_sy_sy__pr_comment", "mcp__sy__pr_comment"}


def emit(reason: str | None, warning: str | None) -> None:
    """The hook's one JSON object on stdout: the deny decision, the fail-open warning, or both. Nothing is the allow."""
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
    """The deny reason and the fail-open warning for one tool call; either may be None."""
    if event.get("tool_name") not in GUARDED_TOOLS:
        return None, None
    live = event.get("session_id")
    if not isinstance(live, str) or not live.strip():
        return None, "pr_session_guard: no session_id in the hook payload, so this pr_comment's session went unchecked."
    args = event.get("tool_input")
    claimed = args.get("session") if isinstance(args, dict) else None
    if claimed == live:
        return None, None
    return (
        f"pr_comment's session {claimed!r} is not this session. Pass exactly what `echo \"$CLAUDE_CODE_SESSION_ID\"` "
        "prints in your own Bash tool, run now; never an id copied from a prompt, a transcript or earlier context."
    ), None


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "self-test":
        _self_test()
        print("pr_session_guard self-test passed")
        return
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise TypeError(f"the hook event is a JSON {type(event).__name__}, not an object")
    except (ValueError, TypeError) as exc:
        emit(None, f"pr_session_guard: this call could not be evaluated ({exc!r}), so its session was not checked.")
        return
    emit(*decision(event))


_LIVE = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
_STALE = "0b6c2f8e-5a1d-4f7e-9c3b-2d8e1a4f6b70"


def _self_test() -> None:
    """Every case, run from the `self-test` argv and from the pytest module of the same name."""
    for case in (
        _test_a_matching_session_allows_silently,
        _test_a_mismatched_or_missing_session_denies,
        _test_both_server_prefixes_are_guarded_and_nothing_else,
        _test_a_payload_without_session_id_allows_and_warns,
        _test_the_hook_json_output_is_the_deny_contract_claude_code_reads,
    ):
        case()


def _event(session: object, live: object = _LIVE, tool: str = "mcp__plugin_sy_sy__pr_comment") -> dict:
    return {"session_id": live, "tool_name": tool, "tool_input": {"pr_url": "u", "body": "b", "session": session}}


def _hook_output(stdin: str) -> dict:
    """`main()` driven the way Claude Code drives it: the event on stdin, the parsed payload from stdout."""
    # Imported here rather than at module scope: the hook path pays every import on each guarded call.
    import contextlib
    import io

    saved_stdin, saved_argv = sys.stdin, sys.argv
    captured = io.StringIO()
    try:
        sys.argv = [saved_argv[0]]
        sys.stdin = io.StringIO(stdin)
        with contextlib.redirect_stdout(captured):
            main()
    finally:
        sys.stdin, sys.argv = saved_stdin, saved_argv
    printed = captured.getvalue()
    return json.loads(printed) if printed.strip() else {}


def _test_a_matching_session_allows_silently() -> None:
    assert decision(_event(_LIVE)) == (None, None), "the live session must allow with no warning"


def _test_a_mismatched_or_missing_session_denies() -> None:
    for claimed in (_STALE, _LIVE.upper(), f" {_LIVE}", "", None, 7):
        reason, warning = decision(_event(claimed))
        assert reason is not None, f"session {claimed!r} is not the live session and must deny"
        assert "CLAUDE_CODE_SESSION_ID" in reason, f"the deny must name the fix: {reason!r}"
        assert warning is None, f"a deny is not a fail-open: {warning!r}"
    event = _event(_LIVE)
    del event["tool_input"]
    assert decision(event)[0] is not None, "a call with no tool_input names no session and must deny"


def _test_both_server_prefixes_are_guarded_and_nothing_else() -> None:
    for tool in sorted(GUARDED_TOOLS):
        assert decision(_event(_STALE, tool=tool))[0] is not None, f"{tool} must be guarded"
    for tool in ("mcp__plugin_sy_sy__post-comment", "mcp__other__pr_comment", "Bash", "Agent"):
        assert decision(_event(_STALE, tool=tool)) == (None, None), f"{tool} is not this guard's to judge"


def _test_a_payload_without_session_id_allows_and_warns() -> None:
    for live in (None, "", "   "):
        reason, warning = decision(_event(_STALE, live=live))
        assert reason is None, f"with session_id {live!r} there is nothing to compare, so it must allow"
        assert warning and "unchecked" in warning, f"and must say enforcement lapsed: {warning!r}"


def _test_the_hook_json_output_is_the_deny_contract_claude_code_reads() -> None:
    assert _hook_output(json.dumps(_event(_LIVE))) == {}, "an allowed call must print nothing at all"
    payload = _hook_output(json.dumps(_event(_STALE)))
    decided = payload.get("hookSpecificOutput")
    assert isinstance(decided, dict), f"a mismatch must emit a hook decision: {payload!r}"
    assert decided.get("hookEventName") == "PreToolUse", payload
    assert decided.get("permissionDecision") == "deny", payload
    assert _STALE in (decided.get("permissionDecisionReason") or ""), payload
    for garbage in ("not json", "[1]"):
        payload = _hook_output(garbage)
        assert "hookSpecificOutput" not in payload, f"an unreadable event must not deny: {payload!r}"
        assert "not checked" in (payload.get("systemMessage") or ""), f"and must say so: {payload!r}"


if __name__ == "__main__":
    main()
