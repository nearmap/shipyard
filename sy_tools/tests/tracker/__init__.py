"""Tests for the tracker adapters. One module per adapter, mirroring `sy_tools/tracker/`.

These modules are the one place outside `sy_tools/tracker/` where tracker-native vocabulary is
legal: they exercise a specific adapter, so they have to name it. `sy_tools/tests/test_seam.py`
exempts exactly `test_*.py` under this directory and nothing else.
"""
