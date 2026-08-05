"""`secret_guard`'s own assertion corpus, run under pytest as well as from its `self-test` argv."""
from __future__ import annotations

from sy_tools.guards import secret_guard


def test_the_guards_own_self_test_passes():
    """Every allow/deny case, the fail-closed paths, and the config-degradation warning."""
    # Private by name only: this corpus *is* the functionality under test.
    secret_guard._self_test()
