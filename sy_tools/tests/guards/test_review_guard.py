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


@pytest.mark.parametrize('mode', sorted(review_guard.SANDBOX_WRITE_MODES))
def test_every_sandbox_write_mode_is_contained_by_the_resolved_root(mode):
    """Parametrized over the live set, so a mode granted `Write` later inherits containment automatically."""
    cwd = str(Path(__file__).resolve().parent)
    root = config.repo_scratch_dir(Path(cwd))
    assert review_guard.decision(mode, 'Write', {'file_path': str(root / 'findings.md')}, cwd=cwd) is None
    assert review_guard.decision(mode, 'Write', {'file_path': '/tmp/out.txt'}, cwd=cwd) is not None
    escape = {'file_path': str(root / '..' / 'elsewhere' / 'a.py')}
    assert review_guard.decision(mode, 'Write', escape, cwd=cwd) is not None


def test_repo_standards_is_refused_a_write_even_inside_the_sandbox_root():
    """In `REVIEW_MODES` but not `SANDBOX_WRITE_MODES`; keying either write site on the wrong set inverts this."""
    cwd = str(Path(__file__).resolve().parent)
    root = config.repo_scratch_dir(Path(cwd))
    assert review_guard.decision('repo-standards', 'Write', {'file_path': str(root / 'x.md')}, cwd=cwd) is not None
    redirect = {'command': f'echo x > {root / "x.md"}'}
    assert review_guard.decision('repo-standards', 'Bash', redirect, cwd=cwd) is not None
    assert review_guard.decision('repo-standards', 'Bash', {'command': 'grep -rn x skills/'}, cwd=cwd) is None


@pytest.mark.parametrize('mode', sorted(review_guard.SANDBOX_WRITE_MODES))
@pytest.mark.parametrize('command', [
    'echo x 2> /etc/o',
    'echo x 1>/etc/o',
    'echo x &> /etc/o',
    'echo x >& /etc/o',
    'echo x 2>> /etc/o',
])
def test_fd_prefixed_redirection_out_of_the_sandbox_is_refused(mode, command):
    """`2>`, `&>` and `>&` write a file exactly as `>` does; reading only the bare `>` let them escape."""
    cwd = str(Path(__file__).resolve().parent)
    assert review_guard.decision(mode, 'Bash', {'command': command}, cwd=cwd) is not None


@pytest.mark.parametrize('mode', sorted(review_guard.REVIEW_MODES))
@pytest.mark.parametrize('command', [
    'pytest -q 2>&1',
    'pytest -q > /dev/null 2>&1',
    'pytest -q >&1',
])
def test_fd_duplication_is_not_read_as_a_redirect_to_a_file(mode, command):
    """An `&`-form operator with a bare-digit target names no file, so there is nothing to contain."""
    cwd = str(Path(__file__).resolve().parent)
    assert review_guard.decision(mode, 'Bash', {'command': command}, cwd=cwd) is None


@pytest.mark.parametrize('mode', sorted(review_guard.REVIEW_MODES))
@pytest.mark.parametrize('command', [
    'echo x > 1',
    'echo x 2> 1',
    'echo x 2>>1',
])
def test_a_digit_target_without_the_ampersand_is_a_file_named_for_that_digit(mode, command):
    """`2>1` writes a file called `1` in the cwd; exempting every bare digit let that escape the sandbox."""
    cwd = str(Path(__file__).resolve().parent)
    assert review_guard.decision(mode, 'Bash', {'command': command}, cwd=cwd) is not None


@pytest.mark.parametrize('mode', sorted(review_guard.REVIEW_MODES))
@pytest.mark.parametrize('command', [
    'gh pr comment 32 --body-file report.md',
    'gh pr merge 32 --squash',
    'gh release delete-asset v1.0.0 asset.zip',
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
    'gh api -f query=x graphql',
    'gh api --input body.json graphql',
])
def test_every_review_mode_keeps_its_remote_reads(mode, command):
    """`gh api graphql` is the load-bearing one: exempt by its literal command shape, not by inspecting
    the query, so an unmethoded field-carrying call to it is allowed whatever the query underneath
    actually does. An explicit mutating `--method` is denied on the leg above, graphql or not.

    Including the flag-first spellings: a value-taking flag before `graphql` must not shift it out of the
    position the exemption reads, quoted header value and all -- `gh api`'s own field flags included,
    which were absent from the skip set and so denied this read outright.
    """
    assert review_guard.decision(mode, 'Bash', {'command': command}, cwd='/repo') is None
