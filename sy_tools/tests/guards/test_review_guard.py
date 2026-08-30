"""`review_guard`'s own assertion corpus, run under pytest as well as from its `self-test` argv."""
from __future__ import annotations

from pathlib import Path

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
