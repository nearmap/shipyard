"""`/sy:retro` discovery: join the session table to PR outcomes, cost every session, and score the evidence.

`python -m sy_tools.retro discover --since D --out DIR` builds the session table and lists candidate repositories;
adding `--repo owner/name ...` fetches their PR outcomes, links them to sessions, renders one digest per session,
writes facet and comment batches, and drafts `discovery.json`. `fold-tags --out DIR` folds `sy:comment-tag` results
back in. `analyze --out DIR` folds the `sy:session-read` facets in and writes `analysis.json`, one section per goal.
"""

import argparse
from collections import Counter
from datetime import UTC, date, datetime
import json
from pathlib import Path
import re
from statistics import median
import subprocess
import sys
from typing import Any

from . import pr_outcomes, sessions

COMMENT_BATCH = 50
DIGEST_CHARS = 40_000
FACET_BATCH_CHARS = 120_000
PRICING = Path(__file__).resolve().parents[1] / "skills" / "retro" / "references" / "pricing.json"
PLANNING_SKILLS = ("sy:plan", "sy:spec", "sy:doit", "sy:mvp", "sy:spike", "sy:whiteboard")
KEY_TOOLS = ("Skill", "Agent", "Task", "AskUserQuestion", "ExitPlanMode")
_PR_URL = re.compile(r"https://[^/\s]+/([^/\s]+/[^/\s]+)/pull/(\d+)")
_COMMAND = re.compile(r"<command-name>([^<]*)</command-name>(?:.*?<command-args>([^<]*)</command-args>)?", re.S)
_DEFAULT_BRANCHES = {"main", "master", "HEAD", ""}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover")
    d.add_argument("--since", type=date.fromisoformat, required=True)
    d.add_argument("--out", type=Path, required=True)
    d.add_argument("--repo", nargs="*", default=[])
    d.add_argument("--projects", type=Path, default=Path.home() / ".claude" / "projects")
    for name in ("fold-tags", "analyze"):
        sub.add_parser(name).add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.cmd == "fold-tags":
        print(json.dumps(fold_tags(args.out)))
        return 0
    if args.cmd == "analyze":
        print(json.dumps(analyze(args.out)))
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
    facet_batches = write_digests(projects, rows, out)
    batches = write_comment_batches(prs, out / "comments")
    draft = draft_discovery(rows, prs, links, repos, since)
    (out / "discovery.json").write_text(json.dumps(draft, indent=1) + "\n", encoding="utf-8")
    return {
        "sessions": len(rows),
        "prs": len(prs),
        "linked_prs": len(links),
        "facet_batches": facet_batches,
        "comment_batches": batches,
    }


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


def write_digests(projects: Path, rows: list[dict[str, Any]], out: Path) -> int:
    """One text digest per session under `digests/`, and `facets/batch-NNN.json` lists for `sy:session-read`.

    A digest keeps every human turn, answer, error and skill or agent call. Over DIGEST_CHARS it drops the other
    tool calls, then elides the middle behind a marker that says how many lines went.
    """
    (out / "digests").mkdir(parents=True, exist_ok=True)
    (out / "facets").mkdir(parents=True, exist_ok=True)
    batches: list[list[dict[str, Any]]] = [[]]
    size = 0
    for r in rows:
        events = _events(projects / r["project_dir"] / f"{r['session_id']}.jsonl")
        text = _digest([line for _, line in events])
        path = out / "digests" / f"{r['session_id']}.txt"
        path.write_text(text, encoding="utf-8")
        if size and size + len(text) > FACET_BATCH_CHARS:
            batches.append([])
            size = 0
        size += len(text)
        batches[-1].append(
            {
                "session_id": r["session_id"],
                "digest": str(path),
                "measured": {
                    "active_minutes": r.get("active_minutes"),
                    "skills": sorted(_skill_names(r)),
                    "interventions": r.get("interventions"),
                },
            }
        )
    batches = [b for b in batches if b]
    for n, batch in enumerate(batches, 1):
        (out / "facets" / f"batch-{n:03d}.json").write_text(json.dumps(batch, indent=1) + "\n", encoding="utf-8")
    return len(batches)


def write_comment_batches(prs: list[dict[str, Any]], comments_dir: Path) -> int:
    """Review comments from anyone but the PR author, in batches for `sy:comment-tag`."""
    comments_dir.mkdir(parents=True, exist_ok=True)
    by_repo: dict[str, list[dict[str, Any]]] = {}
    for p in prs:
        for c in p.get("comments") or []:
            if c.get("author") != p.get("author"):
                by_repo.setdefault(p["repo"], []).append(
                    {
                        "id": f"{_ref(p)}/{c['id']}",
                        "path": c.get("path"),
                        "body": c["body"],
                        "author_kind": c["author_kind"],
                    }
                )
    n = 0
    for repo, items in by_repo.items():
        for i in range(0, len(items), COMMENT_BATCH):
            n += 1
            name = f"batch-{repo.replace('/', '__')}-{i // COMMENT_BATCH + 1:03d}.json"
            (comments_dir / name).write_text(
                json.dumps(items[i : i + COMMENT_BATCH], indent=1) + "\n", encoding="utf-8"
            )
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


def session_costs(rows: list[dict[str, Any]], prices: dict[str, Any]) -> dict[str, Any]:
    """Estimated list-price cost over every session, split by token class, side, skill, agent type and idle rewrites.

    Cache reads, 5-minute writes and 1-hour writes are priced apart: the write premium and the rewrite after an idle
    gap are what make a long session expensive. `unpriced_models` names any model the price table lacks.
    """
    total: Counter = Counter()
    sides: Counter = Counter()
    by_skill: Counter = Counter()
    by_agent: Counter = Counter()
    tokens: Counter = Counter()
    idle: Counter = Counter()
    unpriced: set[str] = set()
    for r in rows:
        t = r.get("tokens") or {}
        for side in ("main", "subagents"):
            for model, counts in (t.get(side) or {}).items():
                c = _cost(model, counts, prices, unpriced)
                total.update(c)
                sides[side] += sum(c.values())
                tokens.update({k: v for k, v in counts.items() if k.endswith("_tokens")})
        for key, out in (("by_skill", by_skill), ("by_agent_type", by_agent)):
            for name, models in (t.get(key) or {}).items():
                out[name] += sum(sum(_cost(m, c, prices, unpriced).values()) for m, c in models.items())
        for gap in ("gt_5m", "gt_1h"):
            for model, n in ((r.get("cache") or {}).get(gap, {}).get("write_tokens") or {}).items():
                idle[gap] += n * _price(model, prices, unpriced).get("input", 0) * 1.5 / 1e6
    whole = sum(total.values()) or 1.0
    return {
        "usd_estimate": round(sum(total.values()), 2),
        "by_class_usd": {k: round(v, 2) for k, v in total.most_common()},
        "by_class_share": {k: round(v / whole, 3) for k, v in total.most_common()},
        "tokens": dict(tokens.most_common()),
        "subagent_share": round(sides["subagents"] / whole, 3),
        "idle_rewrite_usd": {k: round(v, 2) for k, v in idle.items()},
        "by_skill_usd": {k: round(v, 2) for k, v in by_skill.most_common(15)},
        "by_agent_type_usd": {k: round(v, 2) for k, v in by_agent.most_common(15)},
        "unpriced_models": sorted(unpriced),
        "basis": "list price; a subscription bills differently, so compare shares, not the dollar figure",
    }


def analyze(out: Path) -> dict[str, Any]:
    """Fold `facets/facets-*.json` into `analysis.json` and discovery.json: one section of metrics per goal.

    Every metric carries the sessions or PRs it rests on, so the interview can ask why a pattern exists.
    """
    disc_path = out / "discovery.json"
    disc = json.loads(disc_path.read_text(encoding="utf-8"))
    batches = sorted((out / "facets").glob("batch-*.json"))
    facets: dict[str, dict[str, Any]] = {}
    for b in batches:
        done = b.with_name(b.name.replace("batch-", "facets-"))
        assert done.is_file(), f"{done} missing: run sy:session-read over every facet batch first"
        facets.update({f["session_id"]: f for f in json.loads(done.read_text(encoding="utf-8"))})
    in_scope = {s for p in disc["prs"] for s in p["sessions"]} | set(facets)
    table = out / "sessions.jsonl"
    assert table.is_file(), f"{table} missing: run discover first"
    rows = [json.loads(x) for x in table.read_text(encoding="utf-8").splitlines() if x]
    rows = [r for r in rows if r["session_id"] in in_scope]
    _write_jsonl(out / "facets.jsonl", list(facets.values()))
    analysis = _goal_metrics(rows, facets, disc)
    (out / "analysis.json").write_text(json.dumps(analysis, indent=1) + "\n", encoding="utf-8")
    for p in disc["prs"]:
        approaches = Counter(facets[s].get("approach") for s in p["sessions"] if s in facets)
        if approaches:
            p["approach"] = approaches.most_common(1)[0][0]
            p["approach_source"] = "session_facets"
    disc["analysis"] = analysis
    disc["unknown"] = [u for u in disc["unknown"] if u != "analysis"]
    disc_path.write_text(json.dumps(disc, indent=1) + "\n", encoding="utf-8")
    return {"sessions_with_facets": len(facets), "sessions_in_scope": len(rows), "goals": sorted(analysis["goals"])}


def draft_discovery(
    rows: list[dict[str, Any]], prs: list[dict[str, Any]], links: dict[str, Any], repos: list[str], since: date
) -> dict[str, Any]:
    """discovery.json with every measured field filled and every interview field `unknown`."""
    totals: dict[str, Counter] = {k: Counter() for k in ("skills", "agents", "interventions", "tool_errors")}
    for r in rows:
        totals["skills"].update(_skill_names(r))
        totals["agents"].update(r["agents"].get("spawned") or {})
        totals["interventions"].update(r.get("interventions") or {})
        totals["tool_errors"].update(r.get("tool_errors") or {})
    linked = {sid for by_sid in links.values() for sid in by_sid}
    sy_sessions = sum(1 for r in rows if any(k.startswith("sy:") for k in _skill_names(r)))
    prices = _prices()
    shares = Counter(sid for by_sid in links.values() for sid in by_sid)
    by_sid = {r["session_id"]: r for r in rows}
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
                "tokens": _split_share([by_sid[s] for s in sids if s in by_sid], shares, prices),
                "approach_marker": p.get("approach_marker"),
                "approach": "unknown",
            }
        )
    return {
        "schema": "shipyard.discovery.v2",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "window": {"since": since.isoformat(), "until": date.today().isoformat()},
        "repos": repos,
        "sessions": {
            "count": len(rows),
            "linked_to_prs": len(linked),
            "active_hours": round(sum(r.get("active_minutes") or 0 for r in rows) / 60, 1),
            "shipyard": {"sessions": sy_sessions, "level": shipyard_level(sy_sessions, len(rows))},
            **{k: dict(v.most_common()) for k, v in totals.items()},
            "skills_nested": dict(sum((Counter(r["skills"].get("tool_nested") or {}) for r in rows), Counter())),
            "tool_error_classes": dict(
                sum((Counter(r.get("tool_error_classes") or {}) for r in rows), Counter()).most_common()
            ),
            "cost": session_costs(rows, prices),
        },
        "prs": pr_items,
        "analysis": None,
        "patterns": [],
        "shipyard_usage": {"tried": [], "skipped": []},
        "control": [],
        "pain_points": [],
        "extra_fields": {},
        "unknown": [
            "analysis",
            "patterns[].why",
            *(["shipyard_usage"] if sy_sessions else []),
            "control",
            "pain_points",
        ],
        "approval": {"status": "draft", "approved_at": None, "redactions": 0},
    }


def shipyard_level(sy_sessions: int, sessions: int) -> str:
    """`none`, `light` (under 5 sessions or a tenth of them) or `regular`: how much of the window used Shipyard."""
    if not sy_sessions:
        return "none"
    return "light" if sy_sessions < 5 or sy_sessions * 10 < sessions else "regular"


def _skill_names(r: dict[str, Any]) -> dict[str, int]:
    return {**(r["skills"].get("slash") or {}), **(r["skills"].get("tool") or {})}


def _goal_metrics(
    rows: list[dict[str, Any]], facets: dict[str, dict[str, Any]], disc: dict[str, Any]
) -> dict[str, Any]:
    by_sid = {r["session_id"]: r for r in rows}
    merged = [p for p in disc["prs"] if p["outcome"]["state"] == "MERGED"]
    sy = {s for s, r in by_sid.items() if any(k.startswith("sy:") for k in _skill_names(r))}

    def tally(key: str, sids: Any = None) -> dict[str, int]:
        c: Counter = Counter()
        for s in sids if sids is not None else facets:
            v = (facets.get(s) or {}).get(key)
            if isinstance(v, dict):
                c.update({k: n for k, n in v.items() if isinstance(n, int)})
            elif isinstance(v, list):
                c.update(v)
            elif v:
                c[v] += 1
        return dict(c.most_common())

    def where(key: str, value: str, n: int = 5) -> list[str]:
        hits = [s for s, f in facets.items() if value in (f.get(key) or {}) or f.get(key) == value]
        return hits[:n]

    def achieved(sids: set[str]) -> float | None:
        rated = [facets[s]["outcome"] for s in sids if s in facets and facets[s].get("outcome") != "unclear"]
        return round(sum(o in ("fully_achieved", "mostly_achieved") for o in rated) / len(rated), 2) if rated else None

    def per_pr(fn: Any) -> float | None:
        vals = [fn(p) for p in merged]
        vals = [v for v in vals if v is not None]
        return round(median(vals), 2) if vals else None

    shares = Counter(s for p in disc["prs"] for s in p["sessions"])

    def human_turns(p: dict[str, Any]) -> float:
        return sum(by_sid[s]["interventions"]["user_turns"] / shares[s] for s in p["sessions"] if s in by_sid)

    interventions: dict[str, Counter] = {}
    for r in rows:
        for skill, c in (r.get("by_skill") or {}).items():
            interventions.setdefault(skill, Counter()).update(c)
    turns = tally("human_turns")
    cost = session_costs(rows, _prices())
    plan_runs = Counter({k: n for r in rows for k, n in _skill_names(r).items() if k in PLANNING_SKILLS})
    planned = [
        p["ref"]
        for p in merged
        if p.get("approach_marker")
        or any(k in PLANNING_SKILLS for s in p["sessions"] if s in by_sid for k in _skill_names(by_sid[s]))
    ]
    rework = [
        p["ref"]
        for p in disc["prs"]
        if (p["outcome"].get("review_rounds") or 0) >= 3
        or p["outcome"].get("reverted")
        or p["outcome"].get("followed_up")
    ]
    tagged = Counter(k for p in disc["prs"] for k, n in p["outcome"].get("rules_hit", {}).items() for _ in range(n))
    comments = [sum(p["outcome"].get("comments_by_kind", {}).values()) for p in merged]
    return {
        "sessions": len(rows),
        "sessions_with_facets": len(facets),
        "goals": {
            "P1": {
                "planning_layers": tally("planning_layer"),
                "planning_skill_runs": dict(plan_runs.most_common()),
                "merged_prs_with_a_plan": f"{len(planned)}/{len(merged)}",
                "evidence": planned[:5],
            },
            "P2": {
                "shipyard_level": disc["sessions"]["shipyard"]["level"],
                "outcome_achieved_share": {"shipyard": achieved(sy), "other": achieved(set(by_sid) - sy)},
                "evidence": sorted(sy)[:5],
            },
            "P3": {
                "approaches": tally("approach"),
                "outputs": tally("outputs"),
                "work_types": tally("work_type"),
            },
            "P4": {
                "human_turns_by_class": turns,
                "factual_share": round(turns.get("factual", 0) / (sum(turns.values()) or 1), 2),
                "asks": sum(r["interventions"].get("ask_user_question", 0) for r in rows),
                "interventions_by_skill": {
                    k: dict(v) for k, v in sorted(interventions.items(), key=lambda kv: -sum(kv[1].values()))[:8]
                },
                "evidence": where("human_turns", "factual"),
            },
            "P5": {
                "merged_prs": len(merged),
                "median_sessions_per_pr": per_pr(lambda p: len(p["sessions"]) or None),
                "median_human_turns_per_pr": per_pr(human_turns),
                "median_hours_to_merge": per_pr(lambda p: p["outcome"].get("hours_to_merge")),
                "median_ci_failed_commits": per_pr(lambda p: p["outcome"].get("ci_failed_commits")),
            },
            "P6": {
                "median_review_comments_per_merged_pr": round(median(comments), 1) if comments else None,
                "median_review_rounds": per_pr(lambda p: p["outcome"].get("review_rounds")),
                "comments_by_rule": dict(tagged.most_common(8)),
            },
            "T1": {
                "prs_reworked_after_review": rework,
                "friction_rework": {
                    k: v
                    for k, v in tally("friction").items()
                    if k in ("wrong_approach", "review_rework", "misunderstood_request")
                },
                "evidence": list(
                    dict.fromkeys(where("friction", "wrong_approach") + where("friction", "review_rework"))
                ),
            },
            "T3": {
                **cost,
                "median_cost_per_merged_pr_usd": per_pr(lambda p: (p.get("tokens") or {}).get("usd_estimate")),
                "median_peak_context": round(
                    median([r["cache"]["peak_context"] for r in rows if r.get("cache")] or [0])
                ),
                "compactions": sum(r.get("compactions", 0) for r in rows),
            },
        },
        "friction": {k: {"count": v, "evidence": where("friction", k, 3)} for k, v in tally("friction").items()},
        "friction_details": [f["friction_detail"] for f in facets.values() if f.get("friction_detail")][:20],
    }


def _prices() -> dict[str, Any]:
    return json.loads(PRICING.read_text(encoding="utf-8"))


def _price(model: str, prices: dict[str, Any], unpriced: set[str]) -> dict[str, float]:
    match = max((k for k in prices["models"] if model.startswith(k)), key=len, default=None)
    if match is None:
        unpriced.add(model)
        return {}
    return prices["models"][match]


def _cost(model: str, counts: dict[str, int], prices: dict[str, Any], unpriced: set[str]) -> dict[str, float]:
    p = _price(model, prices, unpriced)
    if not p:
        return {}
    m = prices["cache_write_multipliers"]
    w5, w1 = counts.get("cache_write_5m_input_tokens", 0), counts.get("cache_write_1h_input_tokens", 0)
    unsplit = max(counts.get("cache_creation_input_tokens", 0) - w5 - w1, 0)
    return {
        "input": counts.get("input_tokens", 0) * p["input"] / 1e6,
        "output": (counts.get("output_tokens", 0) + counts.get("output_tokens_estimated", 0)) * p["output"] / 1e6,
        "cache_read": counts.get("cache_read_input_tokens", 0) * p["cache_read"] / 1e6,
        "cache_write_5m": (w5 + unsplit) * p["input"] * m["5m"] / 1e6,
        "cache_write_1h": w1 * p["input"] * m["1h"] / 1e6,
    }


def _split_share(rows: list[dict[str, Any]], shares: Counter, prices: dict[str, Any]) -> dict[str, Any] | None:
    """A PR's share of its sessions' tokens and cost: a session linked to n PRs gives each 1/n."""
    if not rows:
        return None
    tokens: Counter = Counter()
    usd = 0.0
    for r in rows:
        n = shares[r["session_id"]] or 1
        for side in ("main", "subagents"):
            for model, counts in ((r.get("tokens") or {}).get(side) or {}).items():
                tokens.update({k: v / n for k, v in counts.items() if k.endswith("_tokens")})
                usd += sum(_cost(model, counts, prices, set()).values()) / n
    return {**{k: round(v) for k, v in tokens.items()}, "usd_estimate": round(usd, 2)}


def _session_table(projects: Path, since: date, out: Path) -> list[dict[str, Any]]:
    cache = out / "sessions.jsonl"
    if cache.is_file():
        return [json.loads(line) for line in cache.read_text(encoding="utf-8").splitlines() if line]
    rows = sessions.collect(projects, datetime.combine(since, datetime.min.time(), tzinfo=UTC))
    _write_jsonl(cache, rows)
    return rows


def _events(main: Path) -> list[tuple[int, str]]:
    events: list[tuple[int, str]] = []
    names: dict[str, str] = {}
    for n, line in enumerate(main.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") not in ("user", "assistant") or not rec.get("timestamp") or rec.get("isMeta"):
            continue
        content = (rec.get("message") or {}).get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
        for b in blocks if isinstance(blocks, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                names[b.get("id", "")] = str(b.get("name"))
            text = _render_block(rec["type"], b, names)
            if text:
                events.append((n, f"L{n} {rec['timestamp'][11:19]} {text}"))
    return events


def _render_block(role: str, b: dict[str, Any], names: dict[str, str]) -> str:
    t = b.get("type")
    if t == "text" and role == "user" and (cmd := _COMMAND.search(b.get("text", ""))):
        return f"COMMAND: /{cmd.group(1).lstrip('/')} {_flat(cmd.group(2) or '')[:200]}".rstrip()
    if t == "text" and role == "user" and not sessions.WRAPPER.sub("", b.get("text", "")).strip():
        return ""
    if t == "text" and b.get("text", "").strip():
        limit = 600 if role == "user" else 300
        return f"{role.upper()}: {_flat(b['text'])[:limit]}"
    if t == "tool_use":
        limit = 600 if b.get("name") == "AskUserQuestion" else 200
        return f"TOOL {b.get('name')}: {_flat(json.dumps(b.get('input'), default=str))[:limit]}"
    if t == "tool_result" and b.get("is_error"):
        return f"ERROR: {_flat(_text(b.get('content')))[:200]}"
    if t == "tool_result" and names.get(b.get("tool_use_id", "")) == "AskUserQuestion":
        return f"ANSWER: {_flat(_text(b.get('content')))[:400]}"
    return ""


def _digest(lines: list[str]) -> str:
    """Fit a session's events in DIGEST_CHARS, dropping in order: plain tool calls, then assistant text.

    Human turns, commands, answers, errors and skill or agent calls are never dropped; each cut leaves a marker.
    """
    kept = [x for x in lines if not _is_tool(x) or any(f" TOOL {t}:" in x for t in KEY_TOOLS)]
    if _size(lines) > DIGEST_CHARS and len(kept) < len(lines):
        lines = [*kept, f"[{len(lines) - len(kept)} other tool calls left out]"]
    if _size(lines) > DIGEST_CHARS:
        spoken = [x for x in lines if " ASSISTANT: " not in x[:20]]
        lines = [*spoken, f"[{len(lines) - len(spoken)} assistant messages left out]"]
    return "\n".join(lines) + "\n"


def _is_tool(line: str) -> bool:
    return " TOOL " in line[:20]


def _size(lines: list[str]) -> int:
    return sum(len(x) + 1 for x in lines)


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
