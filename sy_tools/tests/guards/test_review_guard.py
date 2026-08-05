"""`review_guard`'s own assertion corpus, run under pytest as well as from its `self-test` argv.

The private `_self_test` is imported deliberately: it *is* the functionality under test here, and
`scripts/validate.py` invoking the `self-test` subcommand was the only place it ran.
"""
from __future__ import annotations

from sy_tools.guards import review_guard


def test_the_guards_own_self_test_passes():
    """Every gate/hunt case, plus the unresolvable-sandbox-root denial."""
    review_guard._self_test()
