"""The live-sessions join asks the adapter whether two references name one issue."""

from typing import Any

import pytest

from sy_tools import server, tracker
from sy_tools.tracker.github.adapter import GithubAdapter
from sy_tools.tracker.jira.adapter import JiraAdapter


@pytest.mark.anyio
@pytest.mark.parametrize("ticket", ["42", "#42", "https://github.com/o/r/issues/42"])
async def test_join_matches_one_github_issue_however_it_is_spelled(monkeypatch, ticket):
    adapter = GithubAdapter()
    url = "https://github.com/o/r/issues/42"
    issues = [{"id": url, "url": url, "title": "Joined", "status": "ready"}]

    async def find_issues(**kwargs: Any) -> dict:
        return {"issues": issues, "is_last": True}

    monkeypatch.setattr(adapter, "find_issues", find_issues)
    monkeypatch.setattr(tracker, "adapter", lambda: adapter)
    result: dict[str, Any] = {"sessions": [{"ticket": ticket}], "warnings": []}
    await server._join_issues(result, "7")
    assert result["sessions"][0]["issue"] == {"title": "Joined", "status": "ready"}, result
    assert result["issues_without_session"] == [], result


def test_both_adapters_answer_same_issue():
    assert GithubAdapter().same_issue("42", "#42") and not GithubAdapter().same_issue("42", "43")
    assert JiraAdapter().same_issue("PROJ-1", "PROJ-1") and not JiraAdapter().same_issue("PROJ-1", "PROJ-2")
