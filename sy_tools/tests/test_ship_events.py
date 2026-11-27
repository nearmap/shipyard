"""GATE round and phase verbs: what they write, what they derive, and what they log as illegal."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from sy_tools import server, ship_events


def _finding(key: str, disposition: str = "accepted", impact: str = "breaking") -> dict[str, Any]:
    return {"root_cause_key": key, "disposition": disposition, "severity": "MED", "impact": impact}


def _round(*findings: dict[str, Any]) -> dict[str, Any]:
    return {"reviewed_sha": "abc", "verdict": "CHANGES", "findings": list(findings)}


@pytest.fixture
def state(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "ship-state.yaml"
    path.write_text(yaml.safe_dump({"phase_active": "GATE", "gate_rounds_total": 0, "gate_round_log": []}))
    monkeypatch.setattr(server.config, "get", lambda key: 3 if key == "ship.escalation.max_gate_rounds" else None)
    return path


def test_rounds_append_with_their_total_and_only_substantive_ones_spend_budget(state):
    p = str(state)
    server.ship_gate_round(p, entry=_round(_finding("a")), phase_checkpoint="c1")
    server.ship_gate_round(p, human_thread_fix=False, next_round_causes=["gate-finding"])
    server.ship_gate_round(p, entry=_round(_finding("low", impact="non-breaking")), phase_checkpoint="c2")
    out = server.ship_gate_round(p, human_thread_fix=False, next_round_causes=["gate-finding"])
    saved = yaml.safe_load(state.read_text())
    assert [e["round"] for e in saved["gate_round_log"]] == [1, 2] and saved["gate_rounds_total"] == 2
    assert saved["phase_checkpoint"] == "c2"
    assert out["budget"] == {"used": 1, "cap": 3, "left": 2, "base": 0}, "a LOW-only round spends no budget"
    assert out["next_pass_terminal"] is True, "the pass after an all-LOW round is terminal"
    assert out["illegal"] == []


def test_an_unfinished_round_still_spends_budget_and_a_raise_moves_the_floor():
    log = [{"round": 1, **_round(_finding("low", impact="non-breaking"))}, {"round": 2, **_round()}]
    derived = ship_events.derive({"gate_round_log": log, "gate_rounds_budget_base": 1}, cap=3)
    assert derived["budget"]["used"] == 1, "round 1 is under the base; round 2 is unfinished, so it counts"


def test_a_breaking_recurrence_needs_two_earlier_accepting_rounds():
    log = [
        {"round": 1, "human_thread_fix": False, **_round(_finding("k"), _finding("r", "rejected"))},
        {"round": 2, "human_thread_fix": False, **_round(_finding("k", "deferred"), _finding("r", "rejected"))},
        {"round": 3, **_round(_finding("k", "rejected"), _finding("r"), _finding("k2"))},
    ]
    derived = ship_events.derive({"gate_round_log": log}, cap=5)
    assert derived["breaking_recurrences"] == ["k"], "r was only ever rejected before; k2 is new"
    assert derived["prior_accepts"] == {"k": 2, "r": 1, "k2": 1}


def test_an_illegal_transition_is_logged_and_still_applied(state):
    p = str(state)
    out = server.ship_phase("dispatch", "BUILD", p)
    assert out["phase_active"] == "BUILD" and out["illegal"] == ["dispatch BUILD while GATE was never returned"]
    out = server.ship_phase("return", "gate", p)
    assert out["phase_active"] is None and out["illegal"] == ["return GATE while phase_active is 'BUILD'"]
    out = server.ship_gate_round(p, entry={"verdict": "PASS"})
    assert yaml.safe_load(state.read_text())["gate_rounds_total"] == 1, "the round lands despite the log"
    assert len(out["illegal"]) == 3, out["illegal"]
    assert len(yaml.safe_load(state.read_text())["transition_log"]) == 3, "one entry per illegal event"
