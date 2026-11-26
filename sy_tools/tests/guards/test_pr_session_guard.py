"""`pr_session_guard`'s own assertion corpus, run under pytest as well as from its `self-test` argv."""
from __future__ import annotations

from sy_tools.guards import pr_session_guard as guard


def test_a_matching_session_allows_silently():
    guard._test_a_matching_session_allows_silently()


def test_a_mismatched_or_missing_session_denies():
    guard._test_a_mismatched_or_missing_session_denies()


def test_both_server_prefixes_are_guarded_and_nothing_else():
    guard._test_both_server_prefixes_are_guarded_and_nothing_else()


def test_a_payload_without_session_id_allows_and_warns():
    guard._test_a_payload_without_session_id_allows_and_warns()


def test_the_hook_json_output_is_the_deny_contract_claude_code_reads():
    guard._test_the_hook_json_output_is_the_deny_contract_claude_code_reads()


def test_the_guards_own_self_test_passes():
    """Its own `self-test` argv corpus, so a case added there is caught here and not only by hand."""
    guard._self_test()
