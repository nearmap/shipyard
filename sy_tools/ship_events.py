"""Event verbs over a `/sy:ship` run's state: one GATE round, and one phase dispatch or return.

Each verb takes the loaded state mapping, returns the fields to write, and logs any transition the
contract forbids into `transition_log` rather than refusing it, so a state file stays readable by a
build that predates these verbs. The derived facts a GATE worker used to work out from prose (budget,
recurrences, terminal pass) come back from `derive`.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

PHASES = ("START", "BUILD", "GATE")
ACCEPTS = frozenset({"accepted", "deferred"})
NEXT_ROUND_CAUSES = frozenset({"gate-finding", "ci-fix", "automated-reviewer", "human-thread", "drift", "rebase"})
ROUND_KEYS = ("reviewed_sha", "verdict", "findings")


def record_round(
    state: dict[str, Any],
    entry: dict[str, Any],
    phase_checkpoint: Any,
    standards_reviewed_sha: str | None,
) -> dict[str, Any]:
    """Fields for one GATE pass: its log entry appended and `gate_rounds_total` bumped in the same write."""
    log = _log(state)
    total = int(state.get("gate_rounds_total") or 0)
    illegal = [f"round entry missing {k}" for k in ROUND_KEYS if k not in entry]
    if not isinstance(state.get("gate_round_log") or [], list):
        illegal.append("gate_round_log was not a list and is restarted")
    if state.get("phase_active") != "GATE":
        illegal.append(f"GATE round recorded while phase_active is {state.get('phase_active')!r}")
    if total != len(log):
        illegal.append(f"gate_rounds_total {total} disagreed with {len(log)} logged rounds before this one")
    round_no = max(total, len(log)) + 1
    fields: dict[str, Any] = {
        "gate_round_log": [*log, {"round": round_no, "terminal_pass": False, **entry}],
        "gate_rounds_total": round_no,
        "phase_checkpoint": phase_checkpoint,
    }
    if standards_reviewed_sha:
        fields["standards_reviewed_sha"] = standards_reviewed_sha
    return _with_log(state, fields, "gate_round", illegal)


def finish_round(state: dict[str, Any], human_thread_fix: bool, next_round_causes: list[str]) -> dict[str, Any]:
    """Fields closing the last logged round: whether it pushed a human-thread fix and why another pass follows."""
    log = _log(state)
    illegal = [f"unknown next_round_cause {c!r}" for c in next_round_causes if c not in NEXT_ROUND_CAUSES]
    if not log or not isinstance(log[-1], dict):
        return _with_log(state, {}, "gate_round_finish", ["finish with no round logged", *illegal])
    if "human_thread_fix" in log[-1]:
        illegal.append(f"round {log[-1].get('round')} finished twice")
    log[-1] = {**log[-1], "human_thread_fix": human_thread_fix, "next_round_causes": next_round_causes}
    return _with_log(state, {"gate_round_log": log}, "gate_round_finish", illegal)


def phase(state: dict[str, Any], action: str, name: str) -> dict[str, Any]:
    """Fields for stamping `phase_active` before a worker dispatch, or clearing it when its return is processed."""
    current = state.get("phase_active")
    illegal = [] if name in PHASES else [f"phase {name!r} outside {PHASES}"]
    if action == "dispatch":
        if current is not None:
            illegal.append(f"dispatch {name} while {current} was never returned")
        return _with_log(state, {"phase_active": name}, f"dispatch {name}", illegal)
    if current != name:
        illegal.append(f"return {name} while phase_active is {current!r}")
    return _with_log(state, {"phase_active": None}, f"return {name}", illegal)


def derive(state: dict[str, Any], cap: int) -> dict[str, Any]:
    """What the next GATE decision reads: budget, recurring root causes, and whether the next pass is terminal.

    Only substantive rounds count against the cap: one that accepted a breaking finding or pushed a
    human-thread fix. A round not yet finished counts, so an interrupted round never frees budget.
    """
    log = [e for e in _log(state) if isinstance(e, dict)]
    base = int(state.get("gate_rounds_budget_base") or 0)
    used = sum(1 for n, e in enumerate(log, 1) if n > base and _substantive(e))
    last = log[-1] if log else {}
    earlier = _accept_rounds(log[:-1])
    breaking_recurrences = sorted(
        {
            f["root_cause_key"]
            for f in _findings(last)
            if f.get("impact", "breaking") == "breaking" and earlier[f["root_cause_key"]] >= 2
        }
    )
    return {
        "rounds_total": int(state.get("gate_rounds_total") or 0),
        "budget": {"used": used, "cap": cap, "left": max(cap - used, 0), "base": base},
        "prior_accepts": dict(_accept_rounds(log).most_common()),
        "breaking_recurrences": breaking_recurrences,
        "next_pass_terminal": _all_low(last),
    }


def _substantive(entry: dict[str, Any]) -> bool:
    if entry.get("human_thread_fix", True):
        return True
    findings = _findings(entry, keyed=False)
    return any(f.get("disposition") == "accepted" and f.get("impact", "breaking") == "breaking" for f in findings)


def _all_low(entry: dict[str, Any]) -> bool:
    accepted = [f for f in _findings(entry, keyed=False) if f.get("disposition") in ACCEPTS]
    return bool(
        accepted
        and not entry.get("terminal_pass", True)
        and not entry.get("human_thread_fix", True)
        and all(f.get("impact", "breaking") == "non-breaking" for f in accepted)
    )


def _accept_rounds(log: list[dict[str, Any]]) -> Counter[str]:
    """How many rounds recorded each root cause with an accept disposition."""
    counts: Counter[str] = Counter()
    for e in log:
        counts.update({f["root_cause_key"] for f in _findings(e) if f.get("disposition") in ACCEPTS})
    return counts


def _log(state: dict[str, Any]) -> list[Any]:
    log = state.get("gate_round_log") or []
    return list(log) if isinstance(log, list) else []


def _findings(entry: dict[str, Any], keyed: bool = True) -> list[dict[str, Any]]:
    """An entry's findings, by default only the keyed ones a recurrence can match; older hand-written logs vary."""
    return [f for f in entry.get("findings") or [] if isinstance(f, dict) and (f.get("root_cause_key") or not keyed)]


def _with_log(state: dict[str, Any], fields: dict[str, Any], event: str, illegal: list[str]) -> dict[str, Any]:
    if not illegal:
        return fields
    at = datetime.now(UTC).isoformat(timespec="seconds")
    entry = {"at": at, "event": event, "illegal": illegal}
    return {**fields, "transition_log": [*(state.get("transition_log") or []), entry]}
