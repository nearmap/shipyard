"""The tracker seam, at the MCP package's location: only `sy_tools/tracker/` names a real tracker.

CONTRIBUTING.md's rule is that exactly one place knows how to talk to a specific tracker.
`scripts/validate.py`'s `check_seam` enforces that over the docs zone's markdown; this enforces the
same rule over the package's Python, where the legal zone is `sy_tools/tracker/`. Two scans rather
than one because each walks a different tree by a different rule.

There are exactly three exemptions: the legal zone, adapter tests, and this file. `sy_tools/guards/`
is not one of them — a hook guard's self-test corpus needs command strings of a realistic *shape*,
which a tracker-neutral stand-in gives it. The patterns below are word-bounded, so a tracker name
buried in an identifier (`ACLI_TOKEN`) reads as one word to `\b` and slips a scan that a bare `acli`
would fail; the corpora therefore use neutral names (`EXAMPLE_TOKEN`) rather than relying on that.
"""
from __future__ import annotations

from pathlib import Path
import re

MCP_ROOT = Path(__file__).resolve().parents[1]
LEGAL_ZONE = MCP_ROOT / "tracker"
ADAPTER_TESTS = MCP_ROOT / "tests" / "tracker"

TRACKER_TOKENS = [
    re.compile(p, f) for p, f in [
        (r"\bjira\b", re.I), (r"\bacli\b", re.I), (r"\batlassian\b", re.I),
        (r"\.atlassian\.net", 0), (r"\bADF\b", re.I),
        (r"\bgithub\b", re.I), (r"\bgh\b", re.I), (r"\bgist\b", re.I),
        (r"\bissueType\b", 0), (r"\bsubtask\b", re.I),
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
        text = path.read_text(encoding="utf-8")
        for pattern in TRACKER_TOKENS:
            match = pattern.search(text)
            if match:
                line = text[: match.start()].count("\n") + 1
                violations.append(f"{path.relative_to(MCP_ROOT.parent)}:{line}: {match.group(0)!r}")
                break
    assert not violations, "tracker-native vocabulary outside sy_tools/tracker/: " + "; ".join(violations)


def test_the_legal_zone_exists_and_is_where_the_adapters_live():
    adapters = sorted(p.parent.name for p in LEGAL_ZONE.rglob("adapter.py"))
    assert adapters, "sy_tools/tracker/ must hold the adapter implementations"
    assert (LEGAL_ZONE / "__init__.py").is_file(), "tracker selection must have exactly one home"
