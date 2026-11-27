"""`/sy:retro` costing and analysis: cache classes priced apart, PR shares, and facets scored per goal."""

import json
from pathlib import Path

import pytest

from sy_tools import retro

COUNTS = {
    "input_tokens": 1_000_000,
    "output_tokens": 1_000_000,
    "cache_read_input_tokens": 10_000_000,
    "cache_creation_input_tokens": 3_000_000,
    "cache_write_5m_input_tokens": 1_000_000,
    "cache_write_1h_input_tokens": 1_000_000,
}


def _row(sid: str, turns: int) -> dict:
    return {
        "session_id": sid,
        "skills": {"slash": {"sy:spec": 1} if sid == "a" else {}, "tool": {}},
        "interventions": {"user_turns": turns, "ask_user_question": 1},
        "by_skill": {"sy:spec": {"user_turns": turns}},
        "tokens": {"main": {"claude-opus-5-5[1m]": COUNTS}, "subagents": {}, "by_skill": {}, "by_agent_type": {}},
        "cache": {"peak_context": 100, "gt_5m": {"gaps": 1, "write_tokens": {"claude-opus-5-5": 1_000_000}}},
        "compactions": 0,
    }


def test_cost_prices_reads_and_each_write_ttl_apart():
    cost = retro._cost("claude-opus-5-5[1m]", COUNTS, retro._prices(), set())
    assert cost == pytest.approx(
        {"input": 4.0, "output": 20.0, "cache_read": 2.0, "cache_write_5m": 2 * 5.0, "cache_write_1h": 8.0}
    ), "the unsplit million is priced as a 5-minute write, the longest-prefix model id wins"
    unpriced: set[str] = set()
    assert retro._cost("<synthetic>", COUNTS, retro._prices(), unpriced) == {} and unpriced == {"<synthetic>"}


def test_a_session_linked_to_two_prs_gives_each_half():
    share = retro._split_share([_row("a", 2)], {"a": 2}, retro._prices())
    assert share["usd_estimate"] == pytest.approx(44.0 / 2) and share["input_tokens"] == 500_000


def test_analyze_folds_facets_into_goal_sections(tmp_path: Path):
    (tmp_path / "sessions.jsonl").write_text("".join(json.dumps(_row(s, n)) + "\n" for s, n in (("a", 4), ("b", 2))))
    pr = {
        "ref": "o/r#1",
        "sessions": ["a", "b"],
        "approach_marker": None,
        "approach": "unknown",
        "tokens": None,
        "outcome": {"state": "MERGED", "review_rounds": 3, "comments_by_kind": {"person": 2}, "rules_hit": {}},
    }
    disc = {"prs": [pr], "sessions": {"shipyard": {"level": "light"}}, "unknown": ["analysis"]}
    (tmp_path / "discovery.json").write_text(json.dumps(disc))
    (tmp_path / "facets").mkdir()
    (tmp_path / "facets" / "batch-001.json").write_text("[]")
    with pytest.raises(AssertionError, match=r"facets-001\.json missing"):
        retro.analyze(tmp_path)
    facets = [
        {
            "session_id": "a",
            "approach": "planned",
            "outcome": "fully_achieved",
            "human_turns": {"factual": 3, "value_judgement": 1},
            "friction": {"review_rework": 1},
        },
        {"session_id": "b", "approach": "planned", "outcome": "not_achieved", "human_turns": {"approval": 2}},
    ]
    (tmp_path / "facets" / "facets-001.json").write_text(json.dumps(facets))
    retro.analyze(tmp_path)
    out = json.loads((tmp_path / "discovery.json").read_text())
    goals = out["analysis"]["goals"]
    assert goals["P4"]["factual_share"] == 0.5, goals["P4"]
    assert goals["P1"]["merged_prs_with_a_plan"] == "1/1", "session a ran sy:spec"
    assert goals["P5"]["median_human_turns_per_pr"] == 6
    assert goals["T1"]["prs_reworked_after_review"] == ["o/r#1"]
    assert goals["T3"]["idle_rewrite_usd"]["gt_5m"] == pytest.approx(2 * 4.0 * 1.5)
    assert out["prs"][0]["approach"] == "planned" and out["unknown"] == []
    assert len((tmp_path / "facets.jsonl").read_text().splitlines()) == 2
