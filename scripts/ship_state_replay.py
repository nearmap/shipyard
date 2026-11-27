"""Replay every `ship-state.yaml` write in the kept Claude Code transcripts and rank the transitions that broke a rule.

`ship_state_update` merges fields blindly, so the order of its calls rebuilds each run's state. The replay checks each
write against the state before it and counts the rule each one breaks. A rule is checked only on writes made after the
commit that introduced it. `python scripts/ship_state_replay.py` prints the ranked failures and the `phase_active`
transition table, ranked by impact and then by state files affected; `--json <file>` also writes them; `--days N` keeps only writes from the last N days.
"""

import argparse
from collections import Counter, defaultdict
from collections.abc import Iterator
import dataclasses
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
PHASES = ("START", "BUILD", "GATE")
APPEND_ONLY = ("gate_round_log", "plan_supersede_events")
ROUND_KEYS = ("reviewed_sha", "terminal_pass", "next_round_causes")
_STATE_FILE = "ship-state.yaml"
_HEREDOC_SEED = re.compile(r"cat\s*>\s*(\S*ship-state\.yaml)\s*<<-?\s*['\"]?(\w+)['\"]?\n(.*?)\n\2", re.S)
_REDIRECT = re.compile(r"(?:>>?|tee(?:\s+-a)?)\s*(\S*ship-state\.yaml)")
_SED_IN_PLACE = re.compile(r"(?:sed|yq)\s+[^;&|\n]*-i[^;&|\n]*ship-state\.yaml")
_PY_OPEN_W = re.compile(r"open\(\s*([A-Za-z_]\w*|'[^']*'|\"[^\"]*\")\s*,\s*['\"][wa]")
_PY_WRITE_TEXT = re.compile(r"([A-Za-z_]\w*)\.write_text\(")
IMPACT = {
    "list_field_shrunk": "state lost",
    "list_field_history_rewritten": "state lost",
    "list_field_written_as_string": "state lost",
    "round_log_missing_rounds": "state lost",
    "round_total_written_apart_from_log": "contract broken",
    "bail_recorded_outside_bail_field": "state lost",
    "write_refused:ship_state_update": "write lost",
    "hand_edit:Bash": "merge bypassed",
    "hand_edit:Write": "merge bypassed",
    "hand_edit:Edit": "merge bypassed",
    "phase_order_backwards": "contract broken",
    "phase_active_written_by_worker": "contract broken",
    "phase_active_off_vocabulary": "contract broken",
    "round_log_entry_missing_keys": "contract broken",
    "round_log_entry_not_a_mapping": "contract broken",
    "fields_passed_as_string": "contract broken",
    "write_before_seed_seen": "replay gap",
    "phase_active_restamped_without_clear": "benign",
    "phase_checkpoint_serialised_as_string": "benign",
}
RULE = {
    "list_field_shrunk": "SHIP-S04",
    "list_field_history_rewritten": "SHIP-S04",
    "list_field_written_as_string": "SHIP-S04",
    "round_log_missing_rounds": "SHIP-S03",
    "round_total_written_apart_from_log": "SHIP-S03",
    "bail_recorded_outside_bail_field": "SHIP-S07",
    "hand_edit:Bash": "SHIP-S06",
    "hand_edit:Write": "SHIP-S06",
    "hand_edit:Edit": "SHIP-S06",
    "phase_order_backwards": "SHIP-P02",
    "phase_active_written_by_worker": "SHIP-P04",
    "phase_active_off_vocabulary": "SHIP-P01",
    "round_log_entry_missing_keys": "SHIP-S01",
    "round_log_entry_not_a_mapping": "SHIP-S04",
    "phase_active_restamped_without_clear": "SHIP-P02",
}
_IMPACT_ORDER = ("state lost", "write lost", "merge bypassed", "contract broken", "replay gap", "benign")
_ABS_STATE = re.compile(r"(/[^\s'\"]*ship-state\.yaml)")
_CD = re.compile(r"cd\s+(\S+)")


@dataclasses.dataclass
class Write:
    ts: str
    path: str
    session: str
    by_worker: bool
    tool: str
    fields: Any = None
    error: str | None = None
    content: str | None = None


@dataclasses.dataclass
class Failure:
    kind: str
    path: str
    session: str
    ts: str
    detail: str = ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--projects", type=Path, default=Path.home() / ".claude" / "projects")
    parser.add_argument("--days", type=int, default=0)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat() if args.days else ""
    writes = [w for w in collect(args.projects) if w.ts >= since]
    by_path: dict[str, list[Write]] = defaultdict(list)
    for w in writes:
        by_path[w.path].append(w)
    rule_dates = {k: _introduced(k) for k in ROUND_KEYS}
    failures: list[Failure] = []
    transitions: Counter = Counter()
    for path, seq in by_path.items():
        seq.sort(key=lambda w: w.ts)
        failures += replay(path, seq, transitions, rule_dates)
    report = rank(failures, len(by_path), writes)
    report["rule_introduced"] = rule_dates
    report["phase_active_transitions"] = [
        {"from": a, "to": b, "count": n} for (a, b), n in transitions.most_common()
    ]
    print(render(report))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


def collect(projects: Path) -> list[Write]:
    """Every write to a ship state file found in the transcripts, one per tool_use id."""
    found: dict[str, Write] = {}
    for f in projects.rglob("*.jsonl"):
        pending: dict[str, Write] = {}
        for rec in _records(f, pending):
            content = (rec.get("message") or {}).get("content")
            if not rec.get("timestamp") or not isinstance(content, list):
                continue
            for c in content:
                if c.get("type") == "tool_use" and c.get("id") not in found:
                    w = _as_write(c, rec, f)
                    if w:
                        pending[c["id"]] = found[c["id"]] = w
                elif c.get("type") == "tool_result" and c.get("tool_use_id") in pending and c.get("is_error"):
                    pending[c["tool_use_id"]].error = _text(c.get("content"))[:160]
    return list(found.values())


def replay(path: str, seq: list[Write], transitions: Counter, rule_dates: dict[str, str]) -> list[Failure]:
    """Apply one state file's writes in order; return each rule a write broke."""
    out: list[Failure] = []
    state: dict[str, Any] = {}
    seeded = False
    last_dispatch: str | None = None
    reentry_since_dispatch = False
    desync = 0
    desyncs: list[Failure] = []
    tainted = False

    def fail(w: Write, kind: str, detail: str = "") -> None:
        out.append(Failure(kind, path, w.session, w.ts, detail))

    for w in seq:
        if w.error:
            fail(w, f"write_refused:{w.tool}", _error_class(w.error))
            continue
        if w.tool != "ship_state_update":
            if seeded or w.content is None:
                fail(w, f"hand_edit:{w.tool}")
                tainted = True
            loaded = _safe_yaml(w.content) if w.content else None
            if isinstance(loaded, dict):
                state, seeded, last_dispatch = loaded, True, None
            continue
        fields = w.fields
        if isinstance(fields, str):
            fail(w, "fields_passed_as_string")
            fields = _safe_yaml(fields)
        if not isinstance(fields, dict):
            continue
        if not seeded:
            fail(w, "write_before_seed_seen")
            seeded = True
        if fields.get("reentry_pending") or fields.get("bail") or _plan_changed(state, fields) or _pregate(fields):
            reentry_since_dispatch = True
        if "phase_active" in fields:
            old, new = state.get("phase_active"), fields["phase_active"]
            transitions[(str(old), str(new))] += 1
            if w.by_worker:
                fail(w, "phase_active_written_by_worker", str(new))
            if new is not None and str(new) not in PHASES:
                fail(w, "phase_active_off_vocabulary", str(new))
            if old is not None and new is not None:
                fail(w, "phase_active_restamped_without_clear", f"{old}->{new}")
            if new is not None and str(new).upper() in PHASES:
                cur = str(new).upper()
                if last_dispatch and PHASES.index(cur) < PHASES.index(last_dispatch) and not reentry_since_dispatch:
                    fail(w, "phase_order_backwards", f"{last_dispatch}->{cur}")
                last_dispatch, reentry_since_dispatch = cur, False
        cp = fields.get("phase_checkpoint")
        if isinstance(cp, str) and cp.strip().lower().startswith(("bail", "bail-to-spec")) and not (
            fields.get("bail") or state.get("bail")
        ):
            fail(w, "bail_recorded_outside_bail_field")
        for name in APPEND_ONLY:
            if name in fields and not tainted:
                out += [Failure(k, path, w.session, w.ts, name) for k in _list_failures(state.get(name), fields[name])]
        if isinstance(fields.get("gate_round_log"), list):
            old_log = state.get("gate_round_log") if isinstance(state.get("gate_round_log"), list) else []
            for entry in fields["gate_round_log"][len(old_log):]:
                if not isinstance(entry, dict):
                    fail(w, "round_log_entry_not_a_mapping")
                    continue
                missing = [k for k in ROUND_KEYS if k not in entry and w.ts >= rule_dates[k]]
                if missing:
                    fail(w, "round_log_entry_missing_keys", ",".join(missing))
        if isinstance(cp, str) and isinstance(_safe_yaml(cp), (dict, list)):
            fail(w, "phase_checkpoint_serialised_as_string")
        state = {**state, **fields}
        if "gate_round_log" in fields or "gate_rounds_total" in fields:
            log, total = state.get("gate_round_log"), state.get("gate_rounds_total")
            now = total - len(log) if isinstance(log, list) and isinstance(total, int) else 0
            if now != desync and now != 0 and not tainted:
                desyncs.append(Failure("", path, w.session, w.ts, f"total-log={now:+d}"))
            desync, tainted = now, False
    kind = "round_log_missing_rounds" if desync else "round_total_written_apart_from_log"
    return out + [dataclasses.replace(f, kind=kind) for f in desyncs]


def rank(failures: list[Failure], n_files: int, writes: list[Write]) -> dict[str, Any]:
    """Failures grouped by kind, ranked by state files affected, then occurrences."""
    groups: dict[str, list[Failure]] = defaultdict(list)
    for f in failures:
        groups[f.kind].append(f)
    rows = []
    for kind, fs in groups.items():
        details = Counter(f.detail for f in fs if f.detail)
        rows.append(
            {
                "kind": kind,
                "rule": RULE.get(kind, ""),
                "impact": IMPACT.get(kind, "write lost" if kind.startswith("write_refused") else "contract broken"),
                "files": len({f.path for f in fs}),
                "occurrences": len(fs),
                "sessions": len({f.session for f in fs}),
                "top_details": [f"{d} ({n})" for d, n in details.most_common(4)],
                "first_seen": min(f.ts for f in fs),
                "last_seen": max(f.ts for f in fs),
            }
        )
    rows.sort(key=lambda r: (_IMPACT_ORDER.index(r["impact"]), -r["files"], -r["occurrences"]))
    tss = sorted(w.ts for w in writes)
    return {
        "state_files": n_files,
        "writes": len(writes),
        "window": [tss[0], tss[-1]] if tss else [],
        "failures": rows,
    }


def render(report: dict[str, Any]) -> str:
    lines = [
        f"state files {report['state_files']}, writes {report['writes']}, window {' to '.join(report['window'])}",
        "",
        "| rank | impact | rule | failure | files | occurrences | sessions | last seen | top details |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(report["failures"], 1):
        details = "; ".join(d.replace("\n", " ").replace("|", "/") for d in r["top_details"])
        lines.append(
            f"| {i} | {r['impact']} | {r['rule']} | {r['kind']} | {r['files']} | {r['occurrences']} | {r['sessions']} | {r['last_seen'][:10]} "
            f"| {details} |"
        )
    lines += ["", "| phase_active from | to | count |", "|---|---|---|"]
    lines += [f"| {t['from']} | {t['to']} | {t['count']} |" for t in report["phase_active_transitions"]]
    return "\n".join(lines)


def _list_failures(old: Any, new: Any) -> list[str]:
    if isinstance(new, str):
        return ["list_field_written_as_string"]
    if not isinstance(old, list) or not isinstance(new, list) or not old:
        return []
    if len(new) < len(old):
        return ["list_field_shrunk"]
    keep = len(old) - 1
    if [_key(e) for e in new[:keep]] != [_key(e) for e in old[:keep]]:
        return ["list_field_history_rewritten"]
    return []


def _key(entry: Any) -> str:
    if isinstance(entry, dict):
        return json.dumps({k: entry.get(k) for k in ("round", "reviewed_sha", "id") if k in entry}, default=str)
    return json.dumps(entry, default=str)


def _as_write(c: dict[str, Any], rec: dict[str, Any], f: Path) -> Write | None:
    name, inp = c.get("name", ""), c.get("input") or {}
    base = {"ts": rec["timestamp"], "session": rec.get("sessionId", f.stem), "by_worker": "subagents" in f.parts}
    if name.endswith("ship_state_update"):
        return Write(path=str(inp.get("path")), tool="ship_state_update", fields=inp.get("fields"), **base)
    if name in ("Write", "Edit") and str(inp.get("file_path", "")).endswith(_STATE_FILE):
        return Write(path=inp["file_path"], tool=name, content=inp.get("content"), **base)
    cmd = str(inp.get("command", "")) if name == "Bash" else ""
    if _STATE_FILE not in cmd:
        return None
    if m := _HEREDOC_SEED.search(cmd):
        return Write(path=m.group(1), tool="Bash", content=m.group(3), **base)
    if _REDIRECT.search(cmd) or _SED_IN_PLACE.search(cmd) or _py_writes_state(cmd):
        return Write(path=_bash_target(cmd), tool="Bash", **base)
    return None


def _py_writes_state(cmd: str) -> bool:
    targets = [m.group(1) for m in _PY_OPEN_W.finditer(cmd)] + [m.group(1) for m in _PY_WRITE_TEXT.finditer(cmd)]
    for t in targets:
        if _STATE_FILE in t or re.search(rf"\b{re.escape(t)}\s*=[^\n]*{re.escape(_STATE_FILE)}", cmd):
            return True
    return False


def _pregate(fields: dict[str, Any]) -> bool:
    return any(k.startswith("pregate_checkpoint_") for k in fields)


def _plan_changed(state: dict[str, Any], fields: dict[str, Any]) -> bool:
    return any(k in fields and fields[k] != state.get(k) for k in ("plan_version", "plan_comment_id"))


def _bash_target(cmd: str) -> str:
    if m := _ABS_STATE.search(cmd):
        return m.group(1)
    if m := _CD.search(cmd):
        return f"{m.group(1).rstrip(';&')}/{_STATE_FILE}"
    return f"<unresolved>/{_STATE_FILE}"


def _introduced(key: str) -> str:
    out = subprocess.run(
        ["git", "log", "--reverse", "--format=%cI", f"-S{key}", "--", "skills/ship"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    assert out, f"no commit introduces {key!r} under skills/ship"
    return datetime.fromisoformat(out[0]).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _records(f: Path, pending: dict[str, Write]) -> Iterator[dict[str, Any]]:
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        if "ship" not in line and not any(i in line for i in pending):
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _text(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return str(content)


def _error_class(text: str) -> str:
    return re.sub(r"'[^']*'|\"[^\"]*\"|/\S+", "<v>", text)[:80]


def _safe_yaml(text: str) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


if __name__ == "__main__":
    sys.exit(main())
