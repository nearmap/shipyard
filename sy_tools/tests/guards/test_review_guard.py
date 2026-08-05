"""`review_guard`'s own assertion corpus, run under pytest as well as from its `self-test` argv."""
from __future__ import annotations

from sy_tools.guards import review_guard


def test_the_guards_own_self_test_passes():
    """Every gate/hunt case, plus the unresolvable-sandbox-root denial."""
    # Private by name only: this corpus *is* the functionality under test.
    review_guard._self_test()
