"""`secret_guard`'s own assertion corpus, run under pytest as well as from its `self-test` argv.

The private `_self_test` is imported deliberately: it *is* the functionality under test here, and
`scripts/validate.py` invoking the `self-test` subcommand was the only place it ran.
"""
from __future__ import annotations

from sy_tools.guards import secret_guard


def test_the_guards_own_self_test_passes():
    """Every allow/deny case, the fail-closed paths, and the config-degradation warning."""
    secret_guard._self_test()
