"""`scripts/validate.py::check_agent_mcp_allowlists` pins its own glob/wildcard/twin rules.

Nothing else in the suite imports `scripts/`; it is a standalone CLI, not a package. `ROOT` is
monkeypatched onto a throwaway `agents/` directory rather than the real one, so these cases can be
synthetic and the real 14 agents stay untouched.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# Loaded via importlib rather than a sys.path insert, so this module never mutates import
# resolution for the rest of the pytest session.
_spec = importlib.util.spec_from_file_location(
    "validate", Path(__file__).resolve().parents[2] / "scripts" / "validate.py"
)
assert _spec is not None and _spec.loader is not None
validate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate)


def _check(tmp_path: Path, tools_line: str, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "probe.md").write_text(
        f"---\nname: probe\ndescription: test\ntools: {tools_line}\n---\nbody\n", encoding="utf-8",
    )
    monkeypatch.setattr(validate, "ROOT", tmp_path)
    errors: list[str] = []
    validate.check_agent_mcp_allowlists(errors)
    return errors


@pytest.mark.parametrize(
    "tools_line",
    ["mcp__sy, mcp__plugin_sy_sy", "mcp__sy__*, mcp__plugin_sy_sy__*", "mcp__sy__set_*, mcp__plugin_sy_sy__set_*"],
)
def test_a_server_level_or_tool_name_glob_is_refused(tmp_path, monkeypatch, tools_line):
    errors = _check(tmp_path, tools_line, monkeypatch)
    assert errors, f"{tools_line!r} must be refused"


def test_a_glob_missing_the_double_underscore_is_also_refused(tmp_path, monkeypatch):
    errors = _check(tmp_path, "mcp__sy*, mcp__plugin_sy_sy*", monkeypatch)
    assert errors, "a glob shape that skips the documented `__` separator must still be refused"


def test_named_tools_with_both_deployment_twins_pass_clean(tmp_path, monkeypatch):
    errors = _check(tmp_path, "mcp__sy__set_status, mcp__plugin_sy_sy__set_status", monkeypatch)
    assert not errors, f"a legitimate named-tool twin pair must not be refused: {errors}"


def test_a_missing_twin_is_refused(tmp_path, monkeypatch):
    errors = _check(tmp_path, "mcp__sy__set_status", monkeypatch)
    assert errors, "an entry with no other-deployment twin listed must be refused"
