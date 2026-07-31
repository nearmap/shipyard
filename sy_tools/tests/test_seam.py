"""The tracker seam, at the MCP package's location.

CONTRIBUTING.md's rule is that exactly one place knows how to talk to a specific tracker.
`scripts/validate.py`'s `check_seam` enforces that for `skills/tracker/`; this enforces the same
rule for `sy_tools/`, where the legal zone is `sy_tools/tracker/`. It is a pytest check rather than a new
entry in `check_seam`'s scan list so `scripts/validate.py` stays untouched by this change.

Adapter *tests* are the second legal zone. Every test in this package lives under
`sy_tools/tests/`, mirroring the package it covers, so the tests that exercise one concrete adapter
land in `sy_tools/tests/tracker/` — outside the tracker package but unavoidably naming a tracker.
The exemption is deliberately narrow: only `test_*.py` directly under that directory. A non-test
module dropped there is still core code and is still scanned, so the seam cannot be evaded by
choosing a directory.

This file is the third exemption, for the same reason `check_seam` exempts `validate.py`
(`scripts/validate.py:631`): the scanner has to spell out the tokens it looks for.
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
        (r"\.atlassian\.net", 0), (r"\bADF\b", 0),
        (r"\bgithub\b", re.I), (r"\bgh\b", re.I), (r"\bgist\b", re.I),
        (r"\bissueType\b", 0), (r"\bsubtask\b", re.I),
    ]
]


def _exempt(path: Path) -> bool:
    """Whether `path` is allowed to name a concrete tracker: the adapters, their tests, or this file."""
    if LEGAL_ZONE in path.parents:
        return True
    if path.parent == ADAPTER_TESTS and path.name.startswith("test_"):
        return True
    return path.resolve() == Path(__file__).resolve()


def _scanned_files() -> list[Path]:
    """Every Python file in the package that is not exempt from the seam rule."""
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


def test_every_test_lives_under_the_tests_package():
    """Tests mirror the package they cover from one root; none are co-located with their source.

    Asserted rather than assumed because the seam exemption above is scoped to one directory: a test
    written beside its adapter would silently keep passing while sitting outside the mirror, and the
    next reader would copy it.
    """
    strays = sorted(
        str(p.relative_to(MCP_ROOT.parent))
        for p in MCP_ROOT.rglob("test_*.py")
        if MCP_ROOT / "tests" not in p.parents
    )
    assert not strays, f"tests must live under sy_tools/tests/, mirroring the package: {strays}"
