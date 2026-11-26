"""PR outcome rows from hand-written GraphQL responses; expected values are worked out by hand from the fixtures."""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from typing import Any

from sy_tools import pr_outcomes

FIXTURES = Path(__file__).parent / "fixtures" / "pr_outcomes"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _fake_runner(calls: list[dict[str, Any]]) -> pr_outcomes.Runner:
    def run(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        calls.append(variables)
        if query is pr_outcomes.REVERT_QUERY:
            return _load("reverts.json")
        return _load("prs_page2.json" if variables["after"] == "cursor-1" else "prs_page1.json")

    return run


def _rows() -> dict[int, dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    rows = pr_outcomes.fetch("acme/widget", date(2026, 9, 1), "dev-one", _fake_runner(calls))
    assert [c["after"] for c in calls] == [None, "cursor-1", None], "PR search must page once, reverts once"
    assert "author:dev-one" in calls[0]["q"] and "created:>=2026-09-01" in calls[0]["q"]
    return {r["number"]: r for r in rows}


def test_merged_pr_row_lifecycle_review_and_ci():
    row = _rows()[7]
    assert row["hours_to_merge"] == 10.0
    assert row["commit_shas"][0].startswith("c1c1") and len(row["commit_shas"]) == 3
    assert row["merge_commit_sha"].startswith("f00d")
    assert row["review_rounds"] == 2, "c1 and c3 got non-author reviews; the self-review and pending one do not count"
    assert row["reviews"] == {"COMMENTED": 2, "APPROVED": 1}
    assert row["ci_failed_commits"] == 2, "FAILURE and ERROR rollups count, SUCCESS does not"
    assert row["ci_failed_checks"] == {"lint": 2, "ci/legacy": 1}
    assert row["session_ids"] == ["sess-aaa", "sess-bbb"], "body trailer first, then the comment marker"
    assert row["truncated"] == []


def test_comment_author_kinds():
    by_id = {c["id"]: c for c in _rows()[7]["comments"]}
    assert set(by_id) == {"I1", "I2", "T1", "R2"}, "empty review bodies are not comments"
    agent = by_id["I1"]
    assert (agent["author_kind"], agent["session_id"], agent["kind"]) == ("agent", "sess-bbb", "issue")
    assert by_id["I2"]["author_kind"] == "bot", "a [bot] login is a bot whatever its actor type"
    assert (by_id["R2"]["author_kind"], by_id["R2"]["kind"]) == ("bot", "review")
    assert (by_id["T1"]["author_kind"], by_id["T1"]["path"], by_id["T1"]["kind"]) == ("person", "src/size.py", "review")


def test_reverts_and_follow_ups():
    rows = _rows()
    assert rows[7]["reverted_by"] == [8, 11], "title revert and merge-SHA revert; not #70, not pre-merge #14"
    assert rows[7]["followed_up_by"] == [9], "#10 references the URL but falls outside the 30-day window"
    assert (rows[12]["reverted_by"], rows[12]["followed_up_by"], rows[12]["hours_to_merge"]) == ([], [], None)


def test_unmerged_pr_with_truncated_commits():
    row = _rows()[12]
    assert row["truncated"] == ["commits"], "101 commits with one fetched must be flagged, not silently undercounted"
    assert (row["session_ids"], row["ci_failed_commits"], row["merge_commit_sha"]) == ([], 0, None)


def test_long_bodies_are_truncated():
    comment = pr_outcomes.classify_comment(
        {"id": "X", "createdAt": "2026-10-01T00:00:00Z", "body": "x" * 5000, "author": None}, "issue"
    )
    assert (len(comment["body"]), comment["author"], comment["author_kind"]) == (2000, None, "person")
