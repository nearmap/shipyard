"""`review_guard`'s own assertion corpus, run under pytest as well as from its `self-test` argv."""
from __future__ import annotations

from pathlib import Path

import pytest

from sy_tools import config
from sy_tools.guards import review_guard


def test_the_guards_own_self_test_passes():
    """Every review-mode case, plus the sandbox-set invariant and the unresolvable-root denial."""
    # Private by name only: this corpus *is* the functionality under test.
    review_guard._self_test()


def test_repo_review_writes_into_the_root_the_resolver_itself_reports():
    """Anchored on `repo_scratch_dir`, never a literal path: the agent brief resolves the same function.

    A literal here would keep passing while the two drifted apart, which is the exact failure -- guard and
    guarded disagreeing about where the sandbox is -- that this pair of call sites exists to prevent.
    """
    cwd = str(Path(__file__).resolve().parent)
    root = config.repo_scratch_dir(Path(cwd))
    inside = {'command': f'echo x > {root / "review.json"}'}
    assert review_guard.decision('repo-review', 'Bash', inside, cwd=cwd) is None
    assert review_guard.decision('repo-review', 'Write', {'file_path': str(root / 'findings.md')}, cwd=cwd) is None
    # The task-keyed sibling the agent brief warns against: one directory over, and wholly outside the sandbox.
    sibling = {'file_path': str(root.parent / 'AM-0000' / 'findings.md')}
    assert review_guard.decision('repo-review', 'Write', sibling, cwd=cwd) is not None


def test_repo_standards_is_refused_a_write_even_inside_the_sandbox_root():
    """In `REVIEW_MODES` but not `SANDBOX_WRITE_MODES`; keying either write site on the wrong set inverts this."""
    cwd = str(Path(__file__).resolve().parent)
    root = config.repo_scratch_dir(Path(cwd))
    assert review_guard.decision('repo-standards', 'Write', {'file_path': str(root / 'x.md')}, cwd=cwd) is not None
    redirect = {'command': f'echo x > {root / "x.md"}'}
    assert review_guard.decision('repo-standards', 'Bash', redirect, cwd=cwd) is not None
    assert review_guard.decision('repo-standards', 'Bash', {'command': 'grep -rn x skills/'}, cwd=cwd) is None


@pytest.mark.parametrize('mode', sorted(review_guard.REVIEW_MODES))
@pytest.mark.parametrize('command', [
    'gh pr comment 32 --body-file report.md',
    'gh pr merge 32 --squash',
    'gh api -X POST repos/o/r/issues/1/comments',
    # No method, but a field flag makes gh send a POST: the REST spelling of `gh pr review --approve`.
    'gh api repos/o/r/pulls/32/reviews -f event=APPROVE -f body=lgtm',
    'gh api repos/o/r/issues/1/comments -f body=x',
    'gh pr create --title x --body y',
    'gh pr lock 32',
    'gh pr unlock 32',
    'gh pr revert 32',
    'gh pr update-branch 32',
    'curl -X POST https://example.test/x',
    'curl -d @body.json https://example.test/x',
    # Flag and value quoted into one shell word: `-d hello` is a real POST, and matching the whole word
    # against the body-flag set missed it.
    'curl "-d hello" https://example.test/x',
    # Bundled and glued curl short options: `-sXPOST` is `-s -X POST`, `-d@body.json` is `-d @body.json`.
    # A flag is neither the whole token nor its leading characters in any of these, and all four POST live.
    'curl -d\'{"a":1}\' https://example.test/x',
    'curl -d@body.json https://example.test/x',
    'curl -Fk=v https://example.test/x',
    'curl -sXPOST https://example.test/x',
    'curl -skXDELETE https://example.test/x',
])
def test_every_review_mode_is_refused_a_remote_write(mode, command):
    """Parametrized over the live set, so a review mode added later inherits this coverage rather than missing it."""
    assert review_guard.decision(mode, 'Bash', {'command': command}, cwd='/repo') is not None


@pytest.mark.parametrize('mode', sorted(review_guard.REVIEW_MODES))
@pytest.mark.parametrize('command', [
    'gh pr view 32',
    'gh pr diff 32',
    'gh pr checks 32',
    'gh api repos/o/r/pulls/32/comments',
    'gh api repos/o/r/pulls/32',
    'gh api graphql -f query=query{viewer{login}}',
    'gh api -H "Accept: application/vnd.v3+json" graphql -f query=query{viewer{login}}',
    'gh api --jq .data graphql -f query=query{viewer{login}}',
    'gh api repos/o/r/pulls/32 --method GET -f foo=bar',
    # An ordinary header carrying a `;`, with no body or method flag: masking it must not invent a write.
    'curl -H "Content-Type: application/json; charset=utf-8" https://example.test/x',
])
def test_every_review_mode_keeps_its_remote_reads(mode, command):
    """`gh api graphql` is the load-bearing one: a field-carrying read over POST that names no method.

    Including the flag-first spellings: a value-taking flag before `graphql` must not shift it out of the
    position the exemption reads, quoted header value and all.
    """
    assert review_guard.decision(mode, 'Bash', {'command': command}, cwd='/repo') is None


@pytest.mark.parametrize('command', [
    # A charset directive in a Content-Type header is ordinary, and splitting the raw string on it stranded
    # `curl` and its `-d` in different segments, so the POST was never seen (verified live).
    'curl -H "Content-Type: application/json; charset=utf-8" -d \'{"body":"x"}\' https://example.test/x',
    'git -c user.name="a; b" commit -m x',
])
def test_a_separator_inside_a_quoted_value_does_not_split_the_command(command):
    assert review_guard.decision('gate', 'Bash', {'command': command}, cwd='/repo') is not None


def test_unbalanced_quoting_still_denies_through_the_pre_existing_fallback():
    """Nothing to mask when no closing quote exists, so the split -- and `_tokens`' fallback -- behave as before."""
    assert review_guard._mask_quoted('git commit -m "a; b') == ('git commit -m "a; b', {})
    assert review_guard.decision('gate', 'Bash', {'command': 'git commit -m "a; b'}, cwd='/repo') is not None
