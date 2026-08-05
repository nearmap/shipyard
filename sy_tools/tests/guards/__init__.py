"""Tests for the `PreToolUse` hook guards, mirroring `sy_tools/guards/`.

Each module here wraps the guard's own `_self_test()` rather than restating its cases, so the corpus
has one owner and two ways to run it: pytest here, and the `self-test` argv the guards keep because
Claude Code runs them with a bare `python` that has no pytest in the picture.
"""
