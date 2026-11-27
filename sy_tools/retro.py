"""`/sy:retro` discovery: join the session table to PR outcomes, flag sessions worth reading, and draft discovery.json.

`python -m sy_tools.retro discover --since D --out DIR` builds the session table and lists candidate repositories;
adding `--repo owner/name ...` fetches their PR outcomes, links them to sessions, ranks flags, renders flagged spans,
writes comment batches, and drafts `discovery.json`. `fold-tags --out DIR` folds `sy:comment-tag` results back in.
"""

import argparse
from collections import Counter
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from . import pr_outcomes, sessions

SESSION_BUDGET = 12
COMMENT_BATCH = 50
SPAN_CONTEXT = 12
SPAN_MAX_LINES = 400
_PR_URL = re.compile(r"https://[^/\s]+/([^/\s]+/[^/\s]+)/pull/(\d+)")
_DEFAULT_BRANCHES = {"main", "master", "HEAD", ""}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover")
    d.add_argument("--since", type=date.fromisoformat, required=True)
    d.add_argument("--out", type=Path, required=True)
    d.add_argument("--repo", nargs="*", default=[])
    d.add_argument("--projects", type=Path, default=Path.home() / ".claude" / "projects")
    f = sub.add_parser("fold-tags")
    f.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.cmd == "fold-tags":
        print(json.dumps(fold_tags(args.out)))
        return 0
    args.out.mkdir(parents=True, exist_ok=True)
    rows = _session_table(args.projects, args.since, args.out)
    if not args.repo:
        print(json.dumps({"sessions": len(rows), "repos": candidate_repos(rows)}, indent=1))
        return 0
    print(json.dumps(discover(rows, args.repo, args.since, args.out, args.projects)))
    return 0


def candidate_repos(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Repositories the sessions touched, by how many sessions touched each, busiest first."""
    count: Counter = Counter()
    for r in rows:
        count.update(_session_repos(r))
    return [{"repo": k, "sessions": n} for k, n in count.most_common()]


def discover(rows: list[dict[str, Any]], repos: list[str], since: date, out: Path, projects: Path) -> dict[str, Any]:
    """Fetch PR outcomes, link, flag, render spans and comment batches, and draft discovery.json."""
    rows = [r for r in rows if _session_repos(r) & set(repos)]
    prs = [p for repo in repos for p in pr_outcomes.fetch(repo, since)]
    _write_jsonl(out / "prs.jsonl", prs)
    links = link(rows, prs)
    (out / "links.json").write_text(json.dumps(links, indent=1) + "\n", encoding="utf-8")
    flags = flag(rows, prs, links)
    for fl in flags:
        fl["span_files"] = render_spans(projects, fl, out / "spans")
    (out / "flags.json").write_text(json.dumps(flags, indent=1) + "\n", encoding="utf-8")
    batches = write_comment_batches(prs, out / "comments")
    draft = draft_discovery(rows, prs, links, repos, since)
    (out / "discovery.json").write_text(json.dumps(draft, indent=1) + "\n", encoding="utf-8")
    return {"sessions": len(rows), "prs": len(prs), "linked_prs": len(links), "flags": len(flags), "comment_batches": batches}


def link(rows: list[dict[str, Any]], prs: list[dict[str, Any]]) -> dict[str, dict[str, list[str]]]:
    """`owner/name#n` -> session id -> the kinds of evidence linking them."""
    by_url = {p["url"]: p for p in prs}
    by_branch: dict[str, list[dict[str, Any]]] = {}
    by_sha: dict[str, dict[str, Any]] = {}
    for p in prs:
        if p["head_ref"] not in _DEFAULT_BRANCHES:
            by_branch.setdefault(p["head_ref"], []).append(p)
        for sha in p.get("commit_shas") or []:
            by_sha[sha[:7]] = p
    out: dict[str, dict[str, list[str]]] = {}

    def add(p: dict[str, Any], sid: str, kind: str) -> None:
        kinds = out.setdefault(_ref(p), {}).setdefault(sid, [])
        if kind not in kinds:
            kinds.append(kind)

    for p in prs:
        for sid in p.get("session_ids") or []:
            add(p, sid, "session_trailer")
    for r in rows:
        sid, lk = r["session_id"], r["links"]
        for u in _urls(r):
            if p := by_url.get(u):
                add(p, sid, "pr_url")
        for b in lk.get("branches") or []:
            for p in by_branch.get(b, []):
                add(p, sid, "branch")
        for c in lk.get("commits") or []:
            if p := by_sha.get(c[:7]):
                add(p, sid, "commit")
    return out


def flag(
    rows: list[dict[str, Any]], prs: list[dict[str, Any]], links: dict[str, dict[str, list[str]]]
) -> list[dict[str, Any]]:
    """Sessions worth reading, ranked, capped at SESSION_BUDGET; each names why it was flagged."""
    reasons: dict[str, list[str]] = {}
    score: Counter = Counter()
    for r in rows:
        iv = r["interventions"]
        s = 3 * iv.get("interrupts", 0) + 2 * iv.get("permission_denials", 0) + iv.get("ask_answers", 0)
        s += 2 * r.get("compactions", 0) + sum(r.get("tool_errors", {}).values()) / 4
        if s:
            score[r["session_id"]] += s
            reasons.setdefault(r["session_id"], []).append(
                f"interventions: {iv.get('interrupts', 0)} interrupts, {iv.get('permission_denials', 0)} denials, "
                f"{iv.get('ask_answers', 0)} answers; {sum(r.get('tool_errors', {}).values())} tool errors"
            )
    for p in prs:
        why = []
        if (p.get("review_rounds") or 0) >= 3:
            why.append(f"{p['review_rounds']} review rounds")
        if p.get("reverted_by"):
            why.append("reverted")
        if (p.get("ci_failed_commits") or 0) >= 2:
            why.append(f"{p['ci_failed_commits']} commits failed CI")
        for sid in links.get(_ref(p), {}) if why else ():
            score[sid] += 5
            reasons.setdefault(sid, []).append(f"{_ref(p)}: {', '.join(why)}")
    by_id = {r["session_id"]: r for r in rows}
    ranked = [sid for sid, _ in score.most_common() if sid in by_id][:SESSION_BUDGET]
    return [
        {"session_id": sid, "score": round(score[sid], 1), "reasons": reasons[sid], "project_dir": by_id[sid]["project_dir"]}
        for sid in ranked
    ]


def render_spans(projects: Path, fl: dict[str, Any], spans_dir: Path) -> list[str]:
    """Cut one flagged session into compact text spans around its intervention events; return the file paths."""
    main = projects / fl["project_dir"] / f"{fl['session_id']}.jsonl"
    events, hits = _events(main)
    if not hits:
        hits = [events[0][0], events[-1][0]] if events else []
    keep = sorted({i for h in hits for i in range(h - SPAN_CONTEXT, h + SPAN_CONTEXT + 1)})
    lines = [text for n, text in events if n in keep][:SPAN_MAX_LINES]
    spans_dir.mkdir(parents=True, exist_ok=True)
    path = spans_dir / f"{fl['session_id']}.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return [str(path)]


def write_comment_batches(prs: list[dict[str, Any]], comments_dir: Path) -> int:
    """Review comments from anyone but the PR author, in batches for `sy:comment-tag`."""
    comments_dir.mkdir(parents=True, exist_ok=True)
    by_repo: dict[str, list[dict[str, Any]]] = {}
    for p in prs:
        for c in p.get("comments") or []:
            if c.get("author") != p.get("author"):
                by_repo.setdefault(p["repo"], []).append(
                    {"id": f"{_ref(p)}/{c['id']}", "path": c.get("path"), "body": c["body"], "author_kind": c["author_kind"]}
                )
    n = 0
    for repo, items in by_repo.items():
        for i in range(0, len(items), COMMENT_BATCH):
            n += 1
            name = f"batch-{repo.replace('/', '__')}-{i // COMMENT_BATCH + 1:03d}.json"
            (comments_dir / name).write_text(json.dumps(items[i : i + COMMENT_BATCH], indent=1) + "\n", encoding="utf-8")
    return n


def fold_tags(out: Path) -> dict[str, int]:
    """Count each PR's tagged rules from `comments/tags-*.json` into discovery.json's `rules_hit`."""
    disc_path = out / "discovery.json"
    disc = json.loads(disc_path.read_text(encoding="utf-8"))
    hits: dict[str, Counter] = {}
    tagged = 0
    for f in sorted((out / "comments").glob("tags-*.json")):
        for t in json.loads(f.read_text(encoding="utf-8")):
            ref = t["id"].rsplit("/", 1)[0]
            hits.setdefault(ref, Counter())[t["rule"]] += 1
            tagged += 1
    assert tagged, f"no comments/tags-*.json under {out}; run sy:comment-tag over every batch first"
    for p in disc["prs"]:
        p["outcome"]["rules_hit"] = dict(hits.get(p["ref"], {}))
    disc_path.write_text(json.dumps(disc, indent=1) + "\n", encoding="utf-8")
    return {"tagged": tagged, "prs_with_tags": len(hits)}


def draft_discovery(
    rows: list[dict[str, Any]], prs: list[dict[str, Any]], links: dict[str, Any], repos: list[str], since: date
) -> dict[str, Any]:
    """discovery.json with every measured field filled and every interview field `unknown`."""
    totals: dict[str, Counter] = {k: Counter() for k in ("skills", "agents", "interventions", "tool_errors")}
    for r in rows:
        totals["skills"].update({**(r["skills"].get("slash") or {}), **(r["skills"].get("tool") or {})})
        totals["agents"].update(r["agents"].get("spawned") or {})
        totals["interventions"].update(r.get("interventions") or {})
        totals["tool_errors"].update(r.get("tool_errors") or {})
    linked = {sid for by_sid in links.values() for sid in by_sid}
    tokens = {r["session_id"]: _tokens(r) for r in rows}
    pr_items = []
    for p in sorted(prs, key=lambda p: p["created_at"]):
        ref, sids = _ref(p), links.get(_ref(p), {})
        kinds = Counter(c["author_kind"] for c in p.get("comments") or [] if c.get("author") != p.get("author"))
        pr_items.append(
            {
                "ref": ref,
                "sessions": sorted(sids),
                "link_evidence": sorted({k for ks in sids.values() for k in ks}),
                "size": {k: p.get(k) for k in ("additions", "deletions", "changed_files")},
                "outcome": {
                    "state": p["state"],
                    "review_rounds": p.get("review_rounds", 0),
                    "comments_by_kind": dict(kinds),
                    "rules_hit": {},
                    "hours_to_merge": p.get("hours_to_merge"),
                    "ci_failed_commits": p.get("ci_failed_commits", 0),
                    "reverted": bool(p.get("reverted_by")),
                    "followed_up": bool(p.get("followed_up_by")),
                },
                "tokens": sum(tokens.get(s) or 0 for s in sids) or None,
                "approach_marker": p.get("approach_marker"),
                "approach": "unknown",
                "stages": "unknown",
            }
        )
    return {
        "schema": "shipyard.discovery.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window": {"since": since.isoformat(), "until": date.today().isoformat()},
        "repos": repos,
        "sessions": {
            "count": len(rows),
            "linked_to_prs": len(linked),
            "active_hours": round(sum(r.get("active_minutes") or 0 for r in rows) / 60, 1),
            **{k: dict(v.most_common()) for k, v in totals.items()},
        },
        "prs": pr_items,
        "shipyard_usage": {"tried": [], "skipped": []},
        "control": [],
        "pain_points": [],
        "extra_fields": {},
        "unknown": ["prs[].approach", "prs[].stages", "shipyard_usage", "control", "pain_points"],
        "approval": {"status": "draft", "approved_at": None, "redactions": 0},
    }


def _session_table(projects: Path, since: date, out: Path) -> list[dict[str, Any]]:
    cache = out / "sessions.jsonl"
    if cache.is_file():
        return [json.loads(line) for line in cache.read_text(encoding="utf-8").splitlines() if line]
    rows = sessions.collect(projects, datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc))
    _write_jsonl(cache, rows)
    return rows


def _events(main: Path) -> tuple[list[tuple[int, str]], list[int]]:
    events: list[tuple[int, str]] = []
    hits: list[int] = []
    for n, line in enumerate(main.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") not in ("user", "assistant") or not rec.get("timestamp") or rec.get("isMeta"):
            continue
        content = (rec.get("message") or {}).get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
        for b in blocks:
            text = _render_block(rec["type"], b)
            if not text:
                continue
            events.append((n, f"L{n} {rec['timestamp'][11:19]} {text}"))
            if _is_hit(rec["type"], b):
                hits.append(n)
    return events, hits


def _render_block(role: str, b: dict[str, Any]) -> str:
    t = b.get("type")
    if t == "text" and b.get("text", "").strip():
        limit = 600 if role == "user" else 300
        return f"{role.upper()}: {_flat(b['text'])[:limit]}"
    if t == "tool_use":
        return f"TOOL {b.get('name')}: {_flat(json.dumps(b.get('input'), default=str))[:200]}"
    if t == "tool_result" and b.get("is_error"):
        return f"ERROR: {_flat(_text(b.get('content')))[:200]}"
    if t == "tool_result" and "AskUserQuestion" in str(b.get("content"))[:200]:
        return f"ANSWER: {_flat(_text(b.get('content')))[:300]}"
    return ""


def _is_hit(role: str, b: dict[str, Any]) -> bool:
    text = b.get("text", "") if b.get("type") == "text" else _text(b.get("content"))
    return (
        (role == "user" and b.get("type") == "text" and "[Request interrupted" in text)
        or (b.get("type") == "tool_result" and "doesn't want to proceed" in text)
        or (b.get("type") == "tool_use" and b.get("name") == "AskUserQuestion")
    )


def _session_repos(r: dict[str, Any]) -> set[str]:
    repos = {m.group(1) for u in _urls(r) for m in [_PR_URL.match(u)] if m}
    if slug := _remote_slug(r.get("cwd")):
        repos.add(slug)
    return repos


def _urls(r: dict[str, Any]) -> list[str]:
    lk = r.get("links") or {}
    return [u for k in ("pr_urls_created", "pr_urls_linked", "pr_urls_mentioned") for u in lk.get(k) or []]


def _remote_slug(cwd: str | None) -> str | None:
    if not cwd or not Path(cwd).is_dir():
        return None
    res = subprocess.run(["git", "-C", cwd, "remote", "get-url", "origin"], capture_output=True, text=True)
    m = re.search(r"[:/]([^/\s:]+/[^/\s]+?)(?:\.git)?$", res.stdout.strip())
    return m.group(1) if m else None


def _tokens(r: dict[str, Any]) -> int | None:
    totals = (r.get("usage") or {}).get("totals") or {}
    return sum(v for k, v in totals.items() if k.endswith("_tokens") and isinstance(v, int)) or None


def _ref(p: dict[str, Any]) -> str:
    return f"{p['repo']}#{p['number']}"


def _text(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return str(content or "")


def _flat(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r, default=str) + "\n" for r in rows), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
