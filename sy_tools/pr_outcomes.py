"""PR outcomes for `/sy:retro` discovery: one row per pull request, read from the code host, never self-reported.

`fetch` pages a `gh api graphql` search of one repository; everything else is pure parsing over the GraphQL JSON
so it can be tested on recorded responses. Each row carries the link keys (branch, commit SHAs, merge SHA, session
IDs, URL) the session table joins on.

    python -m sy_tools.pr_outcomes --repo owner/name --since YYYY-MM-DD --out rows.jsonl
"""
from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

Runner = Callable[[str, dict[str, Any]], dict[str, Any]]
"""Runs one GraphQL query with its variables and returns the response's `data` object."""

PAGE_SIZE = 25
GATEWAY_TIMEOUT = re.compile(r"HTTP 50[24]\b|unexpected end of JSON input")
"""A page too heavy for the GraphQL budget fails with a gateway error; the pager halves the page and retries."""
BODY_LIMIT = 2000
FOLLOW_UP_WINDOW = timedelta(days=30)
FAILED_ROLLUP = frozenset({"FAILURE", "ERROR"})
FAILED_CHECK = frozenset({"FAILURE", "ERROR", "TIMED_OUT", "STARTUP_FAILURE"})

SESSION_TRAILER = re.compile(r"^Claude-Code-Session-Id:\s*(\S+)", re.M)
APPROACH_MARKER = re.compile(r"^Approach:\s*(spec|doit|mvp)\b", re.M)
AGENT_MARKER = re.compile(r"<!--\s*shipyard:agent session=([^\s>]+?)\s*-->")

_ACTOR = "author { __typename login }"
PR_QUERY = f"""
query($q: String!, $first: Int!, $after: String) {{
  search(query: $q, type: ISSUE, first: $first, after: $after) {{
    issueCount
    pageInfo {{ hasNextPage endCursor }}
    nodes {{
      ... on PullRequest {{
        number url title body state createdAt mergedAt closedAt headRefName baseRefName
        additions deletions changedFiles {_ACTOR}
        mergeCommit {{ oid }}
        commits(first: 100) {{
          totalCount
          nodes {{ commit {{ id oid statusCheckRollup {{ state }} }} }}
        }}
        reviews(first: 100) {{ totalCount nodes {{ id state createdAt body {_ACTOR} commit {{ oid }} }} }}
        reviewThreads(first: 50) {{ totalCount nodes {{ comments(first: 20) {{ totalCount nodes {{
          id createdAt path body {_ACTOR}
        }} }} }} }}
        comments(first: 100) {{ totalCount nodes {{ id createdAt body {_ACTOR} }} }}
      }}
    }}
  }}
}}
"""

FAILED_CHECKS_QUERY = """
query($ids: [ID!]!) {
  nodes(ids: $ids) { ... on Commit {
    id
    checkSuites(first: 50) { totalCount nodes {
      checkRuns(first: 50, filterBy: {conclusions: [FAILURE, TIMED_OUT, STARTUP_FAILURE]}) {
        totalCount nodes { name conclusion }
      }
    } }
    status { contexts { context state } }
  } }
}
"""
"""Failed checks of failed commits only: a busy CI posts hundreds of checks per commit, too many for the search."""
FAILED_CHECKS_BATCH = 50

REVERT_QUERY = """
query($q: String!, $first: Int!, $after: String) {
  search(query: $q, type: ISSUE, first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest { number url title body createdAt } }
  }
}
"""


def fetch(repo: str, since: date, author: str = "@me", runner: Runner | None = None) -> list[dict[str, Any]]:
    """Every PR in `repo` (`owner/name`) by `author` created on or after `since`, any state, as outcome rows.

    Reverts are also looked for among PRs by anyone (one extra search); follow-ups only among `author`'s own PRs.
    A failed `gh` call raises with its stderr.
    """
    run = runner or run_graphql
    base = f"repo:{repo} is:pr created:>={since.isoformat()} sort:created-asc"
    prs = _search_all(run, PR_QUERY, f"{base} author:{author}")
    _attach_failed_checks(run, prs)
    reverts = _search_all(run, REVERT_QUERY, f"{base} revert")
    return rows_from_prs(repo, prs, reverts)


def rows_from_prs(
    repo: str, prs: list[dict[str, Any]], extra_candidates: Iterable[dict[str, Any]] = ()
) -> list[dict[str, Any]]:
    """Outcome rows for GraphQL `PullRequest` nodes, with reverts and follow-ups found among them and the extras."""
    rows = [parse_pr(repo, pr) for pr in prs]
    candidates = {c["number"]: c for c in [*extra_candidates, *prs]}
    for row in rows:
        row["reverted_by"], row["followed_up_by"] = follow_ups(row, candidates.values())
    return rows


def parse_pr(repo: str, pr: dict[str, Any]) -> dict[str, Any]:
    """One `PullRequest` node as a row, without the follow-up fields that need the other PRs."""
    author = _login(pr)
    comments = [classify_comment(c, "issue") for c in pr["comments"]["nodes"]]
    comments += [
        classify_comment(c, "review") for t in pr["reviewThreads"]["nodes"] for c in t["comments"]["nodes"]
    ]
    reviews = [r for r in pr["reviews"]["nodes"] if r["state"] != "PENDING"]
    comments += [classify_comment(r, "review") for r in reviews if r["body"].strip()]
    others = [r for r in reviews if _login(r) != author]
    commits = [n["commit"] for n in pr["commits"]["nodes"]]
    failed = [c for c in commits if (c["statusCheckRollup"] or {}).get("state") in FAILED_ROLLUP]
    session_ids = SESSION_TRAILER.findall(pr["body"] or "") + [c["session_id"] for c in comments if c["session_id"]]
    return {
        "repo": repo,
        "number": pr["number"],
        "url": pr["url"],
        "title": pr["title"],
        "author": author,
        "head_ref": pr["headRefName"],
        "base_ref": pr["baseRefName"],
        "commit_shas": [c["oid"] for c in commits],
        "merge_commit_sha": (pr["mergeCommit"] or {}).get("oid"),
        "session_ids": list(dict.fromkeys(session_ids)),
        "approach_marker": next(iter(APPROACH_MARKER.findall(pr["body"] or "")), None),
        "state": pr["state"],
        "created_at": pr["createdAt"],
        "merged_at": pr["mergedAt"],
        "closed_at": pr["closedAt"],
        "hours_to_merge": _hours(pr["createdAt"], pr["mergedAt"]),
        "additions": pr["additions"],
        "deletions": pr["deletions"],
        "changed_files": pr["changedFiles"],
        "review_rounds": len({r["commit"]["oid"] for r in others if r["commit"]}),
        "reviews": dict(Counter(r["state"] for r in others)),
        "comments": sorted(comments, key=lambda c: c["created_at"]),
        "ci_failed_commits": len(failed),
        "ci_failed_checks": dict(Counter(name for c in failed for name in _failed_check_names(c))),
        "truncated": _truncated(pr),
    }


def classify_comment(node: dict[str, Any], kind: str) -> dict[str, Any]:
    """A comment, review-thread comment or review body as `{id, kind, author, author_kind, ...}`.

    `author_kind` is `bot` for a Bot actor or a `[bot]` login, `agent` for a body carrying the shipyard agent
    marker (its session in `session_id`), else `person`.
    """
    actor = node.get("author") or {}
    login = actor.get("login")
    body = node.get("body") or ""
    marker = AGENT_MARKER.search(body)
    if actor.get("__typename") == "Bot" or (login or "").endswith("[bot]"):
        author_kind = "bot"
    elif marker:
        author_kind = "agent"
    else:
        author_kind = "person"
    return {
        "id": node["id"],
        "kind": kind,
        "author": login,
        "author_kind": author_kind,
        "session_id": marker.group(1) if marker else None,
        "path": node.get("path"),
        "created_at": node["createdAt"],
        "body": body[:BODY_LIMIT],
    }


def follow_ups(row: dict[str, Any], candidates: Iterable[dict[str, Any]]) -> tuple[list[int], list[int]]:
    """`(reverted_by, followed_up_by)`: PR numbers among `candidates` created after `row` merged.

    A revert's title starts `Revert "<title>`, or its body says `This reverts commit <merge sha>`, or it names
    `#<number>` beside the word revert. A follow-up references `#<number>` or the PR URL within 30 days of merge.
    """
    if not row["merged_at"]:
        return [], []
    merged = _ts(row["merged_at"])
    ref = re.compile(rf"(?:(?<![\w/])|{re.escape(row['repo'])})#{row['number']}\b|{re.escape(row['url'])}(?!\d)")
    reverted, followed = [], []
    for c in sorted(candidates, key=lambda c: c["number"]):
        created = _ts(c["createdAt"])
        if c["number"] == row["number"] or created <= merged:
            continue
        text = f"{c['title']}\n{c['body'] or ''}"
        sha = row["merge_commit_sha"]
        if (
            c["title"].startswith(f'Revert "{row["title"]}')
            or (sha and f"This reverts commit {sha}" in text)
            or (ref.search(text) and re.search(r"\brevert", text, re.I))
        ):
            reverted.append(c["number"])
        elif created - merged <= FOLLOW_UP_WINDOW and ref.search(text):
            followed.append(c["number"])
    return reverted, followed


def run_graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    """Run one query through `gh api graphql` and return its `data`; raise with stderr on any failure."""
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        if isinstance(value, list):
            args += [a for v in value for a in ("-f", f"{key}[]={v}")]
        elif value is not None:
            args += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
    proc = subprocess.run(args, capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise RuntimeError(f"gh api graphql failed ({proc.returncode}): {proc.stderr.strip()}")
    payload = json.loads(proc.stdout)
    if payload.get("errors"):
        raise RuntimeError(f"gh api graphql returned errors: {payload['errors']}")
    return payload["data"]


def main(argv: list[str] | None = None) -> None:
    """CLI: write one JSON row per PR to `--out` and print a one-line summary."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--since", required=True, type=date.fromisoformat, help="YYYY-MM-DD, inclusive")
    parser.add_argument("--author", default="@me")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    calls = 0

    def counted(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return run_graphql(query, variables)

    start = time.monotonic()
    rows = fetch(args.repo, args.since, args.author, counted)
    args.out.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    merged = sum(1 for r in rows if r["merged_at"])
    print(
        f"{len(rows)} PRs ({merged} merged) from {args.repo} since {args.since} in {calls} calls, "
        f"{time.monotonic() - start:.1f}s -> {args.out}"
    )


def _search_all(run: Runner, query: str, q: str) -> list[dict[str, Any]]:
    """Every node of a paginated search, skipping the non-PR nodes an ISSUE search can return."""
    nodes: list[dict[str, Any]] = []
    after = None
    first = PAGE_SIZE
    while True:
        first, data = _shrinking(first, lambda n, a=after: run(query, {"q": q, "first": n, "after": a}), repr(q))
        page = data["search"]
        nodes += [n for n in page["nodes"] if n]
        if not page["pageInfo"]["hasNextPage"]:
            return nodes
        after = page["pageInfo"]["endCursor"]


def _attach_failed_checks(run: Runner, prs: list[dict[str, Any]]) -> None:
    """Set `statusCheckRollup.contexts` in place, to the failed checks only, on every commit whose rollup failed."""
    failed: dict[str, list[dict[str, Any]]] = {}
    for pr in prs:
        for n in pr["commits"]["nodes"]:
            if (rollup := n["commit"]["statusCheckRollup"]) and rollup["state"] in FAILED_ROLLUP:
                failed.setdefault(n["commit"]["id"], []).append(rollup)
    ids, i, size = list(failed), 0, FAILED_CHECKS_BATCH
    while i < len(ids):
        size, data = _shrinking(size, lambda n, s=i: run(FAILED_CHECKS_QUERY, {"ids": ids[s : s + n]}), "failed checks")
        for node in data["nodes"]:
            suites = node["checkSuites"]
            runs = [r for s in suites["nodes"] for r in s["checkRuns"]["nodes"]]
            statuses = [s for s in (node["status"] or {}).get("contexts", []) if s["state"] in FAILED_CHECK]
            cut = suites["totalCount"] > len(suites["nodes"]) or any(
                s["checkRuns"]["totalCount"] > len(s["checkRuns"]["nodes"]) for s in suites["nodes"]
            )
            nodes = runs + statuses
            for rollup in failed[node["id"]]:
                rollup["contexts"] = {"totalCount": len(nodes) + cut, "nodes": nodes}
        i += size


def _shrinking(size: int, call: Callable[[int], dict[str, Any]], what: str) -> tuple[int, dict[str, Any]]:
    """`call(size)`, halving `size` and retrying on a gateway error; returns the size that worked and its result."""
    while True:
        try:
            return size, call(size)
        except RuntimeError as e:
            if size == 1 or not GATEWAY_TIMEOUT.search(str(e)):
                raise
            size //= 2
            print(f"retrying {what} at size {size}: {e}", file=sys.stderr)


def _failed_check_names(commit: dict[str, Any]) -> list[str]:
    contexts = commit["statusCheckRollup"]["contexts"]["nodes"]
    return [
        c.get("name") or c.get("context")
        for c in contexts
        if (c.get("conclusion") or c.get("state")) in FAILED_CHECK
    ]


def _truncated(pr: dict[str, Any]) -> list[str]:
    """Connections whose first page did not hold everything; the row undercounts them."""
    out = [k for k in ("commits", "reviews", "reviewThreads", "comments") if pr[k]["totalCount"] > len(pr[k]["nodes"])]
    if any(t["comments"]["totalCount"] > len(t["comments"]["nodes"]) for t in pr["reviewThreads"]["nodes"]):
        out.append("reviewThreads.comments")
    if any(
        (ctx := (n["commit"]["statusCheckRollup"] or {}).get("contexts")) and ctx["totalCount"] > len(ctx["nodes"])
        for n in pr["commits"]["nodes"]
    ):
        out.append("commits.contexts")
    return out


def _login(node: dict[str, Any]) -> str | None:
    return (node.get("author") or {}).get("login")


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _hours(start: str, end: str | None) -> float | None:
    return None if end is None else round((_ts(end) - _ts(start)).total_seconds() / 3600, 2)


if __name__ == "__main__":
    sys.exit(main())
