"""Tests for the `PreToolUse` hook guards, mirroring `sy_tools/guards/`.

Each module here wraps the guard's own `_self_test()` rather than restating its cases: the guards
keep a `self-test` argv path because Claude Code runs them with a bare `python`, and a contributor
checking one has no pytest in that picture. Wrapping keeps one owner for the corpus and two ways to
run it, so the assertions cannot drift apart.
"""
