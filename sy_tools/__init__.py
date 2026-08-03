"""Shipyard's `sy` MCP server.

One server, not one per tracker. Everything tracker-specific lives under `sy_tools/tracker/<name>/`
and is reached only through `sy_tools.tracker.adapter()`, mirroring the seam rule CONTRIBUTING.md
states for `skills/tracker/`. `sy_tools/tests/test_tracker_seam.py` enforces it mechanically.

Every canonical verb's one implementation lives here. `skills/tracker/**` is the documentation
zone for the same seam — what each tracker does with a verb — and carries no executable path of its
own; `CONTRIBUTING.md` states both zones and what enforces each.
"""
from __future__ import annotations

SERVER_NAME = "sy"
SERVER_VERSION = "0.1.0"

__all__ = ["SERVER_NAME", "SERVER_VERSION"]
