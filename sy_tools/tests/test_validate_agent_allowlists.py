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


def _check(
    tmp_path: Path,
    tools_line: str | None,
    monkeypatch: pytest.MonkeyPatch,
    name: str = "probe",
    trailing: str = "model: sonnet\n",
) -> list[str]:
    agents = tmp_path / "agents"
    agents.mkdir()
    tools = f"tools: {tools_line}\n" if tools_line is not None else ""
    # `model:` trails tools: because that is the layout a valueless `tools:` is mis-read as an allowlist
    # in: a pattern whose whitespace class crosses the newline captures the following frontmatter line.
    # `trailing=""` puts tools: last in the block instead, with nothing at all after it.
    fields = f"{tools}{trailing}"
    (agents / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: test\n{fields}---\nbody\n", encoding="utf-8",
    )
    monkeypatch.setattr(validate, "ROOT", tmp_path)
    errors: list[str] = []
    validate.check_agent_mcp_allowlists(errors)
    return errors


def _twins(*tools: str) -> str:
    """Render `tools` as a `tools:` value naming each under both deployment prefixes."""
    return ", ".join(f"mcp__{prefix}__{tool}" for tool in tools for prefix in ("sy", "plugin_sy_sy"))


@pytest.mark.parametrize(
    "tools_line",
    ["mcp__sy, mcp__plugin_sy_sy", "mcp__sy__*, mcp__plugin_sy_sy__*", "mcp__sy__set_*, mcp__plugin_sy_sy__set_*"],
)
def test_a_server_level_or_tool_name_glob_is_refused(tmp_path, monkeypatch, tools_line):
    # check_env twins ride along here and in the twin case below so the only rule left to break is the
    # one each case is about; without them every case would also trip the check_env-reachability check
    # and pass on that error instead.
    errors = _check(tmp_path, f"{tools_line}, {_twins('check_env')}", monkeypatch)
    assert errors, f"{tools_line!r} must be refused"


def test_a_glob_missing_the_double_underscore_is_also_refused(tmp_path, monkeypatch):
    errors = _check(tmp_path, f"mcp__sy*, mcp__plugin_sy_sy*, {_twins('check_env')}", monkeypatch)
    assert errors, "a glob shape that skips the documented `__` separator must still be refused"


def test_named_tools_with_both_deployment_twins_pass_clean(tmp_path, monkeypatch):
    errors = _check(tmp_path, _twins("set_status", "check_env"), monkeypatch)
    assert not errors, f"a legitimate named-tool twin pair must not be refused: {errors}"


def test_a_missing_twin_is_refused(tmp_path, monkeypatch):
    errors = _check(tmp_path, f"mcp__sy__set_status, {_twins('check_env')}", monkeypatch)
    assert any("twin" in error for error in errors), \
        f"an entry with no other-deployment twin listed must be refused: {errors}"


@pytest.mark.parametrize("tools_line", [
    "mcp__sy__set_status, mcp__plugin_sy_sy__set_status,",
    ", mcp__sy__set_status, mcp__plugin_sy_sy__set_status",
    "mcp__sy__set_status,, mcp__plugin_sy_sy__set_status",
])
def test_an_empty_allowlist_entry_is_refused(tmp_path, monkeypatch, tools_line):
    """A stray comma leaves an empty name every later check skips, so it is flagged rather than dropped mute."""
    errors = _check(tmp_path, tools_line, monkeypatch)
    assert any("empty entry" in error for error in errors), f"{tools_line!r} must be refused: {errors}"


@pytest.mark.parametrize("agent", ["ship-start", "ship-build", "ship-gate"])
@pytest.mark.parametrize("tool", ["memory_add", "memory_refute"])
def test_a_ship_worker_granted_a_memory_write_is_refused(tmp_path, monkeypatch, agent, tool):
    """Only the /sy:ship parent writes the user-global store; a worker relays a MEMORY_REFUTE candidate."""
    tools = f"mcp__sy__{tool}, mcp__plugin_sy_sy__{tool}"
    errors = _check(tmp_path, tools, monkeypatch, name=agent)
    assert any(tool in error for error in errors), f"{agent} must not be able to grant itself {tool}: {errors}"


@pytest.mark.parametrize("agent", ["ship-start", "ship-build", "ship-gate"])
def test_a_ship_worker_declaring_no_tools_at_all_is_refused(tmp_path, monkeypatch, agent):
    """An absent tools field inherits every tool, memory writes included, so it cannot pass silently."""
    errors = _check(tmp_path, None, monkeypatch, name=agent)
    assert errors, f"{agent} with no tools: line inherits the memory writes and must be refused"


@pytest.mark.parametrize("agent", ["ship-start", "ship-build", "ship-gate"])
@pytest.mark.parametrize("tools_line", ["", "   "])
def test_a_ship_worker_declaring_an_empty_tools_value_is_refused(tmp_path, monkeypatch, agent, tools_line):
    """An empty `tools:` value launches the worker with no tools at all rather than inheriting every tool, but
    either way it is not the explicit allowlist a ship worker must declare, so it is refused the same.

    It is the sharper case: the valueless `tools:` line reads as present, and a pattern whose whitespace
    class crosses the newline validates the *next* frontmatter line in its place.
    """
    errors = _check(tmp_path, tools_line, monkeypatch, name=agent)
    assert errors, f"{agent} with an empty tools: value declares no usable allowlist and must be refused"


_CHECK_ENV_TWINS = ("mcp__sy__check_env", "mcp__plugin_sy_sy__check_env")
_BLOCK_LIST = "".join(f"  - {tool}\n" for tool in _CHECK_ENV_TWINS).rstrip("\n")


@pytest.mark.parametrize("agent", ["sweep", "ship-build"])
@pytest.mark.parametrize("continuation", [
    f"\n{_BLOCK_LIST}",
    f"\n\n{_BLOCK_LIST}",
    f"\n  # both deployment prefixes\n{_BLOCK_LIST}",
    f"\n# both deployment prefixes\n{_BLOCK_LIST}",
    f"\n  [{', '.join(_CHECK_ENV_TWINS)}]",
], ids=[
    "block list", "blank line then block list", "indented comment then block list",
    "column-0 comment then block list", "flow sequence",
])
def test_a_multi_line_tools_value_is_refused_by_shape(tmp_path, monkeypatch, agent, continuation):
    """Every one of these puts nothing after `tools:` on its own line, so it reads as an absent field and every
    check below is skipped -- for a non-ship agent silently, however wrong the allowlist it hides. Each sibling
    here bypassed an earlier guard in turn, which is why the guard now recognises the one shape that means
    genuinely empty rather than enumerating the shapes that do not.

    check_env rides along under both prefixes so the form itself is the only thing left to refuse.
    """
    errors = _check(tmp_path, continuation, monkeypatch, name=agent)
    assert any("not genuinely empty" in error for error in errors), \
        f"{agent}'s multi-line tools: must be refused naming the shape: {errors}"


@pytest.mark.parametrize("continuation", ["", "\n  # inherited on purpose", "\n# inherited on purpose"],
                         ids=["bare", "indented comment", "column-0 comment"])
def test_a_genuinely_empty_tools_value_before_a_sibling_key_is_not_refused(tmp_path, monkeypatch, continuation):
    """Nothing follows `tools:` but comments and the next frontmatter key, so a non-ship agent inherits the
    default tool set as it always could; refusing this would report a block list that is not there."""
    errors = _check(tmp_path, continuation, monkeypatch, name="sweep")
    assert not errors, f"a genuinely empty tools: must not be read as a hidden allowlist: {errors}"


def test_a_genuinely_empty_tools_value_as_the_last_frontmatter_field_is_not_refused(tmp_path, monkeypatch):
    """With nothing at all after it there is no continuation to find, so the scan must run off the end clean
    rather than treat the block's closing delimiter as content."""
    errors = _check(tmp_path, "", monkeypatch, name="sweep", trailing="")
    assert not errors, f"a trailing empty tools: must not be read as a hidden allowlist: {errors}"


def test_a_non_ship_agent_declaring_no_tools_still_passes(tmp_path, monkeypatch):
    """The pin is scoped to the ship workers; other agents legitimately inherit the default tool set."""
    assert not _check(tmp_path, None, monkeypatch, name="sweep"), "the guard must not widen past ship workers"


def test_a_ship_worker_keeps_the_memory_read_tools(tmp_path, monkeypatch):
    """START reads memory back, so the guard must pin the write verbs alone, not the whole tool family."""
    tools = _twins("memory_list", "memory_search", "set-status", "assign", "check_env")
    assert not _check(tmp_path, tools, monkeypatch, name="ship-start"), "the read side must stay allowed"


@pytest.mark.parametrize("agent", ["ship-start", "ship-build", "ship-gate"])
def test_a_ship_worker_granted_exactly_its_declared_tracker_verbs_passes(tmp_path, monkeypatch, agent):
    verbs = sorted(validate.SHIP_WORKER_TRACKER_VERBS[agent])
    errors = _check(tmp_path, _twins("check_env", *verbs), monkeypatch, name=agent)
    assert not errors, f"{agent}'s own declared verb set must pass clean: {errors}"


@pytest.mark.parametrize("agent, dropped", [
    ("ship-start", "set-status"), ("ship-start", "assign"), ("ship-gate", "set-status"),
])
def test_a_ship_worker_short_a_declared_tracker_verb_is_refused(tmp_path, monkeypatch, agent, dropped):
    """An under-grant fails loudly rather than at the worker's first write, and names the verb."""
    verbs = sorted(validate.SHIP_WORKER_TRACKER_VERBS[agent] - {dropped})
    errors = _check(tmp_path, _twins("check_env", *verbs), monkeypatch, name=agent)
    assert any("missing" in error and dropped in error for error in errors), \
        f"{agent} short {dropped} must be refused naming it: {errors}"


@pytest.mark.parametrize("agent", ["ship-start", "ship-build", "ship-gate"])
def test_a_ship_worker_granted_a_tracker_verb_outside_its_set_is_refused(tmp_path, monkeypatch, agent):
    """An over-grant cannot ride in on an unrelated edit; the set is exact, not a floor."""
    verbs = [*sorted(validate.SHIP_WORKER_TRACKER_VERBS[agent]), "post-comment"]
    errors = _check(tmp_path, _twins("check_env", *verbs), monkeypatch, name=agent)
    assert any("extra" in error and "post-comment" in error for error in errors), \
        f"{agent} must not silently gain post-comment: {errors}"


def test_an_underscore_spelled_tracker_verb_does_not_satisfy_the_canonical_one(tmp_path, monkeypatch):
    """Tool names are exact: `set_status` reaches no tool, so it must read as the verb still missing."""
    errors = _check(tmp_path, _twins("check_env", "set_status", "assign"), monkeypatch, name="ship-start")
    assert any("missing" in error and "set-status" in error for error in errors), \
        f"an underscore spelling must not count as the hyphenated canonical verb: {errors}"


def test_an_explicit_allowlist_without_check_env_is_refused(tmp_path, monkeypatch):
    """Without it an agent asked about a credential shells out and leaks the value into the transcript."""
    errors = _check(tmp_path, "Read, Grep, Bash", monkeypatch)
    assert any("check_env" in error for error in errors), f"a check_env-less allowlist must be refused: {errors}"


def test_an_explicit_allowlist_with_check_env_twins_passes(tmp_path, monkeypatch):
    assert not _check(tmp_path, _twins("check_env"), monkeypatch), "check_env under both prefixes is the fix"


@pytest.mark.parametrize("entry", ["check_env", "mcp__other__check_env"])
def test_an_unprefixed_or_foreign_check_env_does_not_satisfy_the_grant(tmp_path, monkeypatch, entry):
    """A tail-matching read counts a bare or foreign-server name as the grant; neither reaches the `sy` tool."""
    errors = _check(tmp_path, entry, monkeypatch)
    assert any("check_env" in error for error in errors), f"{entry!r} grants no check_env: {errors}"


def test_an_unprefixed_tracker_verb_does_not_satisfy_a_ship_workers_declared_set(tmp_path, monkeypatch):
    """Same tail-matching hazard on the exact-verb-set check: `assign` alone reaches no tool."""
    errors = _check(tmp_path, f"assign, {_twins('check_env', 'set-status')}", monkeypatch, name="ship-start")
    assert any("missing" in error and "assign" in error for error in errors), \
        f"an unprefixed verb must read as still missing: {errors}"
