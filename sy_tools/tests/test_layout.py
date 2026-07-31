"""Where a test file is allowed to live: under `sy_tools/tests/`, mirroring the package it covers.

One root, never co-located with its source. A layout invariant on its own terms, independent of the
tracker seam that `test_tracker_seam.py` enforces.
"""
from __future__ import annotations

from pathlib import Path

MCP_ROOT = Path(__file__).resolve().parents[1]


def test_every_test_lives_under_the_tests_package():
    """Tests mirror the package they cover from one root; none are co-located with their source.

    Asserted rather than assumed because the seam exemption in `test_tracker_seam.py` is scoped to
    one directory: a test written beside its adapter would silently keep passing while sitting
    outside the mirror, and the next reader would copy it.
    """
    strays = sorted(
        str(p.relative_to(MCP_ROOT.parent))
        for p in MCP_ROOT.rglob("test_*.py")
        if MCP_ROOT / "tests" not in p.parents
    )
    assert not strays, f"tests must live under sy_tools/tests/, mirroring the package: {strays}"
