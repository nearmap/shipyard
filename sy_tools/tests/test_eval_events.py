"""`eval_events`' own assertions, plus the degradation that keeps a broken config off the hot path."""
from __future__ import annotations

import json

import pytest

from sy_tools import config, eval_events


def test_a_namespaced_agent_type_normalises_to_its_bare_name():
    assert eval_events.normalize_agent_type("sy:gate") == "gate"
    assert eval_events.normalize_agent_type(None) == "main"


def test_detail_reports_only_what_the_triggering_tool_carries():
    """Trigger evals need the skill or subagent named; every other tool contributes no detail."""
    assert eval_events.detail("Skill", {"skill": "ship"}) == {"skill": "ship"}
    assert eval_events.detail("Agent", {"subagent_type": "gate", "description": "review"}) == {
        "subagent_type": "gate",
        "description": "review",
    }
    assert eval_events.detail("Read", {"file_path": "a.py"}) == {}


def test_an_event_with_no_session_id_is_not_recordable():
    """The session id is the ledger's only key, so a payload without one has nowhere to go."""
    assert eval_events.build_event({"session_id": ""}) is None


def test_the_ledger_keys_on_session_id_alone_never_on_the_caller_s_cwd(tmp_path, monkeypatch):
    """A build or gate subagent runs in a worktree, so a cwd-keyed ledger would split one run in two."""
    monkeypatch.setattr(eval_events, "EVENTS_ROOT", tmp_path / "eval-events")
    # Deliberately different cwd per call.
    eval_events.record({
        "session_id": "s1", "cwd": "/repo", "hook_event_name": "PreToolUse",
        "agent_type": "sy:gate", "tool_name": "Skill", "tool_input": {"skill": "ship"},
    })
    eval_events.record({
        "session_id": "s1", "cwd": "/repo-worktrees/branch", "hook_event_name": "Stop",
    })
    lines = eval_events.events_path("s1").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2, lines
    first = json.loads(lines[0])
    assert first["tool"] == "Skill", first
    assert first["detail"] == {"skill": "ship"}, first
    assert first["agent_type"] == "gate", first
    second = json.loads(lines[1])
    assert second["hook_event"] == "Stop", second
    assert "tool" not in second, second


def test_enabled_degrades_to_off_when_the_config_cannot_be_resolved(tmp_path, monkeypatch):
    """An unresolvable config must leave the log off, never crash a hook that fires on every tool call."""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))  # exists, but is no checkout
    config.reset_cache()
    try:
        # Refused first so this cannot pass vacuously: a crashed `PreToolUse` hook is fail-open, which
        # takes the gate around it quiet too.
        with pytest.raises(config.ConfigError):
            config.get("debug.evals")
        assert eval_events.enabled() is False, "an unresolvable config must leave the event log off"
    finally:
        config.reset_cache()


@pytest.mark.parametrize("configured", [True, False], ids=["on", "off"])
def test_enabled_reads_debug_evals_from_the_resolved_config(tmp_path, monkeypatch, configured):
    """Off by default is the whole cost argument, so the read has to be wired to the real key."""
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    (home / ".shipyard").mkdir(parents=True)
    (repo / ".shipyard").mkdir(parents=True)
    (repo / ".shipyard" / "config.json").write_text(json.dumps({"debug": {"evals": configured}}), encoding="utf-8")
    monkeypatch.setattr(config, "repo_root", lambda: repo)
    config.reset_cache()
    try:
        assert eval_events.enabled() is configured, f"debug.evals={configured} must decide the log"
    finally:
        config.reset_cache()
