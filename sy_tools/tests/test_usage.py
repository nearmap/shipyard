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
