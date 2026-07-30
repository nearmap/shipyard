"""The GitHub adapter for the MCP deployment.

Inside `mcp/tracker/`, so this is one of the few places in the package allowed to name a
concrete tracker (`mcp/tests/test_seam.py` enforces that boundary). Nothing imports from here
except `mcp.tracker.adapter()`, the single selection point.
"""
from __future__ import annotations
