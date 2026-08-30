#!/usr/bin/env python3
"""PreToolUse guard for the review agents.

Reads Claude Code hook JSON on stdin and denies obvious source mutation. This is a
backstop, not a shell sandbox: the review prompts still require read-only work.
Interpreter indirection (`bash -c`, `python -c`) is a documented gap pending an
allowlist approach.

Runs as a single plugin-level PreToolUse hook for every agent, so it selects its mode
from the event's agent_type (namespace-stripped, e.g. `sy:gate` -> `gate`) and fails
open — allowing the tool — for any agent that is not a review agent, so build agents
can still mutate. An explicit argv naming a review mode, or `self-test`, still forces
that mode.

The write sandbox the `SANDBOX_WRITE_MODES` subset gets is the repository's own scratch
directory, resolved from the event's cwd (see `scratch_root`) and never from the
environment. Resolving it is the only thing this script asks of the config resolver, and
a resolution it cannot make fails closed. Every other review mode is purely read-only.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys

REVIEW_MODES = {'gate', 'hunt', 'repo-standards', 'repo-review'}
# The subset that may write into the resolved scratch root. Everything in REVIEW_MODES but not here is
# read-only; anything here but not in REVIEW_MODES would be unguarded entirely, which `_self_test` pins.
SANDBOX_WRITE_MODES = {'hunt', 'repo-review'}
WRAPPERS = {'sudo', 'env', 'nice', 'ionice', 'nohup', 'time', 'timeout', 'stdbuf', 'xargs', 'command'}
MUTATING_COMMANDS = {
    'rm', 'mv', 'cp', 'install', 'truncate', 'touch', 'dd', 'rsync', 'ln',
    'mkfifo', 'shred', 'chmod', 'chown',
}
MUTATING_GIT = {
    'checkout', 'switch', 'reset', 'clean', 'add', 'commit', 'cherry-pick', 'rebase',
    'merge', 'push', 'pull', 'restore', 'stash', 'apply', 'am', 'branch', 'tag',
    'worktree', 'rm', 'mv', 'revert', 'update-ref', 'filter-branch',
}
_ASSIGNMENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\+?=.*')
"""A leading `NAME=VALUE` or `NAME+=VALUE` assignment prefix, which names no command to check.

`+=` is here because `secret_guard.py` was found missing it and this file carried the same narrow
pattern: both bash and zsh run `NAME+=VALUE cmd` as an assignment prefix (verified live in both,
while `-=`/`*=`/`/=` are not assignment syntax to either), so the walk below stopped on `FOO+=bar`,
read *that* as the command, matched it against nothing, and allowed whatever followed --
`FOO+=bar rm -rf src` and `FOO+=bar git commit -m x` both went through this guard untouched
(verified before the fix). A missed assignment prefix disarms the mutation check entirely rather
than narrowing it, which is why it is worth fixing here rather than deferring with this file's other
known gaps: this hook gates every review agent."""


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def scratch_root(cwd: str) -> Path | None:
    """The sandbox-write modes' writable root for this event, or None when it cannot be resolved.

    Resolved from the event's own `cwd`, which is the load-bearing part. Claude Code exports
    `CLAUDE_PROJECT_DIR` to a hook subprocess but not to a subagent's own Bash tool, so a root
    derived from the environment would name the main checkout here and the worktree there: inside a
    `/sy:ship` worktree the guard would deny every sandboxed write as an escape while the agent believed
    it was writing inside the sandbox it had been given. `repo_scratch_dir` keys on the logical
    repository, so guard and guarded agree from either without depending on `CLAUDE_PROJECT_DIR` or
    any working-directory convention (absent a `GIT_COMMON_DIR`/`GIT_DIR` override, which neither the
    hook nor the agent sets), and containment stays `scratch_dir`'s own logic.

    None means the root could not be resolved at all, and every caller treats it as deny: a guard
    that cannot say where the sandbox is must not grant a write into it. `SystemExit` is caught
    explicitly because it is no `Exception` subclass and so would escape the catch below; the
    `ConfigError` that is how `sy_tools.config` refuses lands there.
    """
    try:
        from sy_tools import config as sy_config
        return sy_config.repo_scratch_dir(Path(cwd))
    except (SystemExit, Exception):
        return None


def under_scratch(path: str, cwd: str, root: Path | None) -> bool:
    if root is None:
        return False
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate
    try:
        candidate = candidate.resolve(strict=False)
        scratch = root.resolve(strict=False)
        return candidate == scratch or scratch in candidate.parents
    except OSError:
        return False


def _sandbox(root: Path | None) -> str:
    return str(root) if root else 'the repository scratch directory, which could not be resolved'


def decision(mode: str, tool: str, args: dict, cwd: str) -> str | None:
    """Return a deny reason, or None to allow."""
    root = scratch_root(cwd)
    if tool in {'Write', 'Edit', 'MultiEdit', 'NotebookEdit'}:
        path = args.get('file_path') or args.get('path') or args.get('notebook_path') or ''
        if mode in SANDBOX_WRITE_MODES and path and under_scratch(path, cwd, root):
            return None
        return (
            f'{mode} is source-read-only; a sandbox-write mode ({", ".join(sorted(SANDBOX_WRITE_MODES))}) may '
            f'write only under {_sandbox(root)}'
        )
    if tool != 'Bash':
        return None
    return _classify_bash(str(args.get('command', '')), mode, cwd, root)


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg == 'self-test':
        _self_test()
        print('review_guard self-test passed')
        return
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return
    if not isinstance(event, dict):
        return
    mode = arg if arg in REVIEW_MODES else _mode_from_event(event)
    if mode not in REVIEW_MODES:
        return  # fail open: the guard restricts only review agents, never the build agents
    reason = decision(
        mode,
        event.get('tool_name', ''),
        event.get('tool_input') or {},
        event.get('cwd') or os.getcwd(),
    )
    if reason:
        deny(reason)


def _mode_from_event(event: dict) -> str | None:
    raw = event.get('agent_type') or event.get('agentType') or ''
    name = str(raw).split(':')[-1].strip()
    return name if name in REVIEW_MODES else None


def _classify_bash(command: str, mode: str, cwd: str, root: Path | None) -> str | None:
    if re.search(r'\bsed\s+-[^\n;]*i\b', command) or re.search(r'\bperl\s+-[^\n;]*pi\b', command):
        return f'{mode} review: in-place edit mutates files'
    for segment in re.split(r'[;&|\n]+', command):
        reason = _segment_reason(segment)
        if reason:
            return f'{mode} review: {reason}'
    # Shell redirection is allowed to /dev/null, and for a sandbox-write mode to the resolved sandbox root.
    for target in re.findall(r'(?:^|\s)(?:>>?|\btee\s+(?:-a\s+)?)\s*([^\s;&|]+)', command):
        target = target.strip('"\'')
        if target == '/dev/null':
            continue
        if mode in SANDBOX_WRITE_MODES and under_scratch(target, cwd, root):
            continue
        return (
            f'{mode} review: shell redirection is allowed only to /dev/null, or for a sandbox-write mode '
            f'({", ".join(sorted(SANDBOX_WRITE_MODES))}) under {_sandbox(root)}'
        )
    return None


def _segment_reason(segment: str) -> str | None:
    tokens = [t.strip('"\'') for t in segment.split()]
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        base = tok.lstrip('\\').rsplit('/', 1)[-1]
        if (
            _ASSIGNMENT.fullmatch(tok)
            or base in WRAPPERS
            or base.startswith('-')
            or re.fullmatch(r'\d+[smhd]?', base)
        ):
            i += 1
            continue
        break
    else:
        return None
    cmd = tokens[i].lstrip('\\').rsplit('/', 1)[-1]
    rest = tokens[i + 1:]
    if cmd in MUTATING_COMMANDS:
        return f'{cmd} mutates files'
    if cmd == 'git':
        sub = _git_subcommand(rest)
        if sub in MUTATING_GIT:
            return f'git {sub} mutates the checkout or git state'
    if cmd == 'find':
        if '-delete' in rest:
            return 'find -delete mutates files'
        for flag in ('-exec', '-execdir', '-ok', '-okdir'):
            if flag in rest and flag != rest[-1]:
                exe = rest[rest.index(flag) + 1].lstrip('\\').rsplit('/', 1)[-1]
                if exe in MUTATING_COMMANDS or exe == 'git':
                    return f'find {flag} {exe} mutates files'
    return None


def _git_subcommand(rest: list[str]) -> str | None:
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in {'-C', '-c'}:
            i += 2
            continue
        if tok.startswith('-'):
            i += 1
            continue
        return tok
    return None


def _self_test() -> None:
    """Containment is asserted against a temporary sandbox root swapped in for the resolved one.

    The root is monkeypatched and restored rather than resolved, so the cases assert this guard's own
    logic and not whatever `scratch.dir` this checkout happens to configure — and so the
    unresolvable-root case is reachable at all.
    """
    import tempfile

    # A mode granted the sandbox but absent from REVIEW_MODES is never dispatched to `decision` at all: the
    # guard fails open on it and the grant silently becomes unrestricted write. Asserted, not assumed,
    # because the two sets are edited independently and only one of them is what `main` gates on.
    assert SANDBOX_WRITE_MODES <= REVIEW_MODES, (
        f'{sorted(SANDBOX_WRITE_MODES - REVIEW_MODES)} may write the sandbox but is not guarded at all'
    )

    original = globals()['scratch_root']
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / 'scratch' / 'logical-repo'
        root.mkdir(parents=True)
        outside = Path(tmp) / 'outside'
        outside.mkdir()
        (root / 'link').symlink_to(outside, target_is_directory=True)
        globals()['scratch_root'] = lambda cwd: root
        try:
            _run_cases(root)
            globals()['scratch_root'] = lambda cwd: None
            for mode in sorted(SANDBOX_WRITE_MODES):
                for tool, tool_input in (
                    ('Write', {'file_path': str(root / 'repro.py')}),
                    ('Bash', {'command': f'echo data > {root / "out.txt"}'}),
                ):
                    assert decision(mode, tool, tool_input, cwd='/repo') is not None, (
                        f'an unresolvable sandbox root must deny {mode} {tool}, never allow it'
                    )
        finally:
            globals()['scratch_root'] = original


def _run_cases(root: Path) -> None:
    cases = [
        # The hunt sandbox: only paths that resolve strictly inside the root are writable.
        ('hunt', 'Write', {'file_path': str(root / 'repro.py')}, False),
        ('hunt', 'Write', {'file_path': str(root / 'a' / 'b' / 'repro.py')}, False),
        ('hunt', 'Write', {'file_path': str(root / '..' / 'elsewhere' / 'a.py')}, True),
        ('hunt', 'Write', {'file_path': 'src/a.py'}, True),
        ('hunt', 'Write', {'file_path': '/tmp/out.txt'}, True),
        ('hunt', 'Write', {'file_path': str(root / 'link' / 'a.py')}, True),
        ('hunt', 'Write', {'file_path': str(root)}, False),
        ('hunt', 'Bash', {'command': f'echo data > {root / "out.txt"}'}, False),
        ('hunt', 'Bash', {'command': 'echo data > /tmp/out.txt'}, True),
        ('hunt', 'Bash', {'command': f'echo data > {root / "link" / "out.txt"}'}, True),
        # `repo-review` is the second sandbox-write mode: the same containment, keyed on the set and not on
        # the one mode name the two write sites used to compare against.
        ('repo-review', 'Write', {'file_path': str(root / 'repro.py')}, False),
        ('repo-review', 'Write', {'file_path': str(root / '..' / 'elsewhere' / 'a.py')}, True),
        ('repo-review', 'Write', {'file_path': 'src/a.py'}, True),
        ('repo-review', 'Bash', {'command': f'echo data > {root / "out.txt"}'}, False),
        ('repo-review', 'Bash', {'command': 'echo data > /tmp/out.txt'}, True),
        ('repo-review', 'Bash', {'command': 'git commit -m x'}, True),
        ('repo-review', 'Bash', {'command': 'git rev-parse HEAD'}, False),
        # `repo-standards` is guarded but ungranted: being in REVIEW_MODES and not SANDBOX_WRITE_MODES has to
        # deny a write *inside* the root too, which is the direction an accidental `in REVIEW_MODES` test
        # at either write site would invert.
        ('repo-standards', 'Write', {'file_path': str(root / 'repro.py')}, True),
        ('repo-standards', 'Write', {'file_path': 'src/a.py'}, True),
        ('repo-standards', 'Bash', {'command': f'echo data > {root / "out.txt"}'}, True),
        ('repo-standards', 'Bash', {'command': 'rm -rf src'}, True),
        ('repo-standards', 'Bash', {'command': "grep -rn 'foo' skills/"}, False),
        ('gate', 'Write', {'file_path': str(root / 'repro.py')}, True),
        ('gate', 'Bash', {'command': 'git log --oneline -5'}, False),
        ('gate', 'Bash', {'command': 'git diff HEAD~1 -- src/'}, False),
        ('gate', 'Bash', {'command': "rg -n 'foo' src/"}, False),
        ('gate', 'Bash', {'command': "grep -rn 'rm -rf docs' src/"}, False),
        ('gate', 'Bash', {'command': 'cat notes/copy.txt'}, False),
        ('gate', 'Bash', {'command': 'pytest tests/ -x > /dev/null'}, False),
        ('gate', 'Bash', {'command': 'timeout 30 pytest -q'}, False),
        ('gate', 'Bash', {'command': "find src -name '*.py' -exec grep -l foo {} +"}, False),
        ('gate', 'Bash', {'command': 'git commit -m x'}, True),
        ('gate', 'Bash', {'command': 'git -C /repo commit -m x'}, True),
        ('gate', 'Bash', {'command': 'git stash'}, True),
        ('gate', 'Bash', {'command': 'git apply patch.diff'}, True),
        ('gate', 'Bash', {'command': 'git worktree add /tmp/x'}, True),
        ('gate', 'Bash', {'command': 'git checkout main'}, True),
        ('gate', 'Bash', {'command': 'rm -rf src'}, True),
        ('gate', 'Bash', {'command': 'sudo rm -rf src'}, True),
        # An assignment prefix names no command, so the walk must step over it and check what follows.
        # Only `NAME=` was recognised, so `NAME+=` -- which both bash and zsh accept -- left the walk
        # holding `FOO+=bar` as the apparent command and let the mutation behind it through.
        ('gate', 'Bash', {'command': 'FOO=bar rm -rf src'}, True),
        ('gate', 'Bash', {'command': 'FOO+=bar rm -rf src'}, True),
        ('gate', 'Bash', {'command': 'FOO+=bar git commit -m x'}, True),
        ('gate', 'Bash', {'command': 'FOO+=bar sudo rm -rf src'}, True),
        ('gate', 'Bash', {'command': 'FOO+=bar ls'}, False),
        ('gate', 'Bash', {'command': 'FOO+=bar git log --oneline -5'}, False),
        ('gate', 'Bash', {'command': 'xargs rm < list.txt'}, True),
        ('gate', 'Bash', {'command': '/bin/rm src/a.py'}, True),
        ('gate', 'Bash', {'command': "find . -name '*.pyc' -delete"}, True),
        ('gate', 'Bash', {'command': 'find . -name x -exec rm {} +'}, True),
        ('gate', 'Bash', {'command': 'dd if=/dev/zero of=src/a.py'}, True),
        ('gate', 'Bash', {'command': 'touch src/a.py'}, True),
        ('gate', 'Bash', {'command': 'echo hi > src/a.py'}, True),
        ('gate', 'Bash', {'command': 'echo x | tee src/a.py'}, True),
        ('gate', 'Bash', {'command': 'cd /tmp && git commit -m x'}, True),
        ('gate', 'Bash', {'command': "sed -i 's/a/b/' src/a.py"}, True),
    ]
    for mode, tool, tool_input, want_deny in cases:
        got = decision(mode, tool, tool_input, cwd='/repo')
        assert (got is not None) == want_deny, f'{mode} {tool} {tool_input!r} -> {got!r}'


if __name__ == '__main__':
    main()
