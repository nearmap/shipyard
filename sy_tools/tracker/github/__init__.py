"""The GitHub adapter for the MCP deployment.

Inside `sy_tools/tracker/`, so this is one of the few places in the package allowed to name a
concrete tracker (`sy_tools/tests/test_tracker_seam.py` enforces that boundary). Nothing imports from here
except `sy_tools.tracker.adapter()`, the single selection point.
"""
from __future__ import annotations
