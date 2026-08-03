"""`sy_tools.config` must resolve byte-identically to the shipped CLI resolver.

The MCP deployment reimplements resolution rather than importing it, so parity is a claim that
has to be tested, not asserted. Both resolvers run over the same fixture layer chain and their
resolved values and provenance are compared key for key.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from sy_tools import config

PLUGIN_ROOT = Path(__file__).resolve().parents[2]

FIXTURE_COLUMNS = {
    "backlog": "Fixture Backlog", "ready": "Fixture Ready", "in_progress": "Fixture In Progress",
    "in_review": "Fixture In Review", "done": "Fixture Done",
}
FIXTURE_LAYER = {
    "$schema": "https://raw.githubusercontent.com/nearmap/shipyard/main/config/schema.json",
    "columns": FIXTURE_COLUMNS,
    "models": {"agents": {"sweep": {"model": "opus"}}},
    "transcript": {"attach": True},
    "redaction": {"extra_words": ["bearer"]},
}


def _cli(root: Path, *args: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "scripts" / "sy_config.py"), *args],
        cwd=root, capture_output=True, text=True, check=False,
        env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT)},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.fixture
def fixture_repo(tmp_path, monkeypatch):
    """A throwaway git checkout carrying one committed config layer, with both resolvers pointed at it."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".shipyard").mkdir()
    (tmp_path / ".shipyard" / "config.json").write_text(json.dumps(FIXTURE_LAYER), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN_ROOT))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # keep the user-global layer out of the fixture
    monkeypatch.chdir(tmp_path)
    config.reload()
    yield tmp_path
    config.reload()


def test_resolution_matches_the_cli_resolver(fixture_repo):
    expected = _cli(fixture_repo, "show", "--json")
    values, provenance = config.resolve()
    assert values == expected["values"], "resolved values must match sy_config.py show --json exactly"
    assert provenance == expected["provenance"], "each key must be attributed to the same layer"


def test_layer_precedence_and_derived_defaults(fixture_repo):
    assert config.get("columns.ready") == "Fixture Ready", "a repo layer must win over the shipped default"
    assert config.get("transcript.attach") is True
    assert config.get("limits.max_depth_agents") == 3, "an unset key must fall through to the shipped default"
    assert config.get("worktree.root") == str(fixture_repo.parent / f"{fixture_repo.name}-worktrees")
    assert config.resolve()[1]["worktree.root"] == "derived-default"
    assert config.get("scratch.dir") == str(Path.home() / ".claude" / "shipyard" / "scratch")
    assert config.resolve()[1]["scratch.dir"] == "derived-default"


def test_scratch_dir_creates_under_the_resolved_root_and_refuses_an_escape(fixture_repo):
    """An identifier that resolves anywhere but strictly inside the root is refused, root included.

    `"."` and `"./"` are the load-bearing cases: they have no path parts, so a string-shaped guard
    admits them and hands back the scratch root itself — shared by every identifier, and deleted by
    the first caller that cleans up what it was given.
    """
    root = Path(str(config.get("scratch.dir")))
    created = config.scratch_dir("AM-1")
    assert created == root / "AM-1"
    assert created.is_dir(), "scratch_dir must create the directory it returns"

    outside = fixture_repo / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    escapes = ("", ".", "./", " ", "..", "../elsewhere", "a/../../b", "link/x", "a\0b", str(fixture_repo))
    for escape in escapes:
        with pytest.raises(config.ConfigError, match="stays inside the resolved scratch root"):
            config.scratch_dir(escape)
    assert not any(outside.iterdir()), "a symlink inside the scratch root must not be followed out of it"


def test_scratch_dir_refuses_the_same_identifiers_as_the_cli_resolver(fixture_repo):
    """The guard is duplicated across the two deployments, so its rejections are compared, not trusted."""
    for identifier in (".", "..", "a/../../b"):
        proc = subprocess.run(
            [sys.executable, str(PLUGIN_ROOT / "scripts" / "sy_config.py"), "scratch-dir", identifier],
            cwd=fixture_repo, capture_output=True, text=True, check=False,
            env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT)},
        )
        assert proc.returncode != 0, f"the CLI accepted {identifier!r}: {proc.stdout}"
        assert "stays inside the resolved scratch root" in proc.stderr, proc.stderr
        with pytest.raises(config.ConfigError, match="stays inside the resolved scratch root"):
            config.scratch_dir(identifier)


def test_agent_binding_matches_the_cli_resolver(fixture_repo):
    for agent in ("sweep", "gate", "ship-build", "img-inspector"):
        assert config.agent_binding(agent) == _cli(fixture_repo, "agent", agent, "--json"), agent


def test_no_agent_resolves_below_its_shipped_floor(fixture_repo):
    """A floor is a quality floor, not a cost dial: resolution may raise one, never lower it."""
    tiers = config.resolve()[0]["models"]["tiers"]
    floors = json.loads((PLUGIN_ROOT / "config" / "floors.json").read_text(encoding="utf-8"))
    order = config.MODEL_ORDER
    for agent, floor in floors.items():
        resolved = config.agent_binding(agent)["model"]
        minimum = tiers.get(floor["min_model"], floor["min_model"])
        assert order.index(resolved) >= order.index(minimum), f"{agent} resolved below its floor"


def test_unknown_key_raises_but_an_explicit_default_does_not(fixture_repo):
    with pytest.raises(config.ConfigError, match="unknown config key"):
        config.get("columns.nonexistent")
    assert config.get("columns.nonexistent", default=None) is None


def test_credential_shaped_keys_are_refused(fixture_repo):
    for key in ("tracker_config.token", "tracker_config.api_key", "some.password"):
        with pytest.raises(config.ConfigError, match="credential-shaped"):
            config.get(key)


def test_validate_reports_a_missing_required_key(fixture_repo, monkeypatch):
    broken = {**FIXTURE_LAYER, "columns": {**FIXTURE_COLUMNS, "ready": ""}}
    (fixture_repo / ".shipyard" / "config.json").write_text(json.dumps(broken), encoding="utf-8")
    config.reload()
    assert any("columns.ready is required" in e for e in config.validate())


def test_reload_picks_up_an_edit_and_reports_the_change(fixture_repo):
    before = config.fingerprint()
    changed = {**FIXTURE_LAYER, "columns": {**FIXTURE_COLUMNS, "done": "Shipped"}}
    (fixture_repo / ".shipyard" / "config.json").write_text(json.dumps(changed), encoding="utf-8")
    summary = config.reload()
    assert summary["changed"] is True
    assert summary["previous_fingerprint"] == before
    assert config.get("columns.done") == "Shipped"
    assert config.reload()["changed"] is False, "a reload with no edit must report no change"
