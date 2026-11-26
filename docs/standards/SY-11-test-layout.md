---
id: SY-11
severity: MED
---
# SY-11 Tests mirror the package they cover

Tests live under `sy_tools/tests/`, mirroring the package they cover, never beside their source. `sy_tools/tests/tracker/` mirrors `sy_tools/tracker/` and is the one place a test may name a concrete tracker; `sy_tools/tests/test_tracker_seam.py` scopes that exemption to it.

Enforced by: `sy_tools/tests/test_layout.py`.
