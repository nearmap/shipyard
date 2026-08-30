"""The tracker seam, at the MCP package's location: only `sy_tools/tracker/` names a real tracker.

CONTRIBUTING.md's rule is that exactly one place knows how to talk to a specific tracker.
`scripts/validate.py`'s `check_seam` enforces that over the docs zone's markdown; this enforces the
same rule over the package's Python, where the legal zone is `sy_tools/tracker/`. Two scans rather
than one because each walks a different tree by a different rule.

There are exactly three whole-file exemptions: the legal zone, adapter tests, and this file.
`sy_tools/guards/` is not one of them; a single token is scoped instead, which is a different thing.
A guard's self-test *corpus* is fake command strings, where a tracker-neutral stand-in serves as well
as a real name — that is what the whole-file rule protects, and it still holds here. A guard's
*matcher* is not the same case: `review_guard.py` refuses a review agent's remote writes by
recognising the literal words an agent would type, and no stand-in can deny `gh pr merge`. So the bare
`gh` pattern alone carries zones where it does not apply: the guard and its tests. That matches the two
other statements of this rule — `CONTRIBUTING.md`'s and `scripts/validate.py`'s `check_seam` — which
both ban GitHub-as-*tracker* vocabulary (`gh issue`, `gh project`, `gh gist`) rather than the CLI's
bare name. Every other token still fires in both directories, so the corpora keep their neutral names.

The patterns below are word-bounded, so a tracker name buried in an identifier (`ACLI_TOKEN`) reads as
one word to `\b` and slips a scan that a bare `acli` would fail; the corpora therefore use neutral
names (`EXAMPLE_TOKEN`) rather than relying on that.
"""
from __future__ import annotations

from pathlib import Path
import re

MCP_ROOT = Path(__file__).resolve().parents[1]
LEGAL_ZONE = MCP_ROOT / "tracker"
ADAPTER_TESTS = MCP_ROOT / "tests" / "tracker"

# The matcher and the tests that exercise it: a case asserting `gh pr merge` is denied has to spell the
# command out exactly as the matcher does.
GUARDS_ZONES = (MCP_ROOT / "guards", MCP_ROOT / "tests" / "guards")

# Each token, and the directories where that token alone does not apply — `None` for almost all of them.
# Per-token rather than a whole-file `_exempt()` entry on purpose: widening `_exempt()` would let every
# tracker name into the guard directories at once and gut the neutral-stand-in discipline above.
TRACKER_TOKENS: list[tuple[re.Pattern[str], tuple[Path, ...] | None]] = [
    (re.compile(p, f), zone) for p, f, zone in [
        (r"\bjira\b", re.I, None), (r"\bacli\b", re.I, None), (r"\batlassian\b", re.I, None),
        (r"\.atlassian\.net", 0, None), (r"\bADF\b", re.I, None),
        (r"\bgithub\b", re.I, None), (r"\bgh\b", re.I, GUARDS_ZONES), (r"\bgist\b", re.I, None),
        (r"\bissueType\b", 0, None), (r"\bsubtask\b", re.I, None),
    ]
]


def _exempt(path: Path) -> bool:
    """Whether `path` may name a concrete tracker: the adapters, their tests, or this file."""
    if LEGAL_ZONE in path.parents:
        return True
    # Deliberately narrow: a non-test module dropped in that directory is still core code.
    if path.parent == ADAPTER_TESTS and path.name.startswith("test_"):
        return True
    # This file spells out the tokens it looks for, so it necessarily names them.
    return path.resolve() == Path(__file__).resolve()


def _file_violation(path: Path, text: str) -> str | None:
    """The first token `text` leaks, as a `path:line: token` string, or None.

    Shared by the real scan and the scoping cases below, so what those cases assert is this rule and not
    a second copy of it that could drift into agreeing with itself.
    """
    for pattern, exempt_zones in TRACKER_TOKENS:
        if exempt_zones is not None and any(zone in path.parents for zone in exempt_zones):
            continue
        match = pattern.search(text)
        if match:
            line = text[: match.start()].count("\n") + 1
            return f"{path.relative_to(MCP_ROOT.parent)}:{line}: {match.group(0)!r}"
    return None


def _scanned_files() -> list[Path]:
    """Every Python file in the package that is not exempt from the seam rule."""
    # Globbed rather than listed: a hand-kept list has to stay complete, and a guard living outside
    # the package once escaped the scan entirely.
    return sorted(p for p in MCP_ROOT.rglob("*.py") if not _exempt(p))


def test_only_the_tracker_package_names_a_concrete_tracker():
    scanned = _scanned_files()
    assert {p.name for p in scanned} >= {"server.py", "config.py", "secrets.py", "__init__.py"}, (
        "the seam scan must actually cover the core modules"
    )
    violations = []
    for path in scanned:
        violation = _file_violation(path, path.read_text(encoding="utf-8"))
        if violation:
            violations.append(violation)
    assert not violations, "tracker-native vocabulary outside sy_tools/tracker/: " + "; ".join(violations)


def test_the_legal_zone_exists_and_is_where_the_adapters_live():
    adapters = sorted(p.parent.name for p in LEGAL_ZONE.rglob("adapter.py"))
    assert adapters, "sy_tools/tracker/ must hold the adapter implementations"
    assert (LEGAL_ZONE / "__init__.py").is_file(), "tracker selection must have exactly one home"


def test_the_guard_zones_lose_only_the_bare_gh_token():
    """Every other tracker name still violates inside `guards/`; the exemption is one token, not the directory.

    The failure this pins is a widened `_exempt()`, which would have been the easy way to let the matcher
    name `gh` and would have taken the whole neutral-stand-in discipline with it.
    """
    for zone in GUARDS_ZONES:
        for name in ("jira", "acli", "atlassian", "github", "gist", "subtask"):
            probe = zone / "probe.py"
            assert _file_violation(probe, f"X = '{name}'\n") is not None, (
                f"{name!r} must still violate inside {zone.name}/; only the bare `gh` token is scoped"
            )


def test_bare_gh_still_violates_outside_the_guard_zones():
    """The scoping is per-directory: core modules and non-guard tests may still not name the CLI."""
    for probe in (MCP_ROOT / "config.py", MCP_ROOT / "server.py", MCP_ROOT / "tests" / "test_config.py"):
        assert _file_violation(probe, "run(['gh', 'pr', 'view'])\n") is not None, (
            f"bare `gh` must still violate in {probe.name}; the exemption covers the guard zones only"
        )


def test_the_guard_matcher_may_name_the_cli_it_matches():
    """The case the scoping exists for: a stand-in cannot deny a command an agent actually types."""
    assert _file_violation(GUARDS_ZONES[0] / "review_guard.py", "'pr': {'merge'}  # gh pr merge\n") is None
