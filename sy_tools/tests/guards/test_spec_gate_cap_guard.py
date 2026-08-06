"""`spec_gate_cap_guard`'s own assertion corpus, run under pytest as well as from its `self-test` argv.

Each case lives in the guard so both entry points assert the same thing; the fixture here is only the
isolation the `self-test` argv does for itself — a temporary ledger root and a pinned cap, so no test
touches the real ~/.claude/shipyard/spec-gate-dispatch-count/ or this checkout's configured cap.
"""
from __future__ import annotations

import pytest

from sy_tools.guards import spec_gate_cap_guard as guard


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "LEDGER_ROOT", tmp_path / "ledger")
    return tmp_path / "ledger"


@pytest.fixture
def pinned_cap(ledger, monkeypatch):
    monkeypatch.setattr(guard, "_resolved_cap", lambda: (guard._SELF_TEST_CAP, None))


def test_a_dispatch_that_is_not_spec_gate_allows_at_any_count(pinned_cap):
    guard._test_a_dispatch_that_is_not_spec_gate_allows_at_any_count()


def test_both_agent_tool_names_and_namespaced_types_are_recognized(pinned_cap):
    guard._test_both_agent_tool_names_and_namespaced_types_are_recognized()


def test_the_cap_th_dispatch_is_the_last_one_allowed(pinned_cap):
    """The cap+1-th deny must name the count, the cap, and the three dispositions."""
    guard._test_the_cap_th_dispatch_is_the_last_one_allowed()


def test_a_corrupted_ledger_counts_as_zero_prior_dispatches(pinned_cap):
    guard._test_a_corrupted_ledger_counts_as_zero_prior_dispatches()


def test_two_sessions_do_not_share_one_budget(pinned_cap):
    guard._test_two_sessions_do_not_share_one_budget()


def test_an_unresolvable_cap_allows_and_reports_it(ledger):
    """The live config resolver, so this one runs without the pinned cap."""
    guard._test_an_unresolvable_cap_allows_and_reports_it()
