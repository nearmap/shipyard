"""The tracker seam, at the MCP package's location.

CONTRIBUTING.md's rule is that exactly one place knows how to talk to a specific tracker.
`scripts/validate.py`'s `check_seam` enforces that for `skills/tracker/`; this enforces the same
rule for `sy_tools/`, where the legal zone is `sy_tools/tracker/`. It is a pytest check rather than a new
entry in `check_seam`'s scan list so `scripts/validate.py` stays untouched by this change.

This file is the one exemption, for the same reason `check_seam` exempts `validate.py`
(`scripts/validate.py:631`): the scanner has to spell out the tokens it looks for.
"""
from __future__ import annotations

from pathlib import Path
import re

MCP_ROOT = Path(__file__).resolve().parents[1]
LEGAL_ZONE = MCP_ROOT / "tracker"

TRACKER_TOKENS = [
    re.compile(p, f) for p, f in [
        (r"\bjira\b", re.I), (r"\bacli\b", re.I), (r"\batlassian\b", re.I),
        (r"\.atlassian\.net", 0), (r"\bADF\b", 0),
        (r"\bgithub\b", re.I), (r"\bgh\b", re.I), (r"\bgist\b", re.I),
        (r"\bissueType\b", 0), (r"\bsubtask\b", re.I),
    ]
]


def _scanned_files() -> list[Path]:
    """Every Python file in the package outside the legal zone, minus this scanner itself."""
    return sorted(
        p for p in MCP_ROOT.rglob("*.py")
        if LEGAL_ZONE not in p.parents and p.resolve() != Path(__file__).resolve()
    )


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
