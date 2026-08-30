#!/usr/bin/env python3
"""PreToolUse guard for the review agents.

Reads Claude Code hook JSON on stdin and denies obvious mutation on two sides: the local
checkout (filesystem verbs and mutating `git` subcommands) and the remote (`gh`
subcommands that write, and `curl`/`wget` carrying a mutating method or a request body).
This is a backstop, not a shell sandbox: the review prompts still require read-only work.

Both sides are deny-lists, so an unrecognised command is allowed. That direction is
deliberate for an agent whose job is reading: a missed write shape is a gap, but a wrongly
denied read breaks the review outright. The remote leg keys on the method a command names
and never on whether a request is a POST underneath, which is what keeps `gh api graphql`
-- a read carried over POST, and how review threads are enumerated -- working.

Interpreter indirection (`bash -c`, `python -c`) is a documented gap pending an allowlist
approach, and it covers the remote side equally: nothing here inspects what an interpreter
would go on to run.

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
MUTATING_GH = {
    'pr': {'merge', 'close', 'create', 'edit', 'ready', 'comment', 'review', 'reopen', 'lock', 'unlock'},
    'release': {'create', 'edit', 'delete', 'upload'},
}
# Issue-level subcommands are deliberately absent, not overlooked: naming them here would put
# tracker-native vocabulary in a core module, which the seam rule forbids.
# They stay reachable, and the `gh api` method leg below still covers the same writes over REST.
MUTATING_HTTP_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}
# Per command, because the same short flag means different things: `-d` is a request body to curl and
# `--debug` to wget, so one shared set would deny a plain wget read.
REMOTE_BODY_FLAGS = {
    'curl': {'-d', '--data', '--data-raw', '--data-binary', '--data-ascii', '--data-urlencode',
             '-F', '--form', '-T', '--upload-file'},
    'wget': {'--post-data', '--post-file', '--body-data', '--body-file'},
}
REMOTE_METHOD_FLAGS = {'curl': ('-X', '--request'), 'wget': ('--method',)}
# `gh api` field flags. Per gh's own documentation the request method "is GET normally and POST if any
# parameters were added", so one of these with no explicit method is a write however it reads.
GH_API_FIELD_FLAGS = ('-f', '--field', '-F', '--raw-field', '--input')
# `gh` global flags taking a separate value, skipped when locating the subcommand so that
# `gh -R owner/repo pr merge` is read as `pr merge` rather than as the repo argument.
_GH_VALUE_FLAGS = {'-R', '--repo', '--hostname'}

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
    if cmd in REMOTE_BODY_FLAGS or cmd == 'gh':
        return _remote_reason(cmd, rest)
    if cmd == 'find':
        if '-delete' in rest:
            return 'find -delete mutates files'
        for flag in ('-exec', '-execdir', '-ok', '-okdir'):
            if flag in rest and flag != rest[-1]:
                exe = rest[rest.index(flag) + 1].lstrip('\\').rsplit('/', 1)[-1]
                if exe in MUTATING_COMMANDS or exe == 'git':
                    return f'find {flag} {exe} mutates files'
    return None


def _remote_reason(cmd: str, rest: list[str]) -> str | None:
    """A deny reason for a remote-side mutation, or None to allow.

    A deny-list: an unrecognised remote command, subcommand or flag is allowed. Reads are the reviewer's
    whole job, so a shape this does not recognise fails open rather than breaking one.
    """
    if cmd == 'gh':
        words = _gh_words(rest)
        group = words[0] if words else None
        if group == 'api':
            # On the named method, plus the implicit POST a field flag makes of an unmethoded call.
            # `gh api graphql` is exempt: it is a read carried over POST that passes its query variables
            # through the same `-f`/`-F` flags, and denying it would break review-thread enumeration.
            method = _flag_value(rest, ('-X', '--method'))
            if method is not None and method.upper() != 'GET':
                return f'gh api --method {method.upper()} writes to the remote'
            if method is None and words[1:2] != ['graphql'] and _flag_value(rest, GH_API_FIELD_FLAGS) is not None:
                return (
                    'gh api with a field flag and no --method is a POST, which writes to the remote; '
                    'name --method GET for a read'
                )
            return None
        sub = words[1] if len(words) > 1 else None
        if group in MUTATING_GH and sub in MUTATING_GH[group]:
            return f'gh {group} {sub} writes to the remote; a review reports, it never changes what it reviews'
        return None
    method = _flag_value(rest, REMOTE_METHOD_FLAGS[cmd])
    if method is not None and method.upper() in MUTATING_HTTP_METHODS:
        return f'{cmd} --request {method.upper()} writes to the remote'
    for tok in rest:
        flag = tok.split('=', 1)[0]
        if flag in REMOTE_BODY_FLAGS[cmd]:
            return f'{cmd} {flag} sends a request body, which writes to the remote'
    return None


def _gh_words(rest: list[str]) -> list[str]:
    """`gh`'s positional words, with global flags and their values stepped over."""
    words: list[str] = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in _GH_VALUE_FLAGS:
            i += 2
            continue
        if tok.startswith('-'):
            i += 1
            continue
        words.append(tok)
        i += 1
    return words


def _flag_value(rest: list[str], flags: tuple[str, ...]) -> str | None:
    """The value of the first of `flags` present, across the `-X V`, `-XV` and `--flag=V` spellings."""
    for i, tok in enumerate(rest):
        for flag in flags:
            if tok == flag:
                return rest[i + 1] if i + 1 < len(rest) else ''
            if tok.startswith(f'{flag}='):
                return tok[len(flag) + 1:]
            # `-XPOST`: a short flag with its value glued on, which curl and gh both accept.
            if len(flag) == 2 and tok.startswith(flag) and len(tok) > 2:
                return tok[2:]
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
            _run_remote_cases()
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


def _run_remote_cases() -> None:
    """The remote leg, both directions, for every review mode -- new modes included automatically.

    The allowed half is the load-bearing half: this is a deny-list guarding an agent whose entire job is
    reading, so a case that wrongly denies a read is a worse defect than one that misses a write.
    """
    deny = [
        'gh pr merge 32 --squash', 'gh pr close 32', 'gh pr edit 32 --body x', 'gh pr ready 32',
        'gh pr comment 32 --body-file report.md', 'gh pr review 32 --approve', 'gh pr reopen 32',
        'gh pr create --title x --body y', 'gh pr lock 32', 'gh pr unlock 32',
        'gh release create v1',
        'gh api -X POST repos/o/r/issues/1/comments', 'gh api --method DELETE repos/o/r/issues/1',
        'gh api -XPATCH repos/o/r/pulls/comments/1', 'gh api --method=PUT repos/o/r/x',
        # No method named, but a field flag makes each one a POST: a REST approval and a REST comment.
        'gh api repos/o/r/pulls/32/reviews -f event=APPROVE -f body=lgtm',
        'gh api repos/o/r/issues/1/comments -f body=x',
        # A global flag with its own value must not be mistaken for the subcommand.
        'gh -R nearmap/shipyard pr merge 32',
        'curl -X POST https://example.test/x', 'curl --request PUT https://example.test/x',
        'curl -XDELETE https://example.test/x', 'curl -d @body.json https://example.test/x',
        'curl --data-binary @body.json https://example.test/x', 'curl -F k=v https://example.test/x',
        'curl -T upload.txt https://example.test/x', 'curl --data-urlencode k=v https://example.test/x',
        'wget --post-data=x https://example.test/x', 'wget --method=DELETE https://example.test/x',
    ]
    allow = [
        'gh pr view 32', 'gh pr diff 32', 'gh pr checks 32', 'gh pr list --state open',
        'gh run view 12345 --log-failed',
        # No method named at all, so nothing to deny on -- including graphql, a read carried over POST.
        'gh api repos/o/r/pulls/32/comments', 'gh api graphql -f query=query{viewer{login}}',
        'gh api -X GET repos/o/r', 'gh api --method GET repos/o/r',
        'gh api repos/o/r/pulls/32', 'gh api repos/o/r/pulls/32 --method GET -f foo=bar',
        'curl https://example.test/x', 'curl -s -L https://example.test/x',
        'curl -X GET https://example.test/x', 'wget https://example.test/x',
        # `-d` is a request body to curl and `--debug` to wget; a shared flag set would deny this read.
        'wget -d https://example.test/x',
    ]
    for mode in sorted(REVIEW_MODES):
        for command, want_deny in [(c, True) for c in deny] + [(c, False) for c in allow]:
            got = decision(mode, 'Bash', {'command': command}, cwd='/repo')
            assert (got is not None) == want_deny, (
                f'{mode} remote: {command!r} -> {got!r} (wanted {"deny" if want_deny else "allow"})'
            )


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
