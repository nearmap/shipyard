"""`/sy:retro` costing and analysis: cache classes priced apart, PR shares, and facets scored per goal."""

import json
from pathlib import Path
import zipfile

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
        "ended_at": "2026-11-04T10:00:00Z",
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
        "size": {"additions": 30, "deletions": 10},
        "outcome": {"state": "MERGED", "review_rounds": 3, "comments_by_kind": {"person": 2}, "rules_hit": {}},
    }
    disc = {
        "prs": [pr],
        "sessions": {"shipyard": {"level": "light"}},
        "unknown": ["analysis"],
        "window": {"since": "2026-11-01", "until": "2026-11-30"},
        "repos": ["o/r"],
        "patterns": [{"id": "P1-1", "goal": "P1", "pattern": "p", "evidence": ["a:L1"], "why": "because"}],
    }
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
    assert goals["P1"]["unplanned_prs"] == [] and goals["P1"]["median_changed_lines"]["planned"] == 40
    assert out["analysis"]["weekly"][0]["week"] == "2026-11-02", "bucketed by the Monday of the last activity"
    assert out["analysis"]["weekly"][0]["friction_per_session"] == {"review_rework": 0.5}
    (tmp_path / "prs.jsonl").write_text(json.dumps({"repo": "o/r", "number": 1, "title": "t"}) + "\n")
    retro.report(tmp_path)
    agents = (tmp_path / "analysis-agents.md").read_text()
    assert "- why: because" in agents and "| o/r#1 | 40 | 3 |" in agents, agents


def test_digest_drops_plain_tools_past_line_ten_thousand():
    lines = [f"L{10_000 + n} 10:00:00 TOOL Bash: {'x' * 500}" for n in range(100)]
    lines += ["L20000 10:00:00 USER: keep me", "L20001 10:00:00 TOOL Agent: keep me too"]
    digest = retro._digest(lines)
    assert "TOOL Bash" not in digest and "USER: keep me" in digest and "TOOL Agent" in digest, digest


def test_projects_roots_finds_config_dirs_outside_home_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for d in (".claude", "Work/.claude-config", "Work/.claude-ent", "elsewhere/cfg"):
        (tmp_path / d / "projects").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere/cfg"))
    roots = {p.relative_to(tmp_path).as_posix() for p in retro.projects_roots()}
    assert roots == {
        ".claude/projects",
        "Work/.claude-config/projects",
        "Work/.claude-ent/projects",
        "elsewhere/cfg/projects",
    }, roots


def test_package_zips_the_approved_files_under_the_persons_name(tmp_path: Path):
    for f in retro.SHARED_FILES:
        (tmp_path / f).write_text("x")
    disc = {"approval": {"status": "draft"}, "window": {"until": "2026-11-28"}}
    (tmp_path / "discovery.json").write_text(json.dumps(disc))
    with pytest.raises(AssertionError, match="approve"):
        retro.package(tmp_path, "Ada Lovelace")
    disc["approval"]["status"] = "approved"
    (tmp_path / "discovery.json").write_text(json.dumps(disc))
    path = retro.package(tmp_path, "Ada Lovelace")
    assert path.name == "retro-ada-lovelace-2026-11-28.zip"
    assert sorted(zipfile.ZipFile(path).namelist()) == sorted(f"ada-lovelace-{f}" for f in retro.SHARED_FILES)
