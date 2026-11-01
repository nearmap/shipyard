"""`scripts/validate.py`'s own unit-testable rules: the agent deny-lists, and frontmatter extraction.

Nothing else in the suite imports `scripts/`; it is a standalone CLI, not a package, and it is loaded
once here for every case below. `ROOT` is monkeypatched onto a throwaway `agents/` directory rather
than the real one, so the deny-list cases can be synthetic and the real agents stay untouched.
"""
from __future__ import annotations

from collections.abc import Callable
import importlib.util
import inspect
import json
from pathlib import Path

import mcp
import pytest

from sy_tools import server

# Loaded via importlib rather than a sys.path insert, so this module never mutates import
# resolution for the rest of the pytest session.
_spec = importlib.util.spec_from_file_location(
    "validate", Path(__file__).resolve().parents[2] / "scripts" / "validate.py"
)
assert _spec is not None and _spec.loader is not None
validate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate)

REAL_ROOT = Path(__file__).resolve().parents[2]


_DENY_SERVER_TOOLS = ("assign", "check_env", "get-issue", "memory_add", "memory_list", "set-status")
_POST_COMMENT = '@mcp.tool(name="post-comment")\nasync def g() -> None: ...\n'
_DENY_GRANTS = {
    "probe": frozenset({"Read", "Grep", "check_env"}),
    "ship-start": frozenset({"Read", "Write", "assign", "check_env", "memory_list", "set-status"}),
}


def _deny_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    grants: dict[str, frozenset[str]] | None = None,
    edit: Callable[[str], str] = lambda frontmatter: frontmatter,
    server_suffix: str = "",
) -> list[str]:
    """Write each agent's canonical deny line, pass the probe's frontmatter through `edit`, and return the errors.

    `server_suffix` lands in the server fixture after the lines are rendered: a registration the server gained since.
    """
    grants = _DENY_GRANTS if grants is None else grants
    monkeypatch.setattr(validate, "ROOT", tmp_path)
    monkeypatch.setattr(validate, "AGENT_GRANTS", grants)
    monkeypatch.setattr(validate, "EXPECTED_AGENTS", set(grants))
    (tmp_path / "agents").mkdir()
    for stem, granted in grants.items():
        frontmatter = f"name: {stem}\ndisallowedTools: {validate._deny_line(granted, frozenset(_DENY_SERVER_TOOLS))}\n"
        if stem == "probe":
            frontmatter = edit(frontmatter)
        brief = f"---\n{frontmatter}model: sonnet\n---\nbody\n"
        (tmp_path / "agents" / f"{stem}.md").write_text(brief, encoding="utf-8")
    (tmp_path / "sy_tools").mkdir()
    (tmp_path / "sy_tools" / "server.py").write_text(
        "".join(f'@mcp.tool(name="{tool}")\nasync def f() -> None: ...\n\n' for tool in _DENY_SERVER_TOOLS)
        + server_suffix,
        encoding="utf-8",
    )
    errors: list[str] = []
    validate.check_agent_denylists(errors)
    return errors


def test_canonical_deny_lines_pass(tmp_path, monkeypatch):
    errors = _deny_check(tmp_path, monkeypatch)
    assert not errors, f"deny lines rendered from AGENT_GRANTS must pass: {errors}"


@pytest.mark.parametrize("edit, expected", [
    (lambda fm: fm.replace(", mcp__sy__memory_add", ""), "missing ['mcp__sy__memory_add']"),
    (lambda fm: fm.replace("mcp__sy__memory_add", "mcp__sy__memory_ad"), "missing ['mcp__sy__memory_add']"),
    (lambda fm: fm.replace("disallowedTools: ", "disallowedTools: Grep, "), "unexpected ['Grep']"),
    (lambda fm: fm + "tools: Read, Grep\n", "declares `tools:`"),
    (lambda fm: fm.partition("disallowedTools")[0], "found 0"),
    (lambda fm: fm + "  suffix\n", "continuation"),
    (lambda fm: fm + "\n  suffix\n", "continuation"),
], ids=["missing deny", "misspelled deny", "deny stripping a grant", "tools field", "no deny line",
        "folded continuation", "blank-line folded continuation"])
def test_a_deny_line_drifted_from_its_grants_is_refused(tmp_path, monkeypatch, edit, expected):
    """Claude Code ignores a deny name that matches nothing, so a typo is an over-grant the line must not hide."""
    errors = _deny_check(tmp_path, monkeypatch, edit=edit)
    assert any(expected in error for error in errors), f"expected {expected!r}: {errors}"


@pytest.mark.parametrize("stem, grant_edit, expected", [
    ("probe", lambda g: g | {"Raed"}, "['Raed']"),
    ("probe", lambda g: g - {"check_env"}, "grants no 'check_env'"),
    ("ship-start", lambda g: g | {"memory_add"}, "['memory_add']"),
    ("ship-start", lambda g: g - {"assign"}, "missing ['assign']"),
    ("ship-start", lambda g: g | {"get-issue"}, "extra ['get-issue']"),
], ids=["outside the universe", "no check_env", "ship worker memory write", "ship worker short a verb",
        "ship worker extra verb"])
def test_a_grant_breaking_an_invariant_is_refused(tmp_path, monkeypatch, stem, grant_edit, expected):
    grants = {**_DENY_GRANTS, stem: grant_edit(_DENY_GRANTS[stem])}
    errors = _deny_check(tmp_path, monkeypatch, grants=grants)
    assert any(expected in error and stem in error for error in errors), f"expected {expected!r}: {errors}"


def test_a_server_tool_denied_nowhere_is_refused(tmp_path, monkeypatch):
    """A new `sy` tool reaches every agent until each deny line names it."""
    errors = _deny_check(tmp_path, monkeypatch, server_suffix=_POST_COMMENT)
    assert any("mcp__plugin_sy_sy__post-comment" in error for error in errors), \
        f"a server tool no deny line names must be refused: {errors}"


def test_an_indented_server_tool_is_still_governed(tmp_path, monkeypatch):
    suffix = "if True:\n" + "".join(f"    {line}\n" for line in _POST_COMMENT.splitlines())
    errors = _deny_check(tmp_path, monkeypatch, server_suffix=suffix)
    assert any("mcp__plugin_sy_sy__post-comment" in error for error in errors), \
        f"a decorator nested under a block still registers a tool: {errors}"


def test_an_unnamed_server_tool_is_refused(tmp_path, monkeypatch):
    errors = _deny_check(tmp_path, monkeypatch, server_suffix="@mcp.tool()\nasync def g() -> None: ...\n")
    assert any("no `@mcp.tool(name=...)`" in error for error in errors), \
        f"a registration without `name=` must void the read: {errors}"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_the_server_read_matches_the_live_registry():
    """The deny lines are rendered from a source read of server.py, so it must agree with what the server serves."""
    async with mcp.Client(server.mcp) as client:
        live = {tool.name for tool in (await client.list_tools()).tools}
    read = validate._sy_server_tools()
    assert read == live, f"source read {sorted(read)} vs live {sorted(live)}"


def test_the_deny_line_orders_builtins_then_both_sy_prefixes(monkeypatch):
    monkeypatch.setattr(validate, "GOVERNED_BUILTINS", frozenset({"Read", "Grep", "Bash"}))
    line = validate._deny_line(frozenset({"Read", "check_env"}), frozenset({"check_env", "set-status"}))
    assert line == "Bash, Grep, mcp__plugin_sy_sy__set-status, mcp__sy__set-status", line


def test_the_grant_checks_are_registered_in_main():
    """A check nothing calls protects nothing, and `main()` is its only caller."""
    source = inspect.getsource(validate.main)
    for check in ("check_agent_denylists(errors)", "check_docs_mcp_citations(errors)"):
        assert check in source, f"{check} must be registered in main()"


_EMBEDDED_RULE = (
    "---\n"
    "name: tighten\n"
    "description: >-\n"
    "  PROACTIVE --- tighten one piece of already-written text before it is sent.\n"
    "disable-model-invocation: true\n"
    "---\n"
    "body\n"
)
"""A skill whose `description` value carries a literal `---`, with a real key on a line below it.

The shape a split on the bare `---` substring mis-cuts: the boundary lands inside the description, so
every key below it -- `disable-model-invocation` here -- falls outside the block a containment pin then
reads as clean, and the field read comes back truncated at the inner rule.
"""


def test_a_frontmatter_value_carrying_a_literal_rule_keeps_the_keys_below_it_in_the_block():
    """The containment pin's whole worth: a key it must catch cannot fall outside what it is handed."""
    block = validate._frontmatter_block(_EMBEDDED_RULE)
    assert "disable-model-invocation: true" in block, f"a key below an inner `---` fell out of the block: {block!r}"
    assert "body" not in block, f"extraction ran past the closing delimiter into the file body: {block!r}"


def test_a_field_read_spans_a_block_scalar_carrying_a_literal_rule():
    """A value cut at an inner rule reads as a shorter description, which is how an opening-word pin goes vacuous."""
    value = validate._frontmatter_field(_EMBEDDED_RULE, "description")
    assert value.strip().endswith("before it is sent."), f"the value was cut at the inner `---`: {value!r}"


@pytest.mark.parametrize("text", ["name: tighten\n---\nbody\n", "---\nname: tighten\nbody\n"],
                         ids=["no opening delimiter", "no closing delimiter"])
def test_a_file_carrying_no_frontmatter_block_extracts_to_empty(text):
    """Empty rather than a raise or a partial: `frontmatter()` reports both delimiter faults against the path, and
    a caller here must not read an unclosed file's body as if it were the block."""
    assert validate._frontmatter_block(text) == "", f"a file with no frontmatter block must extract to empty: {text!r}"


_CLAUSE = "## Net-new agent-facing text\n"
_CARRIER = "- **Carrier** --- the PR body, one line per addition.\n"
_CITATION = (
    "- Net-new agent-facing text in the diff carries one justification line per addition;\n"
    "  see immutable-gate.md for the scope and the keep/cut test.\n"
)
# The heading that closes the clause's section. Present in every fixture the carrier leg is meant to
# reach, because that leg is scoped between the clause heading and the next `## ` one.
_TERM = "## Fix cycle\n"


def _gate_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_ref: str,
    reviewer: str,
) -> list[str]:
    """Build the two-file tree `check_gate_justification` reads and return its errors."""
    (tmp_path / "skills" / "ship" / "references").mkdir(parents=True)
    (tmp_path / "agents").mkdir()
    (tmp_path / "skills" / "ship" / "references" / "immutable-gate.md").write_text(gate_ref, encoding="utf-8")
    (tmp_path / "agents" / "gate.md").write_text(reviewer, encoding="utf-8")
    monkeypatch.setattr(validate, "ROOT", tmp_path)
    errors: list[str] = []
    validate.check_gate_justification(errors)
    return errors


def test_a_tree_carrying_the_clause_its_carrier_and_the_citation_passes(tmp_path, monkeypatch):
    errors = _gate_check(tmp_path, monkeypatch, _CLAUSE + _CARRIER + _TERM, _CITATION)
    assert not errors, f"a complete clause plus reviewer citation must pass: {errors}"


def test_a_gate_reference_without_the_clause_is_refused(tmp_path, monkeypatch):
    errors = _gate_check(tmp_path, monkeypatch, "## Fix cycle\n", _CITATION)
    assert any("Net-new agent-facing text clause" in error for error in errors), \
        f"a reference that dropped the clause must be refused: {errors}"


def test_a_clause_without_its_named_carrier_is_refused(tmp_path, monkeypatch):
    errors = _gate_check(tmp_path, monkeypatch, _CLAUSE + _TERM, _CITATION)
    assert any("carrier" in error for error in errors), \
        f"a clause with nowhere for a justification to land must be refused: {errors}"


def test_a_reviewer_that_never_names_the_obligation_is_refused(tmp_path, monkeypatch):
    errors = _gate_check(
        tmp_path, monkeypatch, _CLAUSE + _CARRIER + _TERM, "- review the diff. See immutable-gate.md.\n"
    )
    assert any("must name the net-new agent-facing text obligation" in error for error in errors), \
        f"an obligation the reviewer's brief never names never runs: {errors}"


def test_a_reviewer_naming_the_obligation_without_the_reference_is_refused(tmp_path, monkeypatch):
    errors = _gate_check(
        tmp_path, monkeypatch, _CLAUSE + _CARRIER + _TERM, "- Net-new agent-facing text is justified.\n"
    )
    assert any("must cite immutable-gate.md" in error for error in errors), \
        f"the duty without its reference leaves the keep/cut test to be invented: {errors}"


@pytest.mark.parametrize("restating", ["gate_ref", "reviewer"])
@pytest.mark.parametrize("cut_test", validate.CUT_TESTS)
def test_either_file_restating_a_context_economy_cut_test_is_refused(tmp_path, monkeypatch, restating, cut_test):
    # The literals come from the constant rather than being retyped here: a second copy in the test is
    # exactly the drift the check exists to stop.
    files = {"gate_ref": _CLAUSE + _CARRIER + _TERM, "reviewer": _CITATION}
    files[restating] += f"{cut_test}\n"
    errors = _gate_check(tmp_path, monkeypatch, files["gate_ref"], files["reviewer"])
    assert any("restates a context-economy cut test" in error for error in errors), \
        f"{restating} restating {cut_test!r} must be refused: {errors}"


def test_a_carrier_phrase_broken_across_a_line_wrap_still_satisfies_the_pin(tmp_path, monkeypatch):
    """Prose re-wraps on every edit. A containment pin on a mid-sentence phrase would go vacuous the first
    time the line reflowed, while the sentence still said exactly what the pin exists to check."""
    wrapped = "- **Carrier** --- the PR body, one\n  line per addition.\n"
    errors = _gate_check(tmp_path, monkeypatch, _CLAUSE + wrapped + _TERM, _CITATION)
    assert not errors, f"a re-wrapped carrier phrase must still count as named: {errors}"


def test_a_hollowed_clause_is_refused_even_when_the_carrier_phrase_survives_elsewhere(tmp_path, monkeypatch):
    """A whole-file containment pin is the vacuity class this check exists to close: the section could be
    gutted to nothing and the leg would still read green off an unrelated section's prose."""
    gate_ref = _CLAUSE + "- **Carrier** --- the PR body.\n## Fix cycle\n- Justify with one line per addition.\n"
    errors = _gate_check(tmp_path, monkeypatch, gate_ref, _CITATION)
    assert any("carrier" in error for error in errors), \
        f"a hollowed clause must be refused however the phrase reads elsewhere in the file: {errors}"


def test_a_clause_section_with_no_terminating_heading_is_reported_not_scanned_to_the_end_of_the_file(
    tmp_path, monkeypatch
):
    """With nothing closing the section the carrier pin has no right boundary, so it would widen back to
    the whole file — the vacuity this scoping exists to close. Unbounded is reported, not assumed green."""
    errors = _gate_check(tmp_path, monkeypatch, _CLAUSE + _CARRIER, _CITATION)
    assert any("boundary could not be located" in error for error in errors), \
        f"a clause section no heading closes must be reported loudly: {errors}"


def test_a_renamed_terminating_heading_still_bounds_the_section_rather_than_voiding_the_pin(tmp_path, monkeypatch):
    """The boundary is the next `## ` heading, whatever it is called: a terminator pinned by name would let
    a rename widen the section until an unrelated section's prose satisfied the carrier pin."""
    gate_ref = _CLAUSE + "- **Carrier** --- the PR body.\n## Justification cycle\n" + _CARRIER
    errors = _gate_check(tmp_path, monkeypatch, gate_ref, _CITATION)
    assert any("must name the carrier" in error for error in errors), \
        f"a hollowed clause must be refused however the following section is titled: {errors}"


def test_a_poller_invocation_wrapped_after_the_verb_is_reported_not_silently_matched(tmp_path, monkeypatch):
    """A newline-crossing selector capture is worse than no match: the flag-order leg then passes on a token
    picked out of the following line, so the argv rule reads clean against text it never checked."""
    (tmp_path / "skills").mkdir()
    site = "skills/probe.md"
    (tmp_path / site).write_text(
        "Wait with `ci_poll.sh poll\n--repo owner/name --head <sha>`.\n", encoding="utf-8",
    )
    monkeypatch.setattr(validate, "ROOT", tmp_path)
    monkeypatch.setattr(validate, "POLLER_CALL_SITES", (site,))
    errors: list[str] = []
    validate.check_poller_argv(errors)
    assert any("must invoke the shared poller by name" in error for error in errors), \
        f"a wrapped invocation must be reported as no invocation, not matched across the newline: {errors}"


_PINS = validate.HUMAN_TEXT_PINS
# The pinned sentences are read out of the checker's own constant, never retyped: a second copy here would
# agree with the first forever and the pin would stop tracking the tree it is meant to police.
_STEP_ONE = (
    f"{_PINS['signoff_pass']}\n"
    "Send that summary as direct text first, in full.\n"
    f"{_PINS['signoff_option']}\n"
)
_GROUND_RULES = f"## Ground rules\nPreserve every fact. {_PINS['short_text_floor']}\n"
_THE_READER = (
    "## The reader\n"
    "Resolve `text.reader` with `get_config` and write to the reader it names.\n"
    f"- **The floor.** The setting {_PINS['reader_floor']}.\n"
)
_PROTECT = f"## Protect\nNever drop an anchor. {_PINS['companion_half']}\n"
_TIGHTEN_REPORT = "## Report\nName what changed and by how much.\n"
_SCHEMA_TEXT = {"type": "object", "additionalProperties": False, "properties": {"reader": {"type": "string"}}}


def _spec_md(step_one: str = _STEP_ONE, step_two: str = "### Step 2 - post the plan\nPost it once.\n") -> str:
    return (
        "## 7. Capture the plan as the highest version\n"
        "### Step 1 - ask for sign-off on the judgment\n"
        f"{step_one}{step_two}"
    )


def _tighten_md(sections: tuple[str, ...] = (_GROUND_RULES, _THE_READER, _PROTECT, _TIGHTEN_REPORT)) -> str:
    return "".join(sections)


def _interaction_md(question_body: str = f"Ask through the tool. {_PINS['escape_hatch']}\n") -> str:
    return (
        "## Status\nA statement needing no answer.\n"
        f"## Question\n{question_body}"
        "## Action needed\nSay what you need.\n"
    )


def _server_py(
    human_desc: str = f"The part a human reads and judges; {_PINS['post_comment_pass']}.",
    detail_desc: str = "The part a future session needs: pointers and exact identifiers.",
    detail_param: str = "agent_detail",
    earlier_desc: str = "The label to add.",
) -> str:
    return (
        '@mcp.tool(name="add-label")\n'
        "async def add_label(\n"
        f'    label: Annotated[str, Field(description="{earlier_desc}")],\n'
        ") -> dict[str, Any]:\n"
        '    """Add a label."""\n\n\n'
        '@mcp.tool(name="post-comment")\n'
        "async def post_comment(\n"
        "    human: Annotated[\n"
        "        str,\n"
        f'        Field(description="{human_desc}"),\n'
        "    ],\n"
        f"    {detail_param}: Annotated[\n"
        "        str,\n"
        f'        Field(description="{detail_desc}"),\n'
        "    ],\n"
        ") -> dict[str, Any]:\n"
        '    """Post a two-part comment."""\n'
    )


_ROUTING_FILES = {
    "spec": ("skills/spec/SKILL.md", _spec_md()),
    "tighten": ("skills/tighten/SKILL.md", _tighten_md()),
    "interaction": ("skills/shared/references/user-interaction.md", _interaction_md()),
    "server": ("sy_tools/server.py", _server_py()),
    "schema": ("config/schema.json", json.dumps({"properties": {"text": _SCHEMA_TEXT}})),
    "defaults": ("config/defaults.json", json.dumps({"text": {"reader": "expert"}})),
}


def _routing_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: str) -> list[str]:
    """Build the six-file tree `check_human_text_routing` reads, replacing any of them, and return its errors."""
    unknown = set(overrides) - set(_ROUTING_FILES)
    assert not unknown, f"no such file in the routing tree: {sorted(unknown)}"
    for key, (rel, default) in _ROUTING_FILES.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(overrides.get(key, default), encoding="utf-8")
    monkeypatch.setattr(validate, "ROOT", tmp_path)
    errors: list[str] = []
    validate.check_human_text_routing(errors)
    return errors


def test_a_step_one_stating_the_pass_before_the_summary_send_passes(tmp_path, monkeypatch):
    step_one = (
        f"{_PINS['signoff_pass']}\n"
        "Hold the pre-pass draft for the duration of this step.\n"
        "Send that summary as direct text first, in full.\n"
        f"{_PINS['signoff_option']}\n"
    )
    errors = _routing_check(tmp_path, monkeypatch, spec=_spec_md(step_one=step_one))
    assert not errors, f"prose between the pass sentence and the send must not disturb the ordering pin: {errors}"


def test_a_step_one_that_never_states_the_pass_over_the_signoff_half_is_refused(tmp_path, monkeypatch):
    step_one = f"Send that summary as direct text first, in full.\n{_PINS['signoff_option']}\n"
    errors = _routing_check(tmp_path, monkeypatch, spec=_spec_md(step_one=step_one))
    assert any("tightened before it is shown" in error for error in errors), \
        f"a sign-off half shown untightened is not the text the user approved: {errors}"


@pytest.mark.parametrize("placement", ["in step two", "after the send"])
def test_the_pass_sentence_outside_step_one_or_after_the_send_is_refused(tmp_path, monkeypatch, placement):
    """Closes the two vacuity classes a whole-file containment pin passes on: the sentence surviving in the
    next step, and the sentence surviving in order-defeating position inside its own step."""
    if placement == "in step two":
        spec = _spec_md(
            step_one=f"Send that summary as direct text first, in full.\n{_PINS['signoff_option']}\n",
            step_two=f"### Step 2 - post the plan\n{_PINS['signoff_pass']}\n",
        )
        expected = "tightened before it is shown"
    else:
        spec = _spec_md(
            step_one=(
                "Send that summary as direct text first, in full.\n"
                f"{_PINS['signoff_pass']}\n{_PINS['signoff_option']}\n"
            ),
        )
        expected = "must state the pass before the step that sends"
    errors = _routing_check(tmp_path, monkeypatch, spec=spec)
    assert any(expected in error for error in errors), f"a pass sentence {placement} must be refused: {errors}"


def test_a_step_one_with_no_step_two_heading_to_bound_it_is_refused(tmp_path, monkeypatch):
    """An unterminated Step 1 runs to the end of the section, so both sign-off pins would be satisfiable by
    prose belonging to the step after approval."""
    errors = _routing_check(tmp_path, monkeypatch, spec=_spec_md(step_two="Post the plan once approved.\n"))
    assert any("no `### Step 2` to bound it" in error for error in errors), \
        f"a Step 1 with no right-hand bound must be refused, not widened: {errors}"


def test_a_step_one_offering_the_pre_pass_half_before_anything_else_passes(tmp_path, monkeypatch):
    step_one = (
        f"{_PINS['signoff_option']}\n{_PINS['signoff_pass']}\n"
        "Send that summary as direct text first, in full.\n"
    )
    errors = _routing_check(tmp_path, monkeypatch, spec=_spec_md(step_one=step_one))
    assert not errors, f"the escape-hatch option is pinned by presence in the step, not by position: {errors}"


def test_a_step_one_with_no_option_to_see_the_pre_pass_half_is_refused(tmp_path, monkeypatch):
    step_one = f"{_PINS['signoff_pass']}\nSend that summary as direct text first, in full.\n"
    errors = _routing_check(tmp_path, monkeypatch, spec=_spec_md(step_one=step_one))
    assert any("must offer the pre-pass sign-off half" in error for error in errors), \
        f"a compressed half with no way back to the full one is a lossy gate: {errors}"


def test_the_pre_pass_option_parked_in_step_two_is_refused(tmp_path, monkeypatch):
    """The vacuity a whole-file pin passes on: the option surviving as prose in the step after approval,
    where the sign-off call it belongs to can no longer offer it."""
    spec = _spec_md(
        step_one=f"{_PINS['signoff_pass']}\nSend that summary as direct text first, in full.\n",
        step_two=f"### Step 2 - post the plan\n{_PINS['signoff_option']}\n",
    )
    errors = _routing_check(tmp_path, monkeypatch, spec=spec)
    assert any("must offer the pre-pass sign-off half" in error for error in errors), \
        f"an option parked outside the step that offers it must be refused: {errors}"


def test_the_pre_pass_option_left_only_past_a_deleted_step_two_heading_is_refused(tmp_path, monkeypatch):
    """Without the loud terminator the region would widen past the deleted heading and swallow this copy,
    turning the bounded pin back into the whole-section pin it replaced."""
    spec = _spec_md(
        step_one=f"{_PINS['signoff_pass']}\nSend that summary as direct text first, in full.\n",
        step_two=f"{_PINS['signoff_option']}\n",
    )
    errors = _routing_check(tmp_path, monkeypatch, spec=spec)
    assert any("no `### Step 2` to bound it" in error for error in errors), \
        f"a widened region must be refused rather than satisfied by the text it swallowed: {errors}"


def test_a_human_field_naming_the_pass_passes_with_further_tools_defined_after_it(tmp_path, monkeypatch):
    server = _server_py() + '\n\n@mcp.tool(name="post-log")\nasync def post_log() -> None:\n    """Post a log."""\n'
    errors = _routing_check(tmp_path, monkeypatch, server=server)
    assert not errors, f"the bound ends at the second parameter; later tools are none of its business: {errors}"


def test_a_human_field_that_never_names_the_pass_is_refused(tmp_path, monkeypatch):
    errors = _routing_check(tmp_path, monkeypatch, server=_server_py(human_desc="The part a human reads."))
    assert any("must tell the caller to run the pass" in error for error in errors), \
        f"the human half is durable text and must be tightened before it is posted: {errors}"


@pytest.mark.parametrize("placement", ["the detail field", "an earlier tool"])
def test_the_pass_clause_in_any_field_but_the_human_one_is_refused(tmp_path, monkeypatch, placement):
    """Closes whole-file containment: the clause surviving on the machine half, or on some unrelated tool's
    field, leaves the human half with no instruction while the pin reads green."""
    clause = f"the caller should {_PINS['post_comment_pass']}."
    if placement == "the detail field":
        server = _server_py(human_desc="The part a human reads.", detail_desc=clause)
    else:
        server = _server_py(human_desc="The part a human reads.", earlier_desc=clause)
    errors = _routing_check(tmp_path, monkeypatch, server=server)
    assert any("must tell the caller to run the pass" in error for error in errors), \
        f"the clause on {placement} must not satisfy the human half's pin: {errors}"


def test_a_renamed_second_parameter_leaves_the_human_field_unbounded_and_is_refused(tmp_path, monkeypatch):
    """An ordinary rename retires the right-hand bound silently. Unterminated, the region runs to the end of
    the module and the clause would be satisfiable from any later description."""
    errors = _routing_check(tmp_path, monkeypatch, server=_server_py(detail_param="machine_detail"))
    assert any("bounds the `human` field description" in error for error in errors), \
        f"a renamed bound must be reported, not silently widened: {errors}"


def test_a_reader_section_carrying_the_setting_and_the_floor_passes(tmp_path, monkeypatch):
    reader = _THE_READER + "Write to the reader it names, not to the reader you imagined.\n"
    tighten = _tighten_md((_GROUND_RULES, reader, _PROTECT, _TIGHTEN_REPORT))
    errors = _routing_check(tmp_path, monkeypatch, tighten=tighten)
    assert not errors, f"extra prose in the section must not disturb either leg: {errors}"


@pytest.mark.parametrize("missing", ["the setting", "the floor"])
def test_a_reader_section_missing_the_setting_or_the_floor_is_refused(tmp_path, monkeypatch, missing):
    """A heading with no consumer leaves the setting declared in two config files and read by nobody, and a
    section with no floor leaves compression free to drop the decision itself."""
    if missing == "the setting":
        reader = f"## The reader\nWrite for whoever reads it. The setting {_PINS['reader_floor']}.\n"
        expected = "must resolve `text.reader`"
    else:
        reader = "## The reader\nResolve `text.reader` with `get_config`.\n"
        expected = "must keep the floor"
    tighten = _tighten_md((_GROUND_RULES, reader, _PROTECT, _TIGHTEN_REPORT))
    errors = _routing_check(tmp_path, monkeypatch, tighten=tighten)
    assert any(expected in error for error in errors), f"a reader section missing {missing} must be refused: {errors}"


def test_a_reader_section_left_last_and_hollow_is_refused(tmp_path, monkeypatch):
    """The heading's following `## ` deleted, with the setting and the floor parked in an earlier section:
    unbounded the region reads to the end of the file, and a whole-file pin stays green on the parked copy."""
    parked = f"## Human-facing text\nResolve `text.reader`. The setting {_PINS['reader_floor']}.\n"
    tighten = _tighten_md((_GROUND_RULES, parked, _PROTECT, "## The reader\n"))
    errors = _routing_check(tmp_path, monkeypatch, tighten=tighten)
    assert any("`## The reader` has no following `## ` heading" in error for error in errors), \
        f"an unbounded reader section must be refused rather than widened: {errors}"


def test_a_protect_section_stating_the_companion_condition_passes(tmp_path, monkeypatch):
    protect = f"## Protect\n{_PINS['companion_half']}\nNo `text.reader` setting relaxes either rule.\n"
    tighten = _tighten_md((_GROUND_RULES, _THE_READER, protect, _TIGHTEN_REPORT))
    errors = _routing_check(tmp_path, monkeypatch, tighten=tighten)
    assert not errors, f"the condition is pinned by presence in the section, not by position: {errors}"


def test_a_protect_section_without_the_companion_condition_is_refused(tmp_path, monkeypatch):
    protect = "## Protect\nAn entry may name an anchor as removable.\n"
    tighten = _tighten_md((_GROUND_RULES, _THE_READER, protect, _TIGHTEN_REPORT))
    errors = _routing_check(tmp_path, monkeypatch, tighten=tighten)
    assert any("only to a companion half" in error for error in errors), \
        f"the relaxation outlives its condition unless the condition is pinned: {errors}"


def test_a_protect_section_left_last_and_hollow_is_refused(tmp_path, monkeypatch):
    """Its following `## ` deleted and the condition parked earlier: one edit is both the terminator absence
    and the vacuity a whole-file pin would pass on."""
    parked = f"## Report\nName what changed. {_PINS['companion_half']}\n"
    tighten = _tighten_md((_GROUND_RULES, _THE_READER, parked, "## Protect\n"))
    errors = _routing_check(tmp_path, monkeypatch, tighten=tighten)
    assert any("`## Protect` has no following `## ` heading" in error for error in errors), \
        f"an unbounded protect section must be refused rather than widened: {errors}"


def test_ground_rules_carrying_the_short_text_floor_mid_section_pass(tmp_path, monkeypatch):
    ground = f"## Ground rules\nPreserve every fact.\n{_PINS['short_text_floor']}\nMatch the register.\n"
    tighten = _tighten_md((ground, _THE_READER, _PROTECT, _TIGHTEN_REPORT))
    errors = _routing_check(tmp_path, monkeypatch, tighten=tighten)
    assert not errors, f"the floor is pinned by presence in the rules, not by position: {errors}"


def test_ground_rules_without_the_short_text_floor_are_refused(tmp_path, monkeypatch):
    ground = "## Ground rules\nPreserve every fact. Cut padding, never substance.\n"
    tighten = _tighten_md((ground, _THE_READER, _PROTECT, _TIGHTEN_REPORT))
    errors = _routing_check(tmp_path, monkeypatch, tighten=tighten)
    assert any("must keep the short-text floor" in error for error in errors), \
        f"without the floor the pass rewrites text that was already correct: {errors}"


def test_ground_rules_left_last_and_hollow_are_refused(tmp_path, monkeypatch):
    """The floor parked in a section that binds one destination is the weakness this leg closes, and an
    unbounded rules section is what would let the pin read green from there."""
    parked = f"## Report\nName what changed. {_PINS['short_text_floor']}\n"
    tighten = _tighten_md((_THE_READER, _PROTECT, parked, "## Ground rules\n"))
    errors = _routing_check(tmp_path, monkeypatch, tighten=tighten)
    assert any("`## Ground rules` has no following `## ` heading" in error for error in errors), \
        f"an unbounded rules section must be refused rather than widened: {errors}"


def test_a_question_section_offering_the_fuller_version_passes(tmp_path, monkeypatch):
    body = (
        "Ask through the tool, one call per turn.\n"
        f"{_PINS['escape_hatch']}\nIt is a third option, not a second call.\n"
    )
    errors = _routing_check(tmp_path, monkeypatch, interaction=_interaction_md(question_body=body))
    assert not errors, f"extra guidance around the hatch must not disturb the pin: {errors}"


def test_a_question_section_with_no_escape_hatch_is_refused(tmp_path, monkeypatch):
    interaction = _interaction_md(question_body="Ask through the tool, one call per turn.\n")
    errors = _routing_check(tmp_path, monkeypatch, interaction=interaction)
    assert any("must offer the fuller version" in error for error in errors), \
        f"a compressed half with no route to the fuller one is a decision taken on less: {errors}"


def test_a_question_section_left_unbounded_by_a_deleted_next_heading_is_refused(tmp_path, monkeypatch):
    """`## Action needed` deleted and its body left behind carrying the hatch: unbounded, the question region
    swallows the orphaned prose and the pin reads green on a sentence that section no longer owns."""
    interaction = (
        "## Status\nA statement needing no answer.\n"
        "## Question\nAsk through the tool, one call per turn.\n"
        f"Say what you need. {_PINS['escape_hatch']}\n"
    )
    errors = _routing_check(tmp_path, monkeypatch, interaction=interaction)
    assert any("`## Question` has no following `## ` heading" in error for error in errors), \
        f"an unbounded question section must be refused rather than widened: {errors}"


def test_a_schema_and_defaults_declaring_the_reader_alongside_other_keys_pass(tmp_path, monkeypatch):
    text = {**_SCHEMA_TEXT, "properties": {"reader": {"type": "string"}, "width": {"type": "integer"}}}
    errors = _routing_check(
        tmp_path, monkeypatch,
        schema=json.dumps({"properties": {"text": text}}),
        defaults=json.dumps({"text": {"reader": "expert", "width": 80}}),
    )
    assert not errors, f"the setting is pinned by path, and siblings under the same object are fine: {errors}"


def test_a_tree_declaring_the_reader_in_neither_config_file_is_refused(tmp_path, monkeypatch):
    errors = _routing_check(
        tmp_path, monkeypatch, schema=json.dumps({"properties": {}}), defaults=json.dumps({}),
    )
    assert any("must declare `text.reader`" in error for error in errors), f"the schema must be refused: {errors}"
    assert any("must ship a `text.reader` default" in error for error in errors), \
        f"the defaults must be refused: {errors}"


@pytest.mark.parametrize(
    ("shape", "expected"),
    [
        (
            {"properties": {"text": {"type": "object", "additionalProperties": False, "properties": {}}}},
            "must declare `text.reader`",
        ),
        (
            {"properties": {"limits": {"properties": {"reader": {"type": "string"}}}}},
            "must declare `text.reader`",
        ),
        (
            {"properties": {"text": {**_SCHEMA_TEXT, "additionalProperties": True}}},
            "must set additionalProperties false",
        ),
    ],
    ids=["a text object with no reader", "a reader under another object", "an open text object"],
)
def test_the_schema_shapes_raw_containment_would_pass_on_are_refused(tmp_path, monkeypatch, shape, expected):
    """Every shape where the key's characters appear in the file while the setting the pass resolves is
    undeclared, unreachable at the path that resolves it, or unprotected against a typo."""
    errors = _routing_check(tmp_path, monkeypatch, schema=json.dumps(shape))
    assert any(expected in error for error in errors), f"a shape containment would pass on must be refused: {errors}"


def test_the_human_text_routing_check_is_registered_in_the_validation_run():
    """A check nothing calls protects nothing, and `main()` is its only caller."""
    assert "check_human_text_routing(errors)" in inspect.getsource(validate.main), \
        "the routing check must be registered in main()"


def _tier_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stem: str, declared_name: str) -> list[str]:
    """`check_agent_frontmatter_tiers` over one synthetic agent, with the real floors and tiers in scope."""
    for rel in ("config/floors.json", "config/defaults.json"):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((REAL_ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8")
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / f"{stem}.md").write_text(
        f"---\nname: {declared_name}\ndescription: test\nmodel: fable\neffort: max\n---\nbody\n", encoding="utf-8",
    )
    monkeypatch.setattr(validate, "ROOT", tmp_path)
    errors: list[str] = []
    validate.check_agent_frontmatter_tiers(errors)
    return errors


def test_a_frontmatter_name_matching_its_filename_stem_passes(tmp_path, monkeypatch):
    assert _tier_check(tmp_path, monkeypatch, "probe", "probe") == []


def test_a_frontmatter_name_drifted_from_its_filename_stem_is_refused(tmp_path, monkeypatch):
    """Floors key on the stem while dispatch and `agent_model` key on the name; drift mis-floors silently."""
    errors = _tier_check(tmp_path, monkeypatch, "repo-review", "repo_review")
    assert any("is not the filename stem" in e for e in errors), errors


_LOOP_PINS = validate.GATE_LOOP_PINS
_LOOP_SECTIONS = validate.GATE_LOOP_SECTIONS


# Every sandbox-write mode holds the same grants; `repo-standards` is guarded but granted no sandbox, so no write.
_SANDBOX_GRANTS = frozenset({"Read", "Grep", "Glob", "Write", "scratch_dir", "check_env"})
_LOOP_GRANTS = {
    "gate": _SANDBOX_GRANTS,
    "gate-triage": _SANDBOX_GRANTS,
    "repo-standards": frozenset({"Read", "Grep", "check_env"}),
}


def _loop_skill() -> str:
    """`skills/ship/SKILL.md` as `check_gate_loop` reads it: a complete worker contract, a BUILD-only pre-gate."""
    return (
        f"{_LOOP_SECTIONS['worker_contract']}\n"
        f"- {_LOOP_PINS['handover_code']} {_LOOP_PINS['gate_only']} — the round is complete, redispatch me.\n"
        f"- The worker makes its own checkpoint {_LOOP_PINS['checkpoint_before_return']}.\n"
        f"{_LOOP_SECTIONS['pre_gate']}\n"
        "Whatever BUILD returns next — `done`, `needs-decision`, `bail-to-spec`, or `blocked`.\n"
        f"{_LOOP_SECTIONS['state_router']}\n"
        f"Where the state file and a return disagree, {_LOOP_PINS['reconcile_ground_truth']}.\n"
        "## Completion bar\n"
    )


def _loop_gate_ref() -> str:
    """`immutable-gate.md`: the cost boundary above the heading, the whole round contract inside § Fix cycle."""
    return (
        "## Pin scope\n"
        f"{_LOOP_PINS['cost_boundary']}: the reviewer keeps its frontier model and max effort.\n"
        f"{_LOOP_SECTIONS['fix_cycle']}\n"
        f"A round returns `done` or `{_LOOP_PINS['handover']}`; every disposition comes from one "
        f"`{_LOOP_PINS['triage_agent']}` delegate and carries a `{_LOOP_PINS['root_cause_key']}` matched "
        f"against `{_LOOP_PINS['round_log']}`, a match {_LOOP_PINS['accept_recurrence']} returning "
        f"`{_LOOP_PINS['bail_to_spec']}`.\n"
        f"It converges only with no {_LOOP_PINS['stopping_rule']} standing.\n"
        f"The round's state write lands {_LOOP_PINS['round_write_ordering']}, never at end of round.\n"
        f"It carries the round's checkpoint, so the checkpoint is {_LOOP_PINS['checkpoint_before_return']}.\n"
        f"For the persisted verdict and dispositions alike, this worker {_LOOP_PINS['verdict_not_parsed']}.\n"
    )


def _loop_start() -> str:
    return (
        f"```yaml\n{_LOOP_PINS['round_log_seed']}\n```\n"
        f"Each entry carries a stable `{_LOOP_PINS['root_cause_key']}` and its disposition.\n"
    )


def _loop_build() -> str:
    """The durability rule in its own slice-manifest paragraph, and a later paragraph echoing the wording."""
    return (
        "## Delegated slice protocol\nBUILD integrates every slice and returns `done`.\n\n"
        f"{_LOOP_SECTIONS['slice_manifest']} in `phase_checkpoint`, written after each integration and so "
        f"{_LOOP_PINS['checkpoint_before_return']}, so a return that never arrives resumes at the next "
        "pending slice.\n\n"
        f"The manifest being {_LOOP_PINS['checkpoint_before_return']} is why a set request field no longer "
        "means \"not folded in yet\".\n"
    )


def _loop_worker() -> str:
    """The status block fenced, with the prose below it naming every field the block names."""
    return (
        f"{_LOOP_SECTIONS['return_contract']} — target 700 tokens\n"
        "End with exactly one status block:\n\n"
        "```text\n"
        "DONE: promoted to `in-review`\n"
        f"{_LOOP_PINS['worker_verdict_field']} none|<absolute path(s)>\n"
        f"{_LOOP_PINS['worker_dispositions_field']} none|<absolute path(s)>\n"
        "```\n\n"
        f"or `{_LOOP_PINS['handover_block']}: round <n> complete, loop not converged; CHECKPOINT: <…>; "
        f"{_LOOP_PINS['worker_verdict_field']} none|<path>; "
        f"{_LOOP_PINS['worker_dispositions_field']} none|<path>`.\n"
    )


def _loop_gate() -> str:
    """`agents/gate.md`: the persisted-verdict pin in a fence, under a heading carrying a token-target suffix."""
    return (
        "---\nname: gate\nmodel: fable\neffort: max\n---\n"
        f"{_LOOP_SECTIONS['return_contract']} — target ≤1,200 tokens\n"
        f"Write the verdict to the scratch root and name its path as `{_LOOP_PINS['gate_verdict_file']}` below.\n"
        "\n```text\n"
        "TL;DR: <safe to ship | not safe to ship, and why>\n"
        f"{_LOOP_PINS['gate_verdict_file']} <absolute path>\n"
        "```\n"
    )


def _loop_standards() -> str:
    return "---\nname: repo-standards\n---\nbody\n"


def _loop_guard() -> str:
    """`review_guard.py` as the cross-check reads it: two mode sets, one of them guarded but ungranted."""
    return (
        "REVIEW_MODES = {\n    'gate', 'gate-triage', 'repo-standards',\n}\n"
        "SANDBOX_WRITE_MODES = {'gate', 'gate-triage'}\n"
    )


def _loop_triage() -> str:
    return (
        "---\nname: gate-triage\nmodel: opus\neffort: high\n---\n"
        f"Every finding gets one disposition and a `{_LOOP_PINS['root_cause_key']}`.\n"
    )


def _loop_floors() -> str:
    return json.dumps(
        {"gate": {"min_model": _LOOP_PINS["gate_min_model"], "min_effort": _LOOP_PINS["gate_min_effort"]}}
    )


def _loop_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    grants: dict[str, frozenset[str]] | None = None,
    /,
    *,
    skill: str | None = None,
    gate_ref: str | None = None,
    start: str | None = None,
    build: str | None = None,
    worker: str | None = None,
    triage: str | None = None,
    gate: str | None = None,
    guard: str | None = None,
    floors: str | None = None,
    standards: str | None = None,
) -> list[str]:
    """Build the tree `check_gate_loop` reads and return its errors; `grants` overrides the `AGENT_GRANTS` entries
    it names, and each keyword replaces one file.

    Every mode the guard names carries a brief, `repo-standards` included: a guarded mode with no brief is
    itself a failure now, so the default tree cannot omit one.
    """
    files = {
        "skills/ship/SKILL.md": _loop_skill() if skill is None else skill,
        "skills/ship/references/immutable-gate.md": _loop_gate_ref() if gate_ref is None else gate_ref,
        "skills/ship/references/start-resume.md": _loop_start() if start is None else start,
        "skills/ship/references/implementation.md": _loop_build() if build is None else build,
        "agents/ship-gate.md": _loop_worker() if worker is None else worker,
        "agents/gate-triage.md": _loop_triage() if triage is None else triage,
        "agents/gate.md": _loop_gate() if gate is None else gate,
        "sy_tools/guards/review_guard.py": _loop_guard() if guard is None else guard,
        "config/floors.json": _loop_floors() if floors is None else floors,
        "agents/repo-standards.md": _loop_standards() if standards is None else standards,
    }
    for rel, text in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    monkeypatch.setattr(validate, "ROOT", tmp_path)
    monkeypatch.setattr(validate, "AGENT_GRANTS", {**_LOOP_GRANTS, **(grants or {})})
    errors: list[str] = []
    validate.check_gate_loop(errors)
    return errors


def test_a_complete_gate_loop_tree_passes(tmp_path, monkeypatch):
    """Also the standing evidence for the two negative legs: neither region names `handover` here."""
    errors = _loop_check(tmp_path, monkeypatch)
    assert not errors, f"a tree carrying every pinned span must pass: {errors}"


def test_a_worker_contract_that_never_names_handover_is_refused(tmp_path, monkeypatch):
    errors = _loop_check(tmp_path, monkeypatch, skill=_loop_skill().replace(_LOOP_PINS["handover"], "done"))
    assert any("arrives as an unrecognised string" in e for e in errors), \
        f"a worker contract missing GATE's sixth return must be refused: {errors}"


def test_a_handover_return_without_its_gate_only_qualifier_is_refused(tmp_path, monkeypatch):
    errors = _loop_check(tmp_path, monkeypatch, skill=_loop_skill().replace(_LOOP_PINS["gate_only"], ""))
    assert any("nothing bars START or BUILD from returning it" in e for e in errors), \
        f"an unqualified GATE-only return must be refused: {errors}"


def test_a_gate_worker_whose_return_contract_omits_the_handover_form_is_refused(tmp_path, monkeypatch):
    errors = _loop_check(tmp_path, monkeypatch, worker=_loop_worker().replace(_LOOP_PINS["handover_block"], "DONE"))
    assert any("is one it never emits" in e for e in errors), \
        f"a worker brief that never shows the handover form must be refused: {errors}"


def test_a_fresh_run_that_declares_neither_the_log_nor_its_key_is_refused(tmp_path, monkeypatch):
    errors = _loop_check(tmp_path, monkeypatch, start="```yaml\ngate_rounds_total: 0\n```\n")
    assert any("a resume reads as absent" in e for e in errors), \
        f"a fresh run that never seeds the round log must be refused: {errors}"
    assert any("recognise no recurrence" in e for e in errors), \
        f"a round log whose entries carry no stable key must be refused: {errors}"


def test_a_fix_cycle_missing_a_recurrence_term_is_refused(tmp_path, monkeypatch):
    errors = _loop_check(
        tmp_path, monkeypatch, gate_ref=_loop_gate_ref().replace(_LOOP_PINS["round_log"], "the earlier rounds")
    )
    assert any("the recurrence clause whole" in e for e in errors), \
        f"a fix cycle that no longer reads the round log must be refused: {errors}"


def test_a_fix_cycle_that_never_names_the_triage_delegate_is_refused(tmp_path, monkeypatch):
    errors = _loop_check(
        tmp_path, monkeypatch, gate_ref=_loop_gate_ref().replace(_LOOP_PINS["triage_agent"], "sy:gate")
    )
    assert any("the controller dispositions itself" in e for e in errors), \
        f"a fix cycle with no delegated triage must be refused: {errors}"


def test_a_fix_cycle_without_the_handover_exit_is_refused(tmp_path, monkeypatch):
    errors = _loop_check(
        tmp_path, monkeypatch, gate_ref=_loop_gate_ref().replace(_LOOP_PINS["handover"], "another round")
    )
    assert any("becomes an in-worker loop again" in e for e in errors), \
        f"a round with no non-converged exit must be refused: {errors}"


def test_a_fix_cycle_without_the_stopping_rule_is_refused(tmp_path, monkeypatch):
    errors = _loop_check(
        tmp_path, monkeypatch, gate_ref=_loop_gate_ref().replace(_LOOP_PINS["stopping_rule"], "open finding")
    )
    assert any("converges over a finding nobody dispositioned" in e for e in errors), \
        f"a fix cycle with no stopping rule must be refused: {errors}"


def test_a_gate_reference_that_drops_the_cost_boundary_is_refused(tmp_path, monkeypatch):
    errors = _loop_check(
        tmp_path, monkeypatch, gate_ref=_loop_gate_ref().replace(_LOOP_PINS["cost_boundary"], "Cost is bounded")
    )
    assert any("what cost-scaling may never touch" in e for e in errors), \
        f"dropping the standing cost boundary must be refused: {errors}"


def test_a_lowered_gate_reviewer_floor_is_refused(tmp_path, monkeypatch):
    floors = json.dumps({"gate": {"min_model": "standard", "min_effort": _LOOP_PINS["gate_min_effort"]}})
    errors = _loop_check(tmp_path, monkeypatch, floors=floors)
    assert any("may lower the independent reviewer" in e and "'standard'" in e for e in errors), \
        f"a gate floor below frontier must be refused, naming what it found: {errors}"


def test_a_pre_gate_checkpoint_that_names_handover_is_refused(tmp_path, monkeypatch):
    skill = _loop_skill().replace("or `blocked`", f"or `{_LOOP_PINS['handover']}`")
    errors = _loop_check(tmp_path, monkeypatch, skill=skill)
    assert any("expect it from BUILD" in e for e in errors), \
        f"a BUILD enumeration naming a GATE-only return must be refused: {errors}"


def test_a_build_procedure_that_names_handover_is_refused(tmp_path, monkeypatch):
    build = _loop_build() + f"A slice that cannot converge returns `{_LOOP_PINS['handover']}`.\n"
    errors = _loop_check(tmp_path, monkeypatch, build=build)
    assert any("BUILD reads as available to itself" in e for e in errors), \
        f"BUILD's own procedure naming a GATE-only return must be refused: {errors}"


def test_a_triage_brief_that_never_names_the_root_cause_key_is_refused(tmp_path, monkeypatch):
    triage = _loop_triage().replace(_LOOP_PINS["root_cause_key"], "label")
    errors = _loop_check(tmp_path, monkeypatch, triage=triage)
    assert any("cannot match across rounds" in e for e in errors), \
        f"a triage brief with no stable key must be refused: {errors}"


def test_a_triage_brief_granting_a_tracker_verb_is_refused(tmp_path, monkeypatch):
    errors = _loop_check(tmp_path, monkeypatch, {"gate-triage": _SANDBOX_GRANTS | {"set-status"}})
    assert any("a record no caller authored" in e for e in errors), \
        f"a delegate granted a tracker verb must be refused: {errors}"


def test_a_sandbox_write_mode_granted_nothing_is_refused(tmp_path, monkeypatch):
    """`_grants` reads a mode with no `AGENT_GRANTS` entry as this empty grant, which is loud rather than vacuous."""
    errors = _loop_check(tmp_path, monkeypatch, {"gate-triage": frozenset()})
    assert any("gate-triage" in e and "grants it no `Write`" in e for e in errors), \
        f"a sandbox-write mode with no grant entry must be refused: {errors}"


def test_a_section_appended_after_the_fix_cycle_is_refused(tmp_path, monkeypatch):
    """The heading-to-EOF scope is only a scope while the section is last; an appended `##` silently widens it."""
    gate_ref = _loop_gate_ref() + "## Appendix\nAnything at all.\n"
    errors = _loop_check(tmp_path, monkeypatch, gate_ref=gate_ref)
    assert any("no longer the file's last section" in e and "Appendix" in e for e in errors), \
        f"a section appended after § Fix cycle must be refused: {errors}"


def test_fix_cycle_terms_moved_under_an_appended_section_are_refused(tmp_path, monkeypatch):
    """The terms are all still in the file, just past a new heading -- the exact drift the scope used to miss."""
    heading = _LOOP_SECTIONS["fix_cycle"] + "\n"
    head, _, body = _loop_gate_ref().partition(heading)
    errors = _loop_check(tmp_path, monkeypatch, gate_ref=head + heading + "## Appendix\n" + body)
    assert any("no longer the file's last section" in e for e in errors), \
        f"terms relocated under an appended heading must be refused: {errors}"
    assert any("becomes an in-worker loop again" in e for e in errors), \
        f"the pins must be re-bounded to the real section, not the appended one: {errors}"


def test_a_section_appended_after_the_gate_worker_return_contract_is_refused(tmp_path, monkeypatch):
    """Same proof for the other heading-to-EOF scope: `agents/ship-gate.md` § Return contract."""
    intro, _, block = _loop_worker().partition("\n")
    errors = _loop_check(tmp_path, monkeypatch, worker=intro + "\n## Appendix\n" + block)
    assert any("no longer the file's last section" in e for e in errors), \
        f"a section appended after § Return contract must be refused: {errors}"
    assert any("is one it never emits" in e for e in errors), \
        f"the handover form moved under the appended heading must leave the pin unsatisfied: {errors}"


def test_the_fix_cycle_pins_do_not_widen_above_their_heading(tmp_path, monkeypatch):
    """The section is last in its file, so a heading-to-EOF scope is the only one available; prove it is a scope."""
    heading = _LOOP_SECTIONS["fix_cycle"] + "\n"
    head, _, body = _loop_gate_ref().partition(heading)
    errors = _loop_check(tmp_path, monkeypatch, gate_ref=head + body + heading)
    assert any("becomes an in-worker loop again" in e for e in errors), \
        f"round contract moved above its own heading must still be refused: {errors}"


def test_the_gate_worker_handover_pin_does_not_widen_above_its_heading(tmp_path, monkeypatch):
    """Same proof for the other end-of-file section: `agents/ship-gate.md` § Return contract."""
    intro, _, block = _loop_worker().partition("\n")
    errors = _loop_check(tmp_path, monkeypatch, worker=block + intro + "\n")
    assert any("is one it never emits" in e for e in errors), \
        f"a handover form shown above the return contract must still be refused: {errors}"


def test_a_worker_contract_with_no_terminating_heading_is_refused(tmp_path, monkeypatch):
    """The `_bounded_section` legs report the missing boundary rather than scanning to the end of the file."""
    skill = _loop_skill().partition(_LOOP_SECTIONS["pre_gate"])[0]
    errors = _loop_check(tmp_path, monkeypatch, skill=skill)
    assert any("has no following `## ` heading to bound it" in e for e in errors), \
        f"an unterminated worker contract must be refused rather than widened: {errors}"


def test_the_gate_loop_check_is_registered_in_main(tmp_path):
    """A check nothing calls protects nothing, and `main()` is its only caller."""
    assert "check_gate_loop(errors)" in inspect.getsource(validate.main), \
        "the gate-loop check must be registered in main()"
    assert "check_agent_returns(errors)" in inspect.getsource(validate.main), \
        "the agent-returns check must be registered in main()"


def test_a_fix_cycle_that_bails_on_any_recorded_key_is_refused(tmp_path, monkeypatch):
    """The log records rejects under their key too, and the delegate reuses a matching key by construction.

    So a recurrence clause keyed on "already treated" bails the whole run to /sy:spec over a finding an
    earlier round correctly rejected; only an accept disposition is the plan-contract signal.
    """
    gate_ref = _loop_gate_ref().replace(_LOOP_PINS["accept_recurrence"], "already recorded as treated")
    errors = _loop_check(tmp_path, monkeypatch, gate_ref=gate_ref)
    assert any("the recurrence clause whole" in e for e in errors), \
        f"a recurrence clause matching a rejected key too must be refused: {errors}"


@pytest.mark.parametrize(
    "carrier, fragment",
    [
        ("skill", "lost with any hand-back the parent never receives"),
        ("build", "the rule binds every phase"),
        ("gate_ref", "takes an in-return-only checkpoint with it"),
    ],
)
def test_a_checkpoint_left_only_in_the_return_is_refused(tmp_path, monkeypatch, carrier, fragment):
    """All three carriers state the same durability rule; a return that never arrives is the loss it prevents."""
    builder = {"skill": _loop_skill, "build": _loop_build, "gate_ref": _loop_gate_ref}[carrier]
    text = builder().replace(_LOOP_PINS["checkpoint_before_return"], "written once the round ends")
    errors = _loop_check(tmp_path, monkeypatch, **{carrier: text})
    assert any(fragment in e for e in errors), f"a checkpoint left only in the return must be refused: {errors}"


def test_a_round_state_write_deferred_past_the_fixes_is_refused(tmp_path, monkeypatch):
    gate_ref = _loop_gate_ref().replace(_LOOP_PINS["round_write_ordering"], "once the round is complete")
    errors = _loop_check(tmp_path, monkeypatch, gate_ref=gate_ref)
    assert any("the round that died applying a fix" in e for e in errors), \
        f"a round log written only at end of round must be refused: {errors}"


def test_a_fix_cycle_that_never_forbids_reading_the_persisted_verdict_is_refused(tmp_path, monkeypatch):
    gate_ref = _loop_gate_ref().replace(_LOOP_PINS["verdict_not_parsed"], "reads it back for the details")
    errors = _loop_check(tmp_path, monkeypatch, gate_ref=gate_ref)
    assert any("recorded fake-green shape" in e for e in errors), \
        f"a controller allowed to re-read the verdict must be refused: {errors}"


def test_a_state_router_that_states_no_reconcile_rule_is_refused(tmp_path, monkeypatch):
    skill = _loop_skill().replace(_LOOP_PINS["reconcile_ground_truth"], "either may be used")
    errors = _loop_check(tmp_path, monkeypatch, skill=skill)
    assert any("no rule for which one it acts on" in e for e in errors), \
        f"a state router with no reconcile rule must be refused: {errors}"


def test_a_gate_worker_return_form_naming_no_verdict_field_is_refused(tmp_path, monkeypatch):
    worker = _loop_worker().replace(_LOOP_PINS["worker_verdict_field"], "REPORT:")
    errors = _loop_check(tmp_path, monkeypatch, worker=worker)
    assert any("nothing to point a human at" in e for e in errors), \
        f"a return form carrying no verdict path must be refused: {errors}"


def test_a_reviewer_return_contract_naming_no_verdict_file_is_refused(tmp_path, monkeypatch):
    gate = _loop_gate().replace(_LOOP_PINS["gate_verdict_file"], "the verdict")
    errors = _loop_check(tmp_path, monkeypatch, gate=gate)
    assert any("only inside a return the caller may compress away" in e for e in errors), \
        f"a reviewer that persists no named verdict must be refused: {errors}"


def test_a_reviewer_block_losing_its_verdict_file_field_while_the_prose_keeps_it_is_refused(tmp_path, monkeypatch):
    """The decoy occurrence: the sentence above the fence names the field, so a section-wide pin stays green."""
    gate = _loop_gate().replace(f"{_LOOP_PINS['gate_verdict_file']} <absolute path>\n", "")
    errors = _loop_check(tmp_path, monkeypatch, gate=gate)
    assert any("only inside a return the caller may compress away" in e for e in errors), \
        f"a return block with no verdict path must be refused even while the prose names one: {errors}"


def test_a_second_fence_in_the_reviewer_return_contract_is_refused(tmp_path, monkeypatch):
    """A decoy fence ahead of the real one carries the field the copied block has lost -- joining reads green."""
    field = f"{_LOOP_PINS['gate_verdict_file']} <absolute path>\n"
    gate = _loop_gate().replace(field, "").replace("\n```text\n", f"\n```text\n{field}```\n\n```text\n", 1)
    errors = _loop_check(tmp_path, monkeypatch, gate=gate)
    assert any("exactly one fenced code block" in e and "carries 2" in e for e in errors), \
        f"a pinned section carrying two fences must be refused rather than read: {errors}"


def test_a_second_fence_in_the_gate_worker_return_contract_is_refused(tmp_path, monkeypatch):
    """Same shape on the other pinned section: the fields sit in a decoy block no worker copies."""
    fields = (
        f"{_LOOP_PINS['worker_verdict_field']} none|<absolute path(s)>\n"
        f"{_LOOP_PINS['worker_dispositions_field']} none|<absolute path(s)>\n"
    )
    worker = _loop_worker().replace(fields, "").replace("```text\n", f"```text\n{fields}```\n\n```text\n", 1)
    errors = _loop_check(tmp_path, monkeypatch, worker=worker)
    assert any("exactly one fenced code block" in e and "carries 2" in e for e in errors), \
        f"a pinned section carrying two fences must be refused rather than read: {errors}"


def test_a_gate_worker_block_renaming_its_verdict_field_while_the_prose_keeps_it_is_refused(tmp_path, monkeypatch):
    """Same decoy shape: the HANDOVER sentence below the fence names `VERDICT:` whatever the block carries."""
    worker = _loop_worker().replace(
        f"{_LOOP_PINS['worker_verdict_field']} none|<absolute path(s)>", "REPORT: none|<absolute path(s)>"
    )
    errors = _loop_check(tmp_path, monkeypatch, worker=worker)
    assert any("nothing to point a human at" in e for e in errors), \
        f"a renamed verdict field must be refused even while the prose names the old one: {errors}"


def test_a_gate_worker_block_losing_its_dispositions_field_is_refused(tmp_path, monkeypatch):
    """The verdict says what the round judged; only the dispositions say what each finding was judged to be."""
    worker = _loop_worker().replace(
        f"{_LOOP_PINS['worker_dispositions_field']} none|<absolute path(s)>\n", ""
    )
    errors = _loop_check(tmp_path, monkeypatch, worker=worker)
    assert any("finding by finding" in e for e in errors), \
        f"a return block naming only the verdict must be refused: {errors}"


def test_a_build_procedure_whose_manifest_paragraph_drops_the_durability_rule_is_refused(tmp_path, monkeypatch):
    """The later paragraph quotes the rule back; only the paragraph defining the manifest states it."""
    manifest, sep, echo = _loop_build().rpartition("\n\n")
    build = manifest.replace(_LOOP_PINS["checkpoint_before_return"], "written once the phase ends") + sep + echo
    errors = _loop_check(tmp_path, monkeypatch, build=build)
    assert any("the rule binds every phase" in e for e in errors), \
        f"a manifest paragraph with no durability rule must be refused: {errors}"


def test_a_build_procedure_with_no_slice_manifest_paragraph_is_refused(tmp_path, monkeypatch):
    """The scope must fail loudly rather than pass on a file the anchor no longer appears in."""
    build = _loop_build().replace(_LOOP_SECTIONS["slice_manifest"], "Keep a record of each slice")
    errors = _loop_check(tmp_path, monkeypatch, build=build)
    assert any("has nowhere to look" in e for e in errors), \
        f"a build procedure with no manifest paragraph must be refused: {errors}"


def test_a_guarded_mode_with_no_brief_at_all_is_refused(tmp_path, monkeypatch):
    """Skipping it passed every grant check on a fabricated or typo'd mode name."""
    guard = _loop_guard().replace("'repo-standards',", "'repo-standards', 'bogus-mode',").replace(
        "SANDBOX_WRITE_MODES = {'gate', 'gate-triage'}",
        "SANDBOX_WRITE_MODES = {'gate', 'gate-triage', 'bogus-mode'}",
    )
    errors = _loop_check(tmp_path, monkeypatch, guard=guard)
    assert any("bogus-mode" in e and "does not exist" in e for e in errors), \
        f"a guarded mode with no brief must be refused, not skipped: {errors}"


def test_a_sandbox_grant_for_a_mode_no_dispatch_resolves_is_refused(tmp_path, monkeypatch):
    """`REVIEW_MODES` is what a dispatch resolves; a grant outside it is licensed by nothing."""
    guard = _loop_guard().replace(" 'repo-standards',", "").replace(
        "SANDBOX_WRITE_MODES = {'gate', 'gate-triage'}",
        "SANDBOX_WRITE_MODES = {'gate', 'gate-triage', 'repo-standards'}",
    )
    errors = _loop_check(tmp_path, monkeypatch, {"repo-standards": _SANDBOX_GRANTS}, guard=guard)
    assert any("the grant is dead" in e for e in errors), \
        f"a sandbox grant for an undispatched mode must be refused: {errors}"


def test_a_guard_mode_set_built_from_a_union_of_literals_reads_as_unparseable():
    """Stopping at the first `}` returned one operand and silently exempted every mode in the others."""
    source = "SANDBOX_WRITE_MODES = {'gate'} | {'gate-triage'}\n"
    assert validate._guard_mode_set(source, "SANDBOX_WRITE_MODES") is None, \
        "a union literal must read as unparseable, never as its first operand"
    assert validate._guard_mode_set("MODES = {\n    'gate',\n}\n", "MODES") == {"gate"}, \
        "the guard's own multi-line literal must still parse"


def test_a_reviewer_return_contract_heading_that_is_gone_is_refused(tmp_path, monkeypatch):
    """The heading carries a token-target suffix, so the pin is prefix-matched and its absence must be loud."""
    gate = _loop_gate().replace(_LOOP_SECTIONS["return_contract"], "## What to return")
    errors = _loop_check(tmp_path, monkeypatch, gate=gate)
    assert any("persisted-verdict pin is scoped to it" in e for e in errors), \
        f"a reviewer with no return contract heading must be refused: {errors}"


def test_a_sandbox_write_mode_whose_brief_holds_no_write_grant_is_refused(tmp_path, monkeypatch):
    errors = _loop_check(tmp_path, monkeypatch, {"gate-triage": _SANDBOX_GRANTS - {"Write"}})
    assert any("grants it no `Write`" in e for e in errors), \
        f"a sandbox-write mode with no write grant must be refused: {errors}"


def test_a_sandbox_write_mode_granted_no_scratch_dir_is_refused(tmp_path, monkeypatch):
    errors = _loop_check(tmp_path, monkeypatch, {"gate-triage": _SANDBOX_GRANTS - {"scratch_dir"}})
    assert any("resolve the root it is allowed to write" in e for e in errors), \
        f"a sandbox-write mode with no scratch_dir must be refused: {errors}"


def test_a_guarded_but_ungranted_mode_whose_brief_holds_a_write_is_refused(tmp_path, monkeypatch):
    errors = _loop_check(tmp_path, monkeypatch, {"repo-standards": _LOOP_GRANTS["repo-standards"] | {"Write"}})
    assert any("guards this mode" in e and "without granting it the sandbox" in e for e in errors), \
        f"a guarded, ungranted mode holding a write must be refused: {errors}"


def test_a_guard_source_with_no_parseable_mode_set_is_refused(tmp_path, monkeypatch):
    """Parsed out of the guard's text; an unreadable source must fail loudly rather than cross-check nothing."""
    errors = _loop_check(tmp_path, monkeypatch, guard="MODES = frozenset(_load_modes())\n")
    assert any("could not both be read out of the guard source" in e for e in errors), \
        f"an unparseable guard source must be refused: {errors}"


_HANDBACK_PINS = validate.AGENT_RETURN_PINS


def _returns_reference() -> str:
    return "## The rules\n" + "".join(f"- **{_LOOP_PINS[key]}** — why.\n" for key in _HANDBACK_PINS)


def _returns_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reference: str | None = None,
    brief: str | None = None,
) -> list[str]:
    """Build the two-file tree `check_agent_returns` reads and return its errors."""
    citation = "Hand back once, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/agent-returns.md`.\n"
    files = {
        "skills/shared/references/agent-returns.md": _returns_reference() if reference is None else reference,
        "agents/probe.md": citation if brief is None else brief,
    }
    for rel, text in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    monkeypatch.setattr(validate, "ROOT", tmp_path)
    errors: list[str] = []
    validate.check_agent_returns(errors)
    return errors


def test_a_reference_carrying_every_rule_and_a_brief_citing_it_pass(tmp_path, monkeypatch):
    assert _returns_check(tmp_path, monkeypatch) == []


@pytest.mark.parametrize("pin", _HANDBACK_PINS)
def test_a_reference_missing_one_hand_back_rule_is_refused(tmp_path, monkeypatch, pin):
    reference = _returns_reference().replace(_LOOP_PINS[pin], "something else")
    errors = _returns_check(tmp_path, monkeypatch, reference=reference)
    assert any(_LOOP_PINS[pin] in e for e in errors), f"a dropped hand-back rule must be refused: {errors}"


def test_a_brief_that_restates_the_rules_instead_of_citing_the_path_is_refused(tmp_path, monkeypatch):
    """The citation is what is pinned: a copy of the text drifts from the reference without ever failing."""
    brief = f"Hand back once. {_LOOP_PINS['handback_is_one_shot']}, and {_LOOP_PINS['return_terminates']}.\n"
    errors = _returns_check(tmp_path, monkeypatch, brief=brief)
    assert any("forks it" in e for e in errors), f"a brief citing no reference must be refused: {errors}"


@pytest.mark.parametrize("pin", _HANDBACK_PINS)
def test_a_brief_that_cites_the_path_and_restates_a_rule_anyway_is_refused(tmp_path, monkeypatch, pin):
    """Citation presence is not restatement absence; a brief can carry both, and the copy is what drifts."""
    citation = "Hand back once, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/agent-returns.md`.\n"
    errors = _returns_check(tmp_path, monkeypatch, brief=f"{citation}Also: {_LOOP_PINS[pin]}.\n")
    assert any("must not restate" in e and _LOOP_PINS[pin] in e for e in errors), (
        f"a cited brief restating {pin} must still be refused: {errors}"
    )


# The rule the grants lean on, under the heading the pin is scoped to, plus the `## ` heading that bounds it.
_LSP_RULE = (
    "The tool is absent where a repository declares no server, and dead where its command is off the session "
    "PATH; fall back to `Grep`/`Read` and say which navigation you could not do type-aware.\n"
)
_LSP_TERMINATOR = "## Prefer it where types beat text\n\nUse it for symbol questions.\n"
_LSP_REFERENCE = (
    f"# Language-server navigation\n\n{validate.LSP_AVAILABILITY_HEADING}\n\n{_LSP_RULE}\n{_LSP_TERMINATOR}"
)


def _lsp_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    granted: frozenset[str],
    brief: str,
    reference: str = _LSP_REFERENCE,
) -> list[str]:
    """Build the agent-plus-reference tree `check_lsp_grants` reads and return its errors."""
    (tmp_path / "agents").mkdir()
    (tmp_path / "skills" / "shared" / "references").mkdir(parents=True)
    (tmp_path / validate.LSP_REFERENCE).write_text(reference, encoding="utf-8")
    (tmp_path / "agents" / "probe.md").write_text(f"---\nname: probe\nmodel: sonnet\n---\n{brief}", encoding="utf-8")
    monkeypatch.setattr(validate, "ROOT", tmp_path)
    monkeypatch.setattr(validate, "AGENT_GRANTS", {"probe": granted})
    errors: list[str] = []
    validate.check_lsp_grants(errors)
    return errors


_LSP_CITATION = "Prefer it per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/lsp.md`.\n"
_LSP_GRANTED = frozenset({"Read", "Grep", "LSP"})
_LSP_UNGRANTED = frozenset({"Read", "Grep"})


def test_a_grant_beside_its_citation_passes(tmp_path, monkeypatch):
    errors = _lsp_check(tmp_path, monkeypatch, _LSP_GRANTED, _LSP_CITATION)
    assert not errors, f"a granted, cited LSP tool must pass: {errors}"


def test_an_agent_naming_neither_the_grant_nor_the_citation_passes(tmp_path, monkeypatch):
    errors = _lsp_check(tmp_path, monkeypatch, _LSP_UNGRANTED, "body\n")
    assert not errors, f"an agent with no business navigating symbols must not be forced into the grant: {errors}"


def test_a_grant_without_the_citation_is_refused(tmp_path, monkeypatch):
    errors = _lsp_check(tmp_path, monkeypatch, _LSP_GRANTED, "body\n")
    assert any("never cites" in error for error in errors), f"an unguided grant must be refused: {errors}"


def test_a_citation_without_the_grant_is_refused(tmp_path, monkeypatch):
    errors = _lsp_check(tmp_path, monkeypatch, _LSP_UNGRANTED, _LSP_CITATION)
    assert any("grants no 'LSP'" in error for error in errors), \
        f"guidance for a tool the agent can never call must be refused: {errors}"


@pytest.mark.parametrize("dropped", ["absent", "PATH", "`Grep`", "`Read`"])
def test_a_reference_that_lost_the_unavailable_case_is_refused(tmp_path, monkeypatch, dropped):
    """A hollowed reference is what turns a missing language server into a fault an agent tries to fix."""
    reference = _LSP_REFERENCE.replace(dropped, "elsewhere")
    errors = _lsp_check(tmp_path, monkeypatch, _LSP_GRANTED, _LSP_CITATION, reference=reference)
    assert any("fallback" in error for error in errors), f"dropping {dropped!r} must be refused: {errors}"


def test_a_reference_whose_rule_moved_out_of_its_section_is_refused(tmp_path, monkeypatch):
    """Every pinned word survives elsewhere in the file while the section stating the rule is empty."""
    reference = (
        f"# Language-server navigation\n\n{_LSP_RULE}\n{validate.LSP_AVAILABILITY_HEADING}\n\n"
        f"Use the tool.\n\n{_LSP_TERMINATOR}"
    )
    errors = _lsp_check(tmp_path, monkeypatch, _LSP_GRANTED, _LSP_CITATION, reference=reference)
    assert any("fallback" in error for error in errors), \
        f"a hollowed section whose words survive outside it must still be refused: {errors}"


def test_a_reference_missing_the_availability_heading_is_refused(tmp_path, monkeypatch):
    """The pin has nowhere to look once the heading is renamed, which is a loud failure, not a pass."""
    reference = _LSP_REFERENCE.replace(validate.LSP_AVAILABILITY_HEADING, "## Something else")
    errors = _lsp_check(tmp_path, monkeypatch, _LSP_GRANTED, _LSP_CITATION, reference=reference)
    assert any("heading" in error for error in errors), f"a renamed heading must be refused: {errors}"


def test_the_lsp_grant_check_is_registered_in_main():
    """A check nothing calls protects nothing, and `main()` is its only caller."""
    assert "check_lsp_grants(errors)" in inspect.getsource(validate.main), \
        "the LSP-grant check must be registered in main()"


_DOCS_MCP_REFERENCE = (
    f"# Repository MCP servers\n\n{validate.DOCS_MCP_AVAILABILITY_HEADING}\n\n"
    "A server may be absent; fall back to `Read`/`Grep`. MCP tools arrive deferred: load one with `ToolSearch`.\n\n"
    "## Read only\n\nNever write.\n"
)
_DOCS_MCP_CITATION = "Search it first, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/docs-mcp.md`.\n"


def _docs_mcp_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, brief: str, reference: str = _DOCS_MCP_REFERENCE,
) -> list[str]:
    """Build the agent-plus-reference tree `check_docs_mcp_citations` reads and return its errors."""
    (tmp_path / "agents").mkdir()
    (tmp_path / "skills" / "shared" / "references").mkdir(parents=True)
    (tmp_path / validate.DOCS_MCP_REFERENCE).write_text(reference, encoding="utf-8")
    (tmp_path / "agents" / "probe.md").write_text(f"---\nname: probe\n---\n{brief}", encoding="utf-8")
    monkeypatch.setattr(validate, "ROOT", tmp_path)
    errors: list[str] = []
    validate.check_docs_mcp_citations(errors)
    return errors


def test_a_brief_citing_the_docs_mcp_reference_passes(tmp_path, monkeypatch):
    errors = _docs_mcp_check(tmp_path, monkeypatch, _DOCS_MCP_CITATION)
    assert not errors, f"a brief citing a complete reference must pass: {errors}"


def test_a_brief_missing_the_docs_mcp_citation_is_refused(tmp_path, monkeypatch):
    errors = _docs_mcp_check(tmp_path, monkeypatch, "body\n")
    assert any("never cites" in error for error in errors), f"an uncited brief must be refused: {errors}"


def test_a_brief_naming_a_repository_server_is_refused(tmp_path, monkeypatch):
    """Agents serve every repository, so a server name in a brief is guidance for one setup only."""
    errors = _docs_mcp_check(tmp_path, monkeypatch, _DOCS_MCP_CITATION + "Call `mcp__acmedocs__search` first.\n")
    assert any("mcp__acmedocs__search" in error for error in errors), f"a named server must be refused: {errors}"


@pytest.mark.parametrize("dropped", ["absent", "ToolSearch", "`Read`", "`Grep`"])
def test_a_docs_mcp_reference_that_lost_its_fallback_is_refused(tmp_path, monkeypatch, dropped):
    reference = _DOCS_MCP_REFERENCE.replace(dropped, "elsewhere")
    errors = _docs_mcp_check(tmp_path, monkeypatch, _DOCS_MCP_CITATION, reference=reference)
    assert any("fallback" in error for error in errors), f"dropping {dropped!r} must be refused: {errors}"
