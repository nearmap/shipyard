"""Shipyard's `sy` MCP server.

One server, not one per tracker. Everything tracker-specific lives under `mcp/tracker/<name>/`
and is reached only through `mcp.tracker.adapter()`, mirroring the seam rule CONTRIBUTING.md
states for `skills/tracker/`. `mcp/tests/test_seam.py` enforces it mechanically.

This package deliberately duplicates rather than imports the equivalent `scripts/` and
`skills/tracker/**` helpers: those stay shipped and unmodified so the CLI deployment keeps
working while the MCP deployment lands beside it.
"""
from __future__ import annotations

SERVER_NAME = "sy"
SERVER_VERSION = "0.1.0"

__all__ = ["SERVER_NAME", "SERVER_VERSION"]
