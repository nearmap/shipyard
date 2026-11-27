"""`sessions.collect` over the hand-written session tree in `fixtures/sessions/`; expected values are read off it."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sy_tools import sessions

FIXTURES = Path(__file__).parent / "fixtures" / "sessions"


def test_row_folds_subagents_and_extracts_links_and_interventions():
    rows = sessions.collect(FIXTURES, since=datetime(2026, 1, 1, tzinfo=UTC))
    assert [r["session_id"] for r in rows] == ["s1"], "s0 ended before `since` and must be skipped"
    row = rows[0]
    assert (row["started_at"], row["ended_at"], row["active_minutes"]) == (
        "2026-01-05T10:00:00Z",
        "2026-01-05T11:01:00Z",
        11.0,
    ), "the 50-minute idle gap before the last turn is not active time"
    assert row["links"] == {
        "branches": ["ABC-12/widgets", "DEF-3/side"],
        "commits": ["1a2b3c4", "9f8e7d6"],
        "pr_urls_created": ["https://git.example.com/example/widgets/pull/7"],
        "pr_urls_mentioned": ["https://git.example.com/example/widgets/pull/5"],
        "pr_urls_linked": ["https://git.example.com/example/widgets/pull/7"],
        "issue_keys": ["ABC-12", "DEF-3"],
    }
    assert row["interventions"] == {
        "user_turns": 2,
        "interrupts": 1,
        "ask_user_question": 1,
        "ask_answers": 1,
        "permission_denials": 1,
    }, "the blob's echoed AskUserQuestion, command wrappers, meta, peer and compact records are not human turns"
    assert row["compactions"] == 1
    assert row["warnings"] == 1
    assert row["skills"] == {"slash": {"sy:ship": 1}, "tool": {"sy:standards": 1}, "tool_nested": {}}
    assert row["by_skill"] == {
        "none": {"user_turns": 1},
        "sy:ship": {"asks": 1, "tool_errors": 1, "permission_denials": 1, "interrupts": 1, "user_turns": 1},
    }, "interventions go to the skill the person started, and sy:standards inside it does not take over"
    assert row["tool_error_classes"] == {"user_denied": 1, "not_found": 1}
    assert row["agents"] == {
        "dispatched": {"sy:hunt": 1},
        "spawned": {"sy:debater": 1, "sy:hunt": 1},
        "max_spawn_depth": 2,
    }
    assert row["tool_errors"] == {"Edit": 1, "Read": 1}
    assert row["usage"]["totals"]["input_tokens"] == 312
    assert {m: t["output_tokens"] for m, t in row["usage"]["by_model"].items()} == {"model-big": 103, "model-small": 7}
    tokens = row["tokens"]
    assert tokens["main"]["model-big"]["cache_write_1h_input_tokens"] == 900
    assert tokens["subagents"]["model-small"]["input_tokens"] == 11
    assert tokens["by_skill"]["sy:ship"]["model-small"]["input_tokens"] == 11, (
        "the nested debater's tokens reach sy:ship through its parent's dispatch"
    )
    assert set(tokens["by_agent_type"]) == {"sy:hunt", "sy:debater"}
    assert row["cache"] == {
        "peak_context": 901,
        "gt_5m": {"gaps": 1, "write_tokens": {"model-big": 900}},
        "gt_1h": {"gaps": 0, "write_tokens": {}},
    }, "the reply after the 55-minute gap rewrote the cache"
