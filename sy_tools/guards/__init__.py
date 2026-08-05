"""The `PreToolUse` hook guards, run as `python -m sy_tools.guards.<name>`.

Each module here is an entry point that Claude Code spawns with a bare `python` on every matching
tool call, so nothing in this package may reach the MCP server or anything it depends on: only the
standard library plus `sy_tools.config` and `sy_tools.secrets`, which are stdlib-only for exactly
this reason. This file re-exports nothing so that importing one guard never drags in the other.
"""
from __future__ import annotations
