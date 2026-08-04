#!/usr/bin/env python3
"""PreToolUse guard for the gate/hunt review agents.

Reads Claude Code hook JSON on stdin and denies obvious source mutation. This is a
backstop, not a shell sandbox: the review prompts still require read-only work.
Interpreter indirection (`bash -c`, `python -c`) is a documented gap pending an
allowlist approach.

Runs as a single plugin-level PreToolUse hook for every agent, so it selects its mode
from the event's agent_type (namespace-stripped, e.g. `sy:gate` -> `gate`) and fails
open — allowing the tool — for any agent that is not a review agent, so build agents
can still mutate. An explicit `gate`/`hunt`/`self-test` argv still forces that mode.

Hunt's write sandbox is the repository's own scratch directory, resolved from the
event's cwd (see `scratch_root`) and never from the environment. Resolving it is the
only thing this script asks of the config resolver, and a resolution it cannot make
fails closed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys

REVIEW_MODES = {'gate', 'hunt'}
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


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def scratch_root(cwd: str) -> Path | None:
    """Hunt's write sandbox for this event, or None when it cannot be resolved.

    Resolved from the event's own `cwd`, which is the load-bearing part. Claude Code exports
    `CLAUDE_PROJECT_DIR` to a hook subprocess but not to a subagent's own Bash tool, so a root
    derived from the environment would name the main checkout here and the worktree there: inside a
    `/sy:ship` worktree the guard would deny every hunt write as an escape while the agent believed
    it was writing inside the sandbox it had been given. `repo_scratch_dir` keys on the shared git
    dir, so guard and guarded agree from either, and containment stays `scratch_dir`'s own logic.

    None means the root could not be resolved at all, and every caller treats it as deny: a guard
    that cannot say where the sandbox is must not grant a write into it. `SystemExit` is caught
    explicitly because that is how `sy_config` refuses.
    """
    try:
        import sy_config
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
        if mode == 'hunt' and path and under_scratch(path, cwd, root):
            return None
        return f'{mode} is source-read-only; in hunt mode writes are allowed only under {_sandbox(root)}'
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
    # Shell redirection is allowed to /dev/null, and for hunt to the resolved sandbox root.
    for target in re.findall(r'(?:^|\s)(?:>>?|\btee\s+(?:-a\s+)?)\s*([^\s;&|]+)', command):
        target = target.strip('"\'')
        if target == '/dev/null':
            continue
        if mode == 'hunt' and under_scratch(target, cwd, root):
            continue
        return (
            f'{mode} review: shell redirection is allowed only to /dev/null, or in hunt mode under '
            f'{_sandbox(root)}'
        )
    return None


def _segment_reason(segment: str) -> str | None:
    tokens = [t.strip('"\'') for t in segment.split()]
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        base = tok.lstrip('\\').rsplit('/', 1)[-1]
        if (
            re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*=.*', tok)
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

    Monkeypatched rather than resolved, mirroring `sy_preflight.py::_self_test`'s save/restore, so the
    cases assert this guard's own logic and not whatever `scratch.dir` this checkout happens to
    configure — and so the unresolvable-root case is reachable at all.
    """
    import tempfile

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
            for tool, tool_input in (
                ('Write', {'file_path': str(root / 'repro.py')}),
                ('Bash', {'command': f'echo data > {root / "out.txt"}'}),
            ):
                assert decision('hunt', tool, tool_input, cwd='/repo') is not None, (
                    f'an unresolvable sandbox root must deny {tool}, never allow it'
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
