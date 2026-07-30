#!/usr/bin/env python3
"""PreToolUse guard: deny a Bash command that would print a secret-shaped env var's value.

Once any command prints a secret, that value is a permanent, byte-for-byte part of this
session's transcript from that point on — every later render (a HANDOFF attachment, an export)
reproduces it, regardless of whether it was ever used or uploaded anywhere. `scrub_known_secrets.py`
cleans this up after the fact, right before a rendered transcript is scanned and attached; this
hook exists to stop the value from ever reaching a tool-call result in the first place.

It denies exactly the two anti-patterns this repo's own docs already warn against
(docs/configuration.md, skills/tracker/jira/references/attachments.md): dumping the environment
(`env`, `printenv`, `set`, `export` with no args) and echoing a secret-shaped variable directly
(`echo $ACLI_TOKEN`). The denial message points at the safe alternative that already exists for
Jira specifically — `sy_preflight.py check` / `jira_rest.py preflight`, or a bare presence check
(`[ -n "$VAR" ]`) for anything else.

Name-based, not value-based, like `scrub_known_secrets.py`'s own discovery: this hook never reads
the actual environment, only the command string, so it fires the same way whether or not a secret
happens to be set right now.

Also covers the same leak in an interpreter idiom — `python -c "import os; print(os.environ['TOKEN'])"`,
`node -e "console.log(process.env.TOKEN)"` — since that's a very plausible next move once the
plain `echo`/`env` form is denied, not a deliberate sandbox escape.

This is a backstop over the documented anti-patterns, not a shell sandbox: encoded indirection
(`base64`, sourcing a script that does the printing, a nested `bash -c` two levels deep) is a
known gap, the same category `review_guard.py` documents for its own `bash -c` indirection gap.

Commands:
  (no args)   read Claude Code hook JSON from stdin; deny if the Bash command matches
  self-test
"""
from __future__ import annotations

import json
import re
import shlex
import sys

from secret_words import looks_like_secret_name as _base_looks_like_secret_name

WRAPPERS = {"sudo", "nice", "ionice", "nohup", "time", "timeout", "stdbuf", "command"}
PRINTING_COMMANDS = {"echo", "printf", "print"}
_ENV_ARG_FLAGS = {"-u", "-C", "-S", "--unset", "--chdir", "--split-string"}
INTERPRETERS = {"python", "python3", "node", "ruby", "perl"}
CODE_FLAGS = {"-c", "-e", "--eval"}
_VAR_REF = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
_ENV_ACCESS = re.compile(
    r"os\.environ(?:\.get)?\s*[\[\(]\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"
    r"|os\.getenv\s*\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"
    r"|process\.env\.([A-Za-z_][A-Za-z0-9_]*)"
    r"|process\.env\[['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\]"
)
_PRINT_CALL = re.compile(r"\b(print|console\.log|sys\.stdout\.write|process\.stdout\.write|puts|warn)\s*\(")
_ADVICE = (
    'this can print a secret value into this command\'s own tool-call result, which becomes '
    'permanent transcript history. Use a presence-only check instead (`[ -n "$VAR" ]`), or for '
    "the tracker: `sy_preflight.py check` / the adapter's `preflight` command, which names what's "
    "missing or dead without ever printing a value."
)


def deny(reason: str) -> None:
    # reason names an env var (e.g. "ACLI_TOKEN") found in the command string; no secret value
    # is ever read from the environment or printed here.
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def decision(tool: str, args: dict) -> str | None:
    """Return a deny reason, or None to allow."""
    if tool != "Bash":
        return None
    command = str(args.get("command", ""))
    if re.search(r"\bsed\s+-[^\n;]*i\b", command) or re.search(r"\bperl\s+-[^\n;]*pi\b", command):
        return None  # in-place edits are review_guard's concern, not this hook's
    reason = _interpreter_reason(command)
    if reason:
        return f"secret guard: {reason} — {_ADVICE}"
    for segment in re.split(r"[;&|\n]+", command):
        reason = _segment_reason(segment.strip())
        if reason:
            return f"secret guard: {reason} — {_ADVICE}"
    return None


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "self-test":
        _self_test()
        print("secret_guard self-test passed")
        return
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return
    if not isinstance(event, dict):
        return
    reason = decision(event.get("tool_name", ""), event.get("tool_input") or {})
    if reason:
        deny(reason)


def _segment_reason(segment: str) -> str | None:
    tokens = [t.strip("\"'") for t in segment.split()]
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        base = tok.lstrip("\\").rsplit("/", 1)[-1]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tok) or base in WRAPPERS:
            i += 1
            continue
        break
    if i >= len(tokens):
        return None
    cmd = tokens[i].lstrip("\\").rsplit("/", 1)[-1]
    rest = tokens[i + 1:]

    if cmd in {"env", "printenv"}:
        return _env_reason(cmd, rest)
    if cmd == "set":
        return "bare `set` dumps every shell variable's value" if not rest else None
    if cmd == "export" and (not rest or rest == ["-p"]):
        return "`export` with no assignment prints every exported variable's value"
    if cmd in PRINTING_COMMANDS:
        for match in _VAR_REF.finditer(segment):
            if _looks_like_secret_name(match.group(1)):
                return f"`{cmd}` of ${{{match.group(1)}}} prints a secret-shaped variable's value"
    return None


def _env_reason(cmd: str, rest: list[str]) -> str | None:
    """`env`/`printenv` reasons. `env` also runs a command with a modified environment
    (`env FOO=bar somecmd`) — that usage is a wrapper, not a dump, and is allowed. A few `env`
    flags (`-u`/`-C`/`-S` and long forms) consume the *next* token as their own argument rather
    than naming the command to run — `env -u ACLI_SITE` alone still dumps the environment."""
    names: list[str] = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tok):
            i += 1
            continue
        if cmd == "env":
            bare = tok.split("=", 1)[0]
            if bare in _ENV_ARG_FLAGS:
                i += 1 if "=" in tok else 2
                continue
            if tok.startswith("-"):
                i += 1
                continue
            return None  # first bare word here is the command env runs — wrapper usage
        if tok.startswith("-"):
            i += 1
            continue
        names.append(tok)
        i += 1
    if cmd == "env":
        return "bare `env` dumps every environment variable's value"
    if not names:
        return "bare `printenv` dumps every environment variable's value"
    secret_names = [n for n in names if _looks_like_secret_name(n)]
    if secret_names:
        return f"`printenv {' '.join(secret_names)}` prints a secret-shaped variable's value"
    return None


def _interpreter_reason(command: str) -> str | None:
    """`python -c "...print(os.environ['TOKEN'])..."` (or node/ruby/perl `-e`) is the same leak
    as `echo $TOKEN` in a different idiom, and it survives the `;&|` segment split used elsewhere
    because the code argument is itself one shell-quoted token that legitimately contains those
    characters — so this parses the whole command with `shlex` instead, which keeps a quoted
    argument intact regardless of what punctuation it contains."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None  # unbalanced quoting: not this hook's problem to parse
    for i, tok in enumerate(tokens):
        base = tok.lstrip("\\").rsplit("/", 1)[-1]
        if base not in INTERPRETERS:
            continue
        for j in range(i + 1, len(tokens) - 1):
            if tokens[j] not in CODE_FLAGS:
                continue
            code = tokens[j + 1]
            if not _PRINT_CALL.search(code):
                continue
            match = _ENV_ACCESS.search(code)
            if not match:
                continue
            name = next(g for g in match.groups() if g)
            if _looks_like_secret_name(name):
                return f"`{base} {tokens[j]}` prints ${{{name}}} via an env-access-plus-print one-liner"
    return None


_EXTRA_WORDS: frozenset[str] | None = None


def _extra_words() -> frozenset[str]:
    """`redaction.extra_words` from resolved config, cached for this process.

    Resolved in-process (not via subprocess — this hook already pays full interpreter startup on
    every `Bash` call, so importing `sy_config` costs nothing beyond that). A misconfigured repo
    must not turn every command into a hard failure: fall back to the built-in word list alone,
    exactly as an unresolvable `debug.evals` does in `scripts/eval_events.py`.
    """
    global _EXTRA_WORDS
    if _EXTRA_WORDS is None:
        try:
            from sy_config import get as _config_get
            words = _config_get("redaction.extra_words", default=[])
        except SystemExit:
            words = []
        _EXTRA_WORDS = frozenset(str(w).upper() for w in words) if isinstance(words, list) else frozenset()
    return _EXTRA_WORDS


def _looks_like_secret_name(name: str) -> bool:
    return _base_looks_like_secret_name(name, extra=_extra_words())


def _self_test() -> None:
    allow = [
        "git status", "ls -la", "pytest -q",
        "set -euo pipefail", "set -x", "set -- a b c",
        "export FOO=bar", "export PATH=$PATH:/usr/local/bin",
        "env FOO=bar python script.py", "env -i FOO=bar somecmd",
        "env -u ACLI_SITE python script.py", "env --unset=ACLI_SITE somecmd",
        "printenv PATH", "printenv HOME SHELL",
        "echo hello", "echo $HOME", 'echo "path is $PATH"',
        '[ -n "$ACLI_TOKEN" ]', "[ -z \"$GITHUB_TOKEN\" ] && echo missing",
        'python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_preflight.py" check --tracker jira --vars ACLI_TOKEN',
        "acli jira auth status",
        "python script.py --flag value",
        '''python -c "import requests; requests.get(u, headers={'Authorization': os.environ['ACLI_TOKEN']})"''',
        '''node -e "console.log(42)"''',
    ]
    deny_cases = [
        "env", "env | grep -i acli", "env | grep -i token",
        "printenv", "printenv -0",
        "printenv ACLI_TOKEN", "printenv HOME ACLI_TOKEN",
        "set", "set;",
        "export", "export -p",
        "echo $ACLI_TOKEN", 'echo "$ACLI_TOKEN"', "echo ${ACLI_TOKEN}",
        "echo $GITHUB_TOKEN", "echo $AWS_SECRET_ACCESS_KEY",
        "printf '%s\\n' \"$ACLI_TOKEN\"",
        "cd /tmp && env",
        "echo $ACLI_TOKEN > /dev/null",
        "env -u ACLI_SITE", "env -u ACLI_SITE -u ACLI_EMAIL",
        '''python -c "import os; print(os.environ['ACLI_TOKEN'])"''',
        '''node -e "console.log(process.env.GITHUB_TOKEN)"''',
        '''node -e "console.log(process.env['GITHUB_TOKEN'])"''',
        '''python3 -c "import os, sys; sys.stdout.write(os.getenv('ACLI_TOKEN'))"''',
    ]
    for command in allow:
        got = decision("Bash", {"command": command})
        assert got is None, f"expected allow, got deny for {command!r}: {got!r}"
    for command in deny_cases:
        got = decision("Bash", {"command": command})
        assert got is not None, f"expected deny for {command!r}"
    assert decision("Write", {"command": "env"}) is None, "non-Bash tools are out of scope"
    assert _looks_like_secret_name("ACLI_TOKEN")
    assert _looks_like_secret_name("GITHUB_TOKEN")
    assert not _looks_like_secret_name("ACLI_SITE")
    assert not _looks_like_secret_name("PATH")


if __name__ == "__main__":
    main()
