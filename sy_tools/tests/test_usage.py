"""`usage`'s parser and renderer, against a synthetic transcript tree built here.

Deterministic by construction: every transcript, ledger entry and config layer these tests read is
written by the test, so nothing depends on whichever session happens to be running them.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sy_tools import usage


@pytest.fixture
def session_tree(tmp_path, monkeypatch) -> Path:
    """A synthetic session `s1`: one main transcript, one nested subagent, one legacy sibling dir.

    Returns the main transcript's path.
    """
    # Redirected too, so attribution runs against a written mapping, not the developer's real ledger.
    monkeypatch.setattr(usage, "LEDGER_ROOT", tmp_path / "ledger")
    main = tmp_path / "s1.jsonl"
    subdir = tmp_path / "s1" / "subagents"
    subdir.mkdir(parents=True)
    sub = subdir / "agent-a.jsonl"
    main_records = [
        {
            "type": "assistant",
            "timestamp": "2026-07-09T10:00:00Z",
            "message": {
                "id": "m1",
                "model": "main-model",
                "content": [{"type": "text", "text": "hello world"}],
                "usage": {"input_tokens": 10, "output_tokens": 2},
            },
        },
        {
            "type": "queue-operation",
            "operation": "enqueue",
            "timestamp": "2026-07-09T10:00:01Z",
            "sessionId": "s1",
            "content": "please also check the logs",
        },
        {
            "type": "user",
            "timestamp": "2026-07-09T10:00:02Z",
            "message": {"content": [{"type": "text", "text": "please also check the logs"}]},
        },
        {
            "type": "queue-operation",
            "operation": "remove",
            "timestamp": "2026-07-09T10:00:03Z",
            "sessionId": "s1",
            "content": "cancelled interjection",
        },
        {
            "type": "queue-operation",
            "operation": "enqueue",
            "timestamp": "2026-07-09T10:00:04Z",
            "sessionId": "s1",
            "content": "please continue",
        },
        {
            "type": "queue-operation",
            "operation": "remove",
            "timestamp": "2026-07-09T10:00:05Z",
            "sessionId": "s1",
            "content": "please continue",
        },
        {
            "type": "user",
            "timestamp": "2026-07-09T10:00:06Z",
            "message": {"content": [{"type": "text", "text": "please continue"}]},
        },
    ]
    main.write_text("".join(json.dumps(r) + "\n" for r in main_records), encoding="utf-8")
    # No agent_type here: attribution comes from the same hook ledger real Shipyard agents write.
    sub.write_text(
        json.dumps(
            {
                "agent_id": "agent-a",
                "type": "assistant",
                "message": {
                    "id": "m2",
                    "model": "sub-model",
                    "usage": {
                        "input_tokens": 20,
                        "output_tokens": 4,
                        "cache_read_input_tokens": 7,
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    usage.record_hook_event(
        {
            "session_id": "s1",
            "hook_event_name": "Stop",
            "agent_id": "agent-a",
            "agent_type": "sy:slice",  # plugin-namespaced; normalization strips to "slice"
            "transcript_path": str(sub),
        }
    )
    # Legacy project-level subagents dir: the other-session file must be excluded, not folded in.
    legacy = tmp_path / "subagents"
    legacy.mkdir()
    (legacy / "agent-b.jsonl").write_text(
        json.dumps(
            {
                "sessionId": "s1",
                "type": "assistant",
                "message": {
                    "id": "m3",
                    "model": "sub-model",
                    "usage": {"input_tokens": 5, "output_tokens": 1},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (legacy / "agent-c.jsonl").write_text(
        json.dumps(
            {
                "sessionId": "s2",
                "type": "assistant",
                "message": {
                    "id": "m4",
                    "model": "sub-model",
                    "usage": {"input_tokens": 999, "output_tokens": 999},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return main


def test_resolve_main_transcript_refuses_both_a_session_id_and_a_path(tmp_path):
    """The docstring promises one of the two; silently preferring one over the other misreports a session."""
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="not both"):
        usage.resolve_main_transcript("s1", str(transcript))


def test_summarize_rolls_up_the_whole_tree_and_only_this_session(session_tree):
    """Totals cover main plus both same-session subagents, and the other session's file is excluded."""
    result = usage.summarize(session_tree, phase="ship", task="PROJ-1")
    assert result["totals"]["input_tokens"] == 35, result["totals"]
    assert result["totals"]["output_tokens"] == 7, result["totals"]
    assert result["totals"]["cache_read_input_tokens"] == 7, result["totals"]
    assert result["transcripts"]["subagents"] == 2, result["transcripts"]


def test_summarize_attributes_a_subagent_from_the_hook_ledger(session_tree):
    """A transcript naming no agent_type is still attributed, via the ledger the hook writes."""
    result = usage.summarize(session_tree, phase="ship", task="PROJ-1")
    assert any(row["agent_type"] == "slice" for row in result["by_agent"]), result["by_agent"]


def test_render_covers_the_main_session_and_each_subagent(session_tree):
    rendered = usage.render(session_tree, task="PROJ-1")
    assert "MAIN SESSION s1" in rendered
    assert "hello world" in rendered
    assert "SUBAGENT slice" in rendered


def test_render_shows_each_queued_interjection_exactly_once(session_tree):
    """A queued interjection renders at enqueue; its later delivery must not duplicate it."""
    rendered = usage.render(session_tree, task="PROJ-1")
    assert rendered.count("(queued interjection)") == 2
    assert rendered.count("please also check the logs") == 1


def test_render_marks_a_cancelled_interjection_without_reprinting_it(session_tree):
    rendered = usage.render(session_tree, task="PROJ-1")
    assert "cancelled interjection" not in rendered
    assert rendered.count("cancelled before delivery") == 1


def test_render_keeps_a_genuine_user_turn_after_an_enqueue_and_remove(session_tree):
    """The cancellation bookkeeping must not swallow an identical user turn that really happened."""
    rendered = usage.render(session_tree, task="PROJ-1")
    assert rendered.count("please continue") == 2, "cancelled queue must not eat the later genuine user turn"
    assert "[2026-07-09 10:00:06] USER" in rendered, "genuine user turn after enqueue+remove must render"


def test_render_warns_about_a_transcript_it_could_not_parse(session_tree):
    """A transcript that will not parse must say so in the render; an empty section reads as a quiet agent."""
    broken = session_tree.with_suffix("") / "subagents" / "agent-broken.jsonl"
    broken.write_text("{not json\n", encoding="utf-8")
    rendered = usage.render(session_tree, task="PROJ-1")
    assert any(
        line.startswith("warning:") and "agent-broken.jsonl" in line for line in rendered.splitlines()
    ), rendered


def test_summarize_always_carries_a_warnings_key(session_tree):
    """An absent key on a clean tree is indistinguishable from a consumer forgetting to look for it."""
    result = usage.summarize(session_tree, phase="ship", task="PROJ-1")
    assert result["warnings"] == [], result


@pytest.fixture
def config_layers(tmp_path, monkeypatch):
    """A live but throwaway config layer chain, with `render_limits()`'s cache reset around each use.

    Yields the repo layer directory to write `config.json` into. Both resolvers are pointed at the
    fixture so the real per-leaf resolution path runs, rather than a mock of it.
    """
    from sy_tools import config as sy_config

    home = tmp_path / "home"
    repo = tmp_path / "repo"
    (home / ".shipyard").mkdir(parents=True)
    (repo / ".shipyard").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(sy_config, "repo_root", lambda: repo)
    monkeypatch.setattr(usage, "_RENDER_LIMITS", None)
    sy_config.reset_cache()
    yield repo / ".shipyard" / "config.json"
    sy_config.reset_cache()


def _reresolve(layer: Path, values: dict | None) -> dict[str, int]:
    """Write (or skip writing) a config layer, drop both caches, and resolve the limits again."""
    from sy_tools import config as sy_config

    if values is not None:
        layer.write_text(json.dumps(values), encoding="utf-8")
    sy_config.reset_cache()
    usage._RENDER_LIMITS = None
    return usage.render_limits()


def test_render_limits_fall_back_to_the_shipped_defaults_without_an_override(config_layers):
    assert _reresolve(config_layers, None) == usage._DEFAULT_RENDER_LIMITS, (
        "no override must fall back to shipped defaults"
    )


def test_a_config_override_reaches_render_limits_leaf_by_leaf(config_layers):
    """`_flatten()` only stores leaf keys, so a naive whole-object `get()` would silently be swallowed."""
    overridden = _reresolve(config_layers, {"transcript": {"truncation_limits": {"tool_result": 99}}})
    assert overridden["tool_result"] == 99, "a config override must actually change render_limits()"
    assert overridden["tool_input"] == usage._DEFAULT_RENDER_LIMITS["tool_input"], (
        "an unset sibling keeps its default"
    )


def test_an_unresolvable_config_falls_back_instead_of_crashing_the_render(tmp_path, monkeypatch):
    """A render usually happens late in a session, so a broken config must cost the override, not the run."""
    from sy_tools import config as sy_config

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))  # exists, but is no checkout
    monkeypatch.setattr(usage, "_RENDER_LIMITS", None)
    sy_config.reset_cache()
    try:
        # Refused first, so the degradation is pinned against the exception actually thrown rather
        # than passing vacuously because nothing was raised.
        with pytest.raises(sy_config.ConfigError):
            sy_config.get("transcript.truncation_limits.tool_result")
        assert usage.render_limits() == usage._DEFAULT_RENDER_LIMITS, (
            "an unresolvable config must fall back to shipped defaults, not crash the render"
        )
    finally:
        sy_config.reset_cache()


def test_a_non_numeric_configured_limit_falls_back_instead_of_crashing_the_render(config_layers):
    """`get()` does not itself enforce the schema (only `validate` does).

    A hand-edited layer bypassing `validate` can still reach here with a non-numeric value; `int(...)`
    must not crash the render, same as an unresolvable config.
    """
    limits = _reresolve(config_layers, {"transcript": {"truncation_limits": {"tool_result": "not-a-number"}}})
    assert limits == usage._DEFAULT_RENDER_LIMITS, (
        "a non-numeric resolved value must fall back to shipped defaults, not crash the render"
    )


def _refusal_records(tool_use_id: str, payload: object = None) -> list[dict]:
    return [
        {
            "type": "assistant",
            "message": {
                "id": "h1",
                "content": [{"type": "tool_use", "id": tool_use_id, "name": "SubagentHandback", "input": {}}],
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": '{"success":false,"reason":"already delivered"}' if payload is None else payload,
                    }
                ]
            },
        },
    ]


def test_handbacks_counts_a_replayed_refusal_once(tmp_path, monkeypatch):
    """A verbatim-replayed transcript span is one refusal, not two; the reader must dedup by tool_use_id."""
    monkeypatch.setattr(usage, "LEDGER_ROOT", tmp_path / "ledger")
    main = tmp_path / "s1.jsonl"
    records = _refusal_records("call-1") * 2
    main.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    result = usage.handbacks(main)
    assert result["refused"] == 1, result


def test_handbacks_reports_an_unreadable_subagent_tree_instead_of_a_clean_zero(tmp_path, monkeypatch):
    """An unreadable subagent dir must name itself in `warnings`; a bare zero reads as health."""
    monkeypatch.setattr(usage, "LEDGER_ROOT", tmp_path / "ledger")
    main = tmp_path / "s1.jsonl"
    main.write_text("", encoding="utf-8")
    subdir = tmp_path / "s1" / "subagents"
    subdir.mkdir(parents=True)
    (subdir / "agent-a.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in _refusal_records("call-1")), encoding="utf-8"
    )

    # Injected rather than chmod'd: root ignores the permissions that would otherwise make the tree unreadable.
    def denied_walk(top, onerror=None, **kwargs):
        assert onerror is not None
        onerror(PermissionError(13, "Permission denied", str(top)))
        return iter(())

    monkeypatch.setattr(usage.os, "walk", denied_walk)
    result = usage.handbacks(main)

    assert result["refused"] == 0, result
    assert any(str(tmp_path / "s1") in warning for warning in result["warnings"]), result


@pytest.mark.parametrize("payload", [
    '{"reason": "already delivered", "success": false}',
    '{\n  "success": false,\n  "reason": "already delivered"\n}',
    [{"type": "text", "text": '{"success": false, "reason": "already delivered"}'}],
])
def test_handbacks_counts_a_refusal_however_its_payload_is_serialized(tmp_path, monkeypatch, payload):
    """Spacing, key order and content-block wrapping are all valid JSON; only a substring match cares."""
    monkeypatch.setattr(usage, "LEDGER_ROOT", tmp_path / "ledger")
    main = tmp_path / "s1.jsonl"
    records = _refusal_records("call-1", payload)
    main.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    assert usage.handbacks(main)["refused"] == 1, payload


def test_handbacks_does_not_count_a_delivered_handback_as_refused(tmp_path, monkeypatch):
    """The other half of parsing the payload: `success: true` must stay uncounted."""
    monkeypatch.setattr(usage, "LEDGER_ROOT", tmp_path / "ledger")
    main = tmp_path / "s1.jsonl"
    records = _refusal_records("call-1", '{"success": true, "note": "success:false is not the verdict"}')
    main.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    assert usage.handbacks(main)["refused"] == 0


def test_an_unreadable_legacy_layout_transcript_warns_instead_of_vanishing(tmp_path, monkeypatch):
    """The legacy sibling layout drops a file that claims no session; an unreadable one must say so."""
    monkeypatch.setattr(usage, "LEDGER_ROOT", tmp_path / "ledger")
    main = tmp_path / "s1.jsonl"
    main.write_text("", encoding="utf-8")
    legacy = tmp_path / "subagents"
    legacy.mkdir()
    unreadable = legacy / "agent-b.jsonl"
    unreadable.write_text("", encoding="utf-8")

    # Injected rather than chmod'd: root ignores the permissions that would otherwise make the file unreadable.
    target = unreadable.resolve()
    real_open = Path.open

    def denied_open(self, *args, **kwargs):
        if self.resolve() == target:
            raise PermissionError(13, "Permission denied", str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied_open)
    result = usage.handbacks(main)

    assert any("agent-b.jsonl" in warning for warning in result["warnings"]), result


def test_handbacks_keeps_a_row_per_transcript_when_no_agent_id_surfaces(tmp_path, monkeypatch):
    """Same-typed transcripts with no agent id must not merge: one row's `transcript` hides the rest."""
    monkeypatch.setattr(usage, "LEDGER_ROOT", tmp_path / "ledger")
    main = tmp_path / "s1.jsonl"
    main.write_text("", encoding="utf-8")
    subdir = tmp_path / "s1" / "subagents"
    subdir.mkdir(parents=True)
    first = subdir / "agent-a.jsonl"
    second = subdir / "agent-b.jsonl"
    first.write_text(
        "".join(json.dumps(r) + "\n" for r in [{"agent_type": "sy:slice"}, *_refusal_records("call-1")]),
        encoding="utf-8",
    )
    second.write_text(
        "".join(
            json.dumps(r) + "\n"
            for r in [{"agent_type": "sy:slice"}, *_refusal_records("call-2"), *_refusal_records("call-3")]
        ),
        encoding="utf-8",
    )

    result = usage.handbacks(main)

    assert result["refused"] == 3, result
    rows = {row["transcript"]: row for row in result["by_agent"]}
    assert set(rows) == {str(first.resolve()), str(second.resolve())}, result
    assert rows[str(first.resolve())]["refused"] == 1, result
    assert rows[str(second.resolve())]["refused"] == 2, result
    assert all(row["agent_type"] == "slice" and row["agent_id"] == "" for row in result["by_agent"]), result


def test_handbacks_still_merges_two_transcripts_of_one_agent_id(tmp_path, monkeypatch):
    """The common case is unchanged: one agent's id groups its transcripts onto a single row."""
    monkeypatch.setattr(usage, "LEDGER_ROOT", tmp_path / "ledger")
    main = tmp_path / "s1.jsonl"
    main.write_text("", encoding="utf-8")
    subdir = tmp_path / "s1" / "subagents"
    subdir.mkdir(parents=True)
    for name, call in (("agent-a.jsonl", "call-1"), ("agent-b.jsonl", "call-2")):
        (subdir / name).write_text(
            "".join(
                json.dumps(r) + "\n"
                for r in [{"agent_type": "sy:slice", "agent_id": "ag-1"}, *_refusal_records(call)]
            ),
            encoding="utf-8",
        )

    result = usage.handbacks(main)

    assert result["by_agent"] == [
        {
            "agent_type": "slice",
            "agent_id": "ag-1",
            "refused": 2,
            "transcript": str((subdir / "agent-a.jsonl").resolve()),
        }
    ], result


def test_a_line_of_invalid_utf8_warns_instead_of_killing_the_read(tmp_path, monkeypatch):
    """Text-mode iteration raised `UnicodeDecodeError` out of `handbacks`; only that line may be lost."""
    monkeypatch.setattr(usage, "LEDGER_ROOT", tmp_path / "ledger")
    main = tmp_path / "s1.jsonl"
    records = _refusal_records("call-1")
    main.write_bytes(
        json.dumps(records[0]).encode("utf-8")
        + b'\n{"type": "assistant", "note": "\xff\xfe not utf-8"}\n'
        + json.dumps(records[1]).encode("utf-8")
        + b"\n"
    )

    result = usage.handbacks(main)

    assert result["refused"] == 1, result
    assert result["warnings"] == ["decode_error:s1.jsonl:2"], result


def test_summarize_warns_instead_of_raising_on_a_line_of_invalid_utf8(tmp_path, monkeypatch):
    """`summarize()`'s own doc promises this never fails; its token-counting path is a separate read
    from `handbacks()`'s and regressed independently of it."""
    monkeypatch.setattr(usage, "LEDGER_ROOT", tmp_path / "ledger")
    main = tmp_path / "s1.jsonl"
    usage_record = {"type": "assistant", "message": {"model": "claude", "usage": {"input_tokens": 3}}}
    main.write_bytes(
        json.dumps(usage_record).encode("utf-8")
        + b'\n{"type": "assistant", "note": "\xff\xfe not utf-8"}\n'
        + json.dumps(usage_record).encode("utf-8")
        + b"\n"
    )

    result = usage.summarize(main, phase="ship", task="PROJ-1")

    assert result["totals"]["input_tokens"] == 6, result
    assert result["warnings"] == ["decode_error:s1.jsonl:2"], result
