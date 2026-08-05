#!/usr/bin/env python3
"""PreToolUse guard: a narrow, best-effort speed bump against printing a secret into a transcript.

Once any command prints a secret, that value is a permanent, byte-for-byte part of this session's
transcript from that point on — every later render (a HANDOFF attachment, an export) reproduces it,
regardless of whether it was ever used or uploaded anywhere. `scrub_known_secrets.py` cleans this up
after the fact, right before a rendered transcript is scanned and attached; this hook exists to make
the most likely way of getting there awkward in the first place.

Scope, stated plainly: this is best-effort defence in depth over a small named set of command shapes.
It is not a soundness guarantee, not a completeness claim, and not a shell sandbox. What it covers is:

- the two anti-patterns this repo's own docs already warn against (docs/configuration.md, the tracker
  adapters' attachment references) — dumping the environment (`env`, `printenv`, `set`, `export` with
  no assignment) and echoing a secret-shaped variable directly;
- the same leak in an interpreter idiom (`python -c "import os; print(os.environ['TOKEN'])"`,
  `node -e "console.log(process.env.TOKEN)"`), since that is a plausible next move once the plain
  `echo`/`env` form is denied, not a deliberate sandbox escape;
- the argument arity of the wrappers named in `_WRAPPER_ARG_FLAGS` (`sudo`, `nice`, `ionice`,
  `timeout` and `stdbuf` are the ones with value-taking flags), so a wrapped spelling of one of the
  shapes above still lands the walk on the command actually being run;
- `env`'s other role as a wrapper, whose wrapped command is re-checked as a segment of its own rather
  than blanket-allowed.

The set of ways a shell can print a value is unbounded, so a command shape outside that named set is
out of scope by construction rather than a defect to be closed by enumeration. The real boundary is
therefore not this hook, and two controls that do not depend on classifying a command carry it:

- to find out whether a credential is present, use the `check_env` MCP tool. It reports only whether a
  variable is set, never its value, so there is nothing left worth printing;
- any write of non-code text to an external system goes through the MCP tracker tools
  (`create-issue`, `update-issue`, `post-comment`), which scrub known secret values out of what they
  send.

Name-based, not value-based, like `scrub_known_secrets.py`'s own discovery: this hook never reads the
actual environment, only the command string, so it fires the same way whether or not a secret happens
to be set right now.

Failing to reach a decision is a deny, never a silent return. A `PreToolUse` hook that writes nothing
is read as no decision at all, which runs the command with the check skipped — so an input this hook
cannot read in the shape it expects, cannot evaluate inside a Bash call's latency budget, or crashes
it, is refused rather than allowed unchecked.

Commands:
  (no args)   read Claude Code hook JSON from stdin; deny if the Bash command matches
  self-test
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys

from sy_tools.secrets import looks_like_secret_name as _base_looks_like_secret_name

_WRAPPER_ARG_FLAGS: dict[str, frozenset[str]] = {
    "sudo": frozenset({
        "-u", "-g", "-p", "-C", "-h", "-r", "-t", "-U", "-D",
        "--user", "--group", "--prompt", "--close-from", "--host", "--role", "--type",
        "--other-user", "--chdir",
    }),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "-n", "-p", "--class", "--classdata", "--pid"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "stdbuf": frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}),
    "nohup": frozenset(),
    "time": frozenset(),
    "command": frozenset(),
}
_WRAPPER_POSITIONALS = {"timeout": 1}  # timeout's mandatory DURATION, consumed after its flags
WRAPPERS = frozenset(_WRAPPER_ARG_FLAGS)
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
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
_ADVICE = (
    "this can print a secret value into this command's own tool-call result, which becomes "
    "permanent transcript history. Use the `check_env` MCP tool instead — it reports only whether a "
    "variable is set, never its value — or, with no MCP session available, the shell equivalent "
    '`[ -n "$THE_VAR" ]`, which tests presence without printing anything. For the tracker: '
    "`sy_preflight.py check` / the adapter's `preflight` command, which names what's missing or dead "
    "without ever printing a value."
)
MAX_COMMAND_CHARS = 20_000
_TOO_LONG = (
    "secret guard: this command is longer than this hook can evaluate within a Bash call's latency "
    f"budget ({MAX_COMMAND_CHARS} characters), so it is refused rather than allowed unchecked. Split it "
    "into separate calls, or move the body into a script file and run that."
)
"""The ceiling on the command text every check below scales with, and the deny past it.

The checks here are not all linear: `_interpreter_reason` re-scans the remaining tokens once per
interpreter-shaped token, and `_env_reason` re-walks the remaining tokens once per `env` layer. This
hook is a `PreToolUse` gate on every Bash call in every session, so a slow enough input is a stall of
the whole session, and a hook that has not written a decision by the time anything gives up has
written no decision at all — the same fail-open shape as a crash. A ceiling on the input closes that
class whatever a matcher's shape turns out to be, without needing each matcher to be linear.

The number is far above any plausible single Bash call (a long `&&`-joined pipeline is hundreds of
characters, not tens of thousands) and far below where the quadratic terms bite: the worst
interpreter-shaped input the ceiling admits (2857 `python ` tokens) scans in ~0.14s here, which is
what `_test_an_oversized_command_is_refused_instead_of_scanned` bounds — timing the refusal instead
would only show that refusing is cheap, which it is by construction, since it scans nothing at all."""
_UNEVALUABLE = (
    "secret guard: this command could not be evaluated safely, so it is refused rather than allowed "
    "unchecked. Deeply nested wrapper or `env` layers are the known cause; flatten them and retry."
)
"""The deny that an unreadable input or a crash inside `decision()` becomes, since the alternative
is an allow.

A `PreToolUse` hook blocks only by exit code 2 or a `permissionDecision: "deny"` payload, so a hook
that dies mid-evaluation writes nothing and Claude Code reads that as no decision — the command runs
with the check silently skipped. `_env_reason` recurses once per `env` layer, so a long enough chain
of them raises `RecursionError` inside `decision()`; that, and whatever the next such input turns out
to be, lands here. The text names no command and no exception message, because either could carry the
very value this hook exists to keep out of the transcript."""
_UNREADABLE_COMMAND = (
    "secret guard: this Bash call's `command` is not a string, so this hook cannot read what it would "
    "run and refuses it rather than allowing it unchecked. Pass the command as a single string."
)
"""The deny for a `tool_input.command` of an unexpected shape, which `str()` would silently accept.

Its own reason rather than `_UNEVALUABLE`: nothing here is nested or deep, so naming wrapper layers as
the cause would send the caller after the wrong thing."""
_UNREADABLE_TOOL = (
    "secret guard: this call's `tool_name` is not a string, so this hook cannot tell whether it is a Bash "
    "call and refuses it rather than allowing it unchecked."
)
"""The same deny for a `tool_name` of an unexpected shape as `_UNREADABLE_COMMAND` is for the command.

`tool != "Bash"` allows, correctly, for every other tool — and so also allowed for a malformed value like
`["Bash"]`, which is the same silent-allow-on-a-shape-this-hook-cannot-read the `command` check closes.
Claude Code sets this field, so reachability is low; consistency with the sibling check is the point."""


def emit(reason: str | None, warning: str | None) -> None:
    """The hook's one JSON object on stdout: the deny decision, the degraded-config warning, or both.

    `reason` names an env var (e.g. "ACLI_TOKEN") found in the command string; no secret value is
    ever read from the environment or printed here. `warning` goes in the top-level `systemMessage`
    field, which per Claude Code's documented hook-output contract is surfaced to the user on an allow
    decision too — unlike stderr, which that contract describes as reaching only an opt-in debug log on
    a hook's exit-0 path.
    """
    payload: dict = {}
    if reason is not None:
        payload["hookSpecificOutput"] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    if warning is not None:
        payload["systemMessage"] = warning
    if payload:
        print(json.dumps(payload))


def decision(tool: object, args: dict) -> str | None:
    """Return a deny reason, or None to allow.

    Every segment of a compound command is checked, and no segment can excuse another. This hook
    once carried an exemption for a segment that ran an in-place edit (`sed -i`, `perl -pi` —
    review_guard's concern, not this hook's), which was a bypass twice over: matched against the
    whole command string, prefixing any denied command with a harmless `sed -i` allowed the whole
    thing, and matched against a segment's text, the token needed no command of its own to appear,
    so `echo "sed -i" $ACLI_TOKEN` disarmed the check for the segment that was the leak. Scoped
    finally to a segment's actually-invoked command it became unreachable — no segment can lead with
    `sed`/`perl` and with one of the printing or dumping commands `_segment_reason` denies — so it is
    gone rather than left wired up to fail open again the day that denied set grows.

    Two shapes are refused before any of that runs, for the same reason: an input this hook cannot
    read in the shape it expects is not an input it has cleared. Command text past
    `MAX_COMMAND_CHARS` is refused because every check below scales with it and not all of them
    linearly. A `command` that is not a string is refused rather than coerced — `str()` on an
    unexpected shape does not raise, so a list-shaped `{"command": ["echo $ACLI_TOKEN"]}` became the
    text `['echo $ACLI_TOKEN']`, whose bracket-and-quote punctuation matches none of the patterns
    below, and the leak was allowed. A non-string `tool` is refused for the same reason:
    `tool != "Bash"` is a correct allow for every other tool, and was therefore an allow for a
    malformed value too.
    """
    if not isinstance(tool, str):
        return _UNREADABLE_TOOL
    if tool != "Bash":
        return None
    raw = args.get("command")
    if raw is not None and not isinstance(raw, str):
        return _UNREADABLE_COMMAND
    command = raw or ""
    if len(command) > MAX_COMMAND_CHARS:
        return _TOO_LONG
    reason = _interpreter_reason(command)
    if reason:
        return f"secret guard: {reason} — {_ADVICE}"
    for segment in re.split(r"[;&|\n]+", command):
        reason = _segment_reason(segment.strip())
        if reason:
            return f"secret guard: {reason} — {_ADVICE}"
    return None


def main() -> None:
    """The hook entry point. Every way of failing to reach a decision here becomes a deny.

    Reading stdin is one of those ways and used to sit outside that rule: `json.load` also raises
    `UnicodeDecodeError` on malformed bytes, `OSError` on a broken pipe, and `RecursionError` on deeply
    nested JSON, none of which a `JSONDecodeError`-only catch covers, so each escaped as an unhandled
    crash — and a crashed `PreToolUse` hook writes nothing, which Claude Code reads as no decision, i.e.
    an allow. Even the caught case returned silently, which is the same allow by a tidier route, as did
    an event that parses into something other than a JSON object. Enumerating the read's failure modes
    kept missing one, so the catch is now `Exception`, the same fail-closed catch-all the `decision()`
    call already uses: all of them emit `_UNEVALUABLE`.
    """
    if len(sys.argv) > 1 and sys.argv[1] == "self-test":
        _self_test()
        print("secret_guard self-test passed")
        return
    try:
        event = json.load(sys.stdin)
    except Exception:  # every way of failing to read or parse the event is a deny, not a silent exit
        emit(_UNEVALUABLE, _CONFIG_WARNING)
        return
    if not isinstance(event, dict):
        emit(_UNEVALUABLE, _CONFIG_WARNING)
        return
    try:
        reason = decision(event.get("tool_name", ""), event.get("tool_input") or {})
    except Exception:  # a crash here is read as no decision, i.e. an allow, so it becomes a deny
        emit(_UNEVALUABLE, _CONFIG_WARNING)
        return
    emit(reason, _CONFIG_WARNING)


def _leading_command(segment: str) -> tuple[str, list[str]] | None:
    """The command a segment actually invokes and its remaining tokens, or None if it invokes nothing.

    The walk steps over leading `FOO=bar` assignments and over each recognised `WRAPPERS` name
    together with that wrapper's own arguments — the flags whose *next* token is their value
    (`_WRAPPER_ARG_FLAGS`, the same shape as `_ENV_ARG_FLAGS`) and any mandatory positional
    (`_WRAPPER_POSITIONALS`, `timeout`'s DURATION) — so `timeout 5 echo $VAR` reads as `echo`. It
    stays iterative so a nested wrapper unwraps one layer per pass (`sudo timeout 5 echo $VAR`), and
    it basenames what it lands on so `/bin/echo` reads as `echo`. A bare `--` ends that wrapper's
    option processing.

    The two ways this can miscount are not symmetric, and the tables err accordingly. Over-consuming
    — treating a flag as value-taking when it isn't — steps past the real command onto its first
    argument, which fails open; under-consuming leaves the walk on a flag, which is recoverable. So
    only flags confirmed to take a separated value consume two tokens, and every other flag consumes
    one: an unrecognised `-x` is skipped rather than treated as the command, because landing on the
    real command is what makes the deny reachable. `--flag=value` and an attached short value (`-o0`)
    are one token by construction.
    """
    tokens = [t.strip("\"'") for t in segment.split()]
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if _ASSIGNMENT.fullmatch(tok):
            i += 1
            continue
        base = tok.lstrip("\\").rsplit("/", 1)[-1]
        if base not in WRAPPERS:
            break
        flags = _WRAPPER_ARG_FLAGS[base]
        i += 1
        while i < len(tokens) and tokens[i].startswith("-"):
            if tokens[i] == "--":
                i += 1
                break
            i += 2 if tokens[i] in flags else 1
        i += _WRAPPER_POSITIONALS.get(base, 0)
    if i >= len(tokens):
        return None
    return tokens[i].lstrip("\\").rsplit("/", 1)[-1], tokens[i + 1:]


def _segment_reason(segment: str) -> str | None:
    leading = _leading_command(segment)
    if leading is None:
        return None
    cmd, rest = leading

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
    (`env FOO=bar somecmd`) — that usage is a wrapper, not a dump, so what it wraps is re-checked as a
    segment of its own rather than blanket-allowed: `env echo $VAR` and `env printenv VAR` deny for
    exactly the reasons their unwrapped forms do, and a genuine wrapper use stays allowed because the
    wrapped command is allowed. A few `env` flags (`-u`/`-C`/`-S` and long forms) consume the *next*
    token as their own argument rather than naming the command to run — `env -u ACLI_SITE` alone still
    dumps the environment."""
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
            return _segment_reason(" ".join(rest[i:]))  # the command env runs — check it, don't excuse it
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
_CONFIG_WARNING: str | None = None


def _extra_words() -> frozenset[str]:
    """`redaction.extra_words` from resolved config, cached for this process.

    Resolved in-process (not via subprocess — this hook already pays full interpreter startup on
    every `Bash` call, so importing `sy_tools.config` costs nothing beyond that). A misconfigured repo
    must not turn every command into a hard failure: fall back to the built-in word list alone,
    exactly as an unresolvable `debug.evals` does in `scripts/eval_events.py`.

    Every resolution failure degrades, including an `OSError` and the `ConfigError` that is the shape
    every `sy_tools.config` refusal takes. `SystemExit` alone was not enough: the
    resolver shells out to `git rev-parse`, so a `git` missing from `PATH` raised `FileNotFoundError`
    straight through this catch and crashed the hook process — and a crashed `PreToolUse` hook is
    fail-open, so the *whole* gate went quiet, not just the configured extra words. That is the
    opposite of what this fallback is for.

    The fallback reports through the hook's `systemMessage` output rather than degrading silently.
    What it drops is a security control the repo asked for, and the reasons resolution can refuse
    include a stale `CLAUDE_PROJECT_DIR` or an unusable `git` — environment faults, not missing files,
    that leave the guard quietly narrower than the repo configured it for the whole session. Non-fatal
    by design: the built-in word list still applies and the command still runs. The hook is a fresh
    process per call and holds no session state, so the warning repeats on every command that consults
    the word list until the fault is fixed; that is accepted over inventing cross-process session
    plumbing here, and the trigger is narrow (only commands naming a candidate variable at all).
    """
    global _EXTRA_WORDS, _CONFIG_WARNING
    if _EXTRA_WORDS is None:
        try:
            from sy_tools.config import ConfigError
            from sy_tools.config import get as _config_get
            words = _config_get("redaction.extra_words", default=[])
        except (SystemExit, ConfigError, OSError) as exc:
            _CONFIG_WARNING = (
                f"secret_guard: redaction.extra_words could not be resolved, so only the built-in secret "
                f"word list applies for this command: {exc}"
            )
            words = []
        _EXTRA_WORDS = frozenset(str(w).upper() for w in words) if isinstance(words, list) else frozenset()
    return _EXTRA_WORDS


def _looks_like_secret_name(name: str) -> bool:
    return _base_looks_like_secret_name(name, extra=_extra_words())


def _self_test() -> None:
    # Pinned rather than live: a consuming repo's real redaction.extra_words could overlap one of
    # the allow-cases below (e.g. a repo adding "SITE" or "HOME"), turning this self-test flaky
    # depending on wherever it happens to run. The built-in word set is what this pass/fail list
    # covers; _extra_words()'s own resolution is exercised separately, below.
    global _EXTRA_WORDS
    saved_extra_words = _EXTRA_WORDS
    _EXTRA_WORDS = frozenset()
    allow = [
        "git status", "ls -la", "pytest -q",
        "set -euo pipefail", "set -x", "set -- a b c",
        "export FOO=bar", "export PATH=$PATH:/usr/local/bin",
        "env FOO=bar python script.py", "env -i FOO=bar somecmd",
        "env -u ACLI_SITE python script.py", "env --unset=ACLI_SITE somecmd",
        "env FOO=bar echo hello", "sudo env FOO=bar python script.py",
        "printenv PATH", "printenv HOME SHELL",
        "timeout 5 ls", "timeout --signal SIGKILL 5 ls", "timeout -k 1 5 pytest -q",
        "nice -n 10 git status", "ionice -c 3 pytest -q",
        "sudo -u root ls", "sudo -- ls", "stdbuf -o0 pytest -q", "stdbuf -o 0 pytest -q",
        "sudo timeout 5 git status", "nohup python script.py",
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
        "timeout 5 echo $ACLI_TOKEN", "timeout --signal SIGKILL 5 echo $ACLI_TOKEN",
        "timeout --signal=SIGKILL 5 printenv ACLI_TOKEN", "timeout -k 1 5 env",
        "nice -n 10 echo $ACLI_TOKEN", "nice -n10 echo $ACLI_TOKEN",
        "ionice -c 3 echo $ACLI_TOKEN",
        "stdbuf -o0 echo $ACLI_TOKEN", "stdbuf -o 0 echo $ACLI_TOKEN",
        "sudo -u root echo $ACLI_TOKEN", "sudo --user=root echo $ACLI_TOKEN",
        "sudo -- echo $ACLI_TOKEN", "sudo -n printenv ACLI_TOKEN",
        "env echo $ACLI_TOKEN", "env printenv ACLI_TOKEN", "env FOO=bar echo $ACLI_TOKEN",
        "sudo env echo $ACLI_TOKEN", "sudo timeout 5 env",
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
    _EXTRA_WORDS = saved_extra_words

    _test_an_in_place_edit_excuses_no_segment_and_denies_none()
    _test_an_oversized_command_is_refused_instead_of_scanned()
    _test_an_input_that_cannot_be_read_or_evaluated_denies_rather_than_returning_silently()
    _test_a_command_that_is_not_a_string_denies_rather_than_being_coerced()
    _test_extra_words_from_config()
    _test_unresolvable_config_warns_rather_than_dropping_silently()


def _test_an_in_place_edit_excuses_no_segment_and_denies_none() -> None:
    """`sed -i`/`perl -pi` is review_guard's concern: this hook neither denies one nor lets one excuse.

    Both halves used to need an exemption, and the exemption was fail-open twice — matched against the
    whole command string, prefixing a denied command with a harmless in-place edit allowed the whole
    thing; matched against a segment's text, a quoted argument or a `#` comment carried the token and
    disarmed the segment that was the leak. It is now gone entirely, because a segment leading with
    `sed`/`perl` never leads with a command `_segment_reason` denies. Pinned with the built-in word list
    alone, for the same reason the pass/fail lists above are.
    """
    global _EXTRA_WORDS
    saved = _EXTRA_WORDS
    _EXTRA_WORDS = frozenset()
    try:
        for command in (
            "sed -i.bak s/a/b/ f && echo $ACLI_TOKEN",
            "perl -pi -e x f ; echo $ACLI_TOKEN",
            "sed -i s/x/y/ f | env",
            "sed -i s/x/y/ f && printenv GITHUB_TOKEN",
            'echo "sed -i" $ACLI_TOKEN',
            "echo $ACLI_TOKEN # sed -i",
            'printf "ran sed -i %s" "$ACLI_TOKEN"',
            "echo $ACLI_TOKEN # perl -pi",
        ):
            assert decision("Bash", {"command": command}) is not None, (
                f"an in-place edit, run or merely mentioned, must not excuse a secret-bearing segment: {command!r}"
            )
        for command in (
            "sed -i.bak s/a/b/ f",
            "perl -pi -e s/a/b/ f",
            "sed -i s/x/y/ f && ls",
            "sudo sed -i s/a/b/ f",
            "perl -i -pe s/a/b/ f",
        ):
            assert decision("Bash", {"command": command}) is None, (
                f"an in-place edit carrying no secret name is not this hook's to deny: {command!r}"
            )
    finally:
        _EXTRA_WORDS = saved


def _test_an_oversized_command_is_refused_instead_of_scanned() -> None:
    """A hook slow enough to stall a session is the same fail-open shape as one that crashes.

    `_interpreter_reason` re-scans the remaining tokens once per interpreter-shaped token, so 50000
    `python` tokens took ~38s on every Bash call that shape reached — no output for long enough that
    Claude Code has nothing to act on. The ceiling in `decision()` refuses that input outright, so the
    bound that matters is on an input the ceiling *admits*, not on the refusal, which scans nothing at
    all by construction. The timed input below is a representative dense admitted shape rather than a
    proven worst case — denser ones exist (`python -c ` or `perl -e ` repeated to the ceiling measure a
    few times slower) and stay well inside the bound. 5s is deliberately loose against the ~0.14s
    measured here: the property is a latency budget that has to hold on slower hardware, not a benchmark.
    """
    import time

    global _EXTRA_WORDS
    saved = _EXTRA_WORDS
    _EXTRA_WORDS = frozenset()
    try:
        oversized = "python " * 50_000
        started = time.perf_counter()
        reason = decision("Bash", {"command": oversized})
        elapsed = time.perf_counter() - started
        assert reason == _TOO_LONG, f"an oversized command must be refused by the ceiling: {reason!r}"
        assert elapsed < 1.0, f"the ceiling must refuse before any scan, took {elapsed:.2f}s"
        assert oversized[:20] not in reason, "the reason must not echo the command back"
        dense = "python " * (MAX_COMMAND_CHARS // 7)
        assert len(dense) <= MAX_COMMAND_CHARS, "the timed case must sit under the ceiling to be admitted"
        started = time.perf_counter()
        assert decision("Bash", {"command": dense}) != _TOO_LONG, "and must be scanned, not refused"
        elapsed = time.perf_counter() - started
        assert elapsed < 5.0, f"a dense input the ceiling admits took {elapsed:.2f}s"
        assert decision("Bash", {"command": "x" * MAX_COMMAND_CHARS}) is None, (
            "the ceiling is exclusive: a command exactly at it is still evaluated normally"
        )
        # Nothing a human writes in one Bash call comes near this, including a long compound one.
        compound = " && ".join(["git status", "pytest -q -k something_fairly_long", "ruff check ."] * 40)
        assert len(compound) < MAX_COMMAND_CHARS // 4, f"a real compound command is small: {len(compound)}"
        assert decision("Bash", {"command": compound}) is None, "and must not trip the ceiling"
        assert decision("Bash", {"command": f"{compound} && echo $ACLI_TOKEN"}) is not None, (
            "a leak in a long-but-admitted command is still scanned and still denied"
        )
    finally:
        _EXTRA_WORDS = saved


def _test_an_input_that_cannot_be_read_or_evaluated_denies_rather_than_returning_silently() -> None:
    """Every way `main()` can fail to decide must deny, including the read that happens before the try.

    `json.load(sys.stdin)` raises `UnicodeDecodeError` on malformed bytes, `OSError` on a broken pipe,
    and `RecursionError` on deeply nested JSON — none of which a `JSONDecodeError`-only catch covers, so
    each escaped as an unhandled crash, and a `PreToolUse` hook that writes nothing is read as no
    decision, i.e. an allow. Enumerating those failure modes kept missing one, which is why the read is
    now guarded by the same catch-all `Exception` as the decision below. The caught case returned
    silently, which is that same allow — and so did input that parsed into something other than a JSON
    object. A crash inside `decision()` itself is the same rule, and is reachable: `_env_reason` recurses
    once per `env` layer, so a long enough chain of them raises `RecursionError` on an input short enough
    for `MAX_COMMAND_CHARS` to admit.
    """
    import contextlib
    import io

    class _Unreadable(io.TextIOBase):
        def read(self, *_args) -> str:
            raise OSError("broken pipe")

    class _Undecodable(io.TextIOBase):
        def read(self, *_args) -> str:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    saved_stdin, saved_argv = sys.stdin, sys.argv
    for label, stream in (
        ("malformed JSON", io.StringIO("{not json")),
        ("a broken pipe", _Unreadable()),
        ("undecodable bytes", _Undecodable()),
        ("JSON nested past the recursion limit", io.StringIO("[" * 10_000 + "]" * 10_000)),
        ("a JSON list", io.StringIO("[]")),
        ("a JSON string", io.StringIO('"env"')),
        ("a JSON number", io.StringIO("5")),
        ("JSON null", io.StringIO("null")),
        ("a command that crashes the walk", io.StringIO(json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "env " * 5000}},
        ))),
    ):
        captured = io.StringIO()
        try:
            sys.argv = [saved_argv[0]]  # the hook's stdin path, not the self-test this runs inside
            sys.stdin = stream  # type: ignore[assignment]
            with contextlib.redirect_stdout(captured):
                main()
        finally:
            sys.stdin, sys.argv = saved_stdin, saved_argv
        output = captured.getvalue()
        assert output.strip(), f"{label} on stdin emitted nothing, which Claude Code reads as an allow"
        decided = json.loads(output)["hookSpecificOutput"]
        assert decided["permissionDecision"] == "deny", f"{label} must deny: {output!r}"
        assert decided["permissionDecisionReason"] == _UNEVALUABLE, output


def _test_a_command_that_is_not_a_string_denies_rather_than_being_coerced() -> None:
    """`str()` on an unexpected shape does not raise, so a crash backstop never covered this one.

    `{"command": ["echo $ACLI_TOKEN"]}` stringified to `['echo $ACLI_TOKEN']`, and the bracket-and-quote
    punctuation around the text means none of the deny patterns matched what is plainly the leak — an
    allow reached without any exception for `main()` to catch. An absent `command` is a different thing
    and still allows: a Bash call with nothing to run has nothing to leak.

    `tool_name` is the same family: `tool != "Bash"` is a correct allow for every other tool and was
    therefore also an allow for a shape this hook cannot read, like `["Bash"]`.
    """
    global _EXTRA_WORDS
    saved = _EXTRA_WORDS
    _EXTRA_WORDS = frozenset()
    try:
        for shape in (["echo $ACLI_TOKEN"], {"cmd": "env"}, 5, True, ["env"]):
            got = decision("Bash", {"command": shape})
            assert got == _UNREADABLE_COMMAND, f"a {type(shape).__name__}-shaped command must deny: {got!r}"
        assert decision("Bash", {}) is None, "an absent command has nothing to run and nothing to leak"
        assert decision("Bash", {"command": None}) is None
        assert decision("Bash", {"command": ""}) is None
        for tool in (["Bash"], {"name": "Bash"}, 5, None, True):
            got = decision(tool, {"command": "env"})
            assert got == _UNREADABLE_TOOL, f"a {type(tool).__name__}-shaped tool_name must deny: {got!r}"
        assert decision("Write", {"command": "env"}) is None, "a string naming another tool still allows"
    finally:
        _EXTRA_WORDS = saved


def _test_extra_words_from_config() -> None:
    """`redaction.extra_words` from a real (faked) config layer must actually widen the gate."""
    from pathlib import Path
    import tempfile

    from sy_tools import config as sy_config

    global _EXTRA_WORDS
    saved = _EXTRA_WORDS
    original_home, original_repo_root = Path.home, sy_config.repo_root
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        repo = Path(tmp) / "repo"
        (home / ".shipyard").mkdir(parents=True)
        (repo / ".shipyard").mkdir(parents=True)
        Path.home = staticmethod(lambda: home)  # ty: ignore[invalid-assignment]
        sy_config.repo_root = lambda: repo  # ty: ignore[invalid-assignment]
        sy_config.reset_cache()
        _EXTRA_WORDS = None
        try:
            assert not _looks_like_secret_name("NM_BEARER"), "no override must not widen the gate"

            (repo / ".shipyard" / "config.json").write_text(
                json.dumps({"redaction": {"extra_words": ["BEARER"]}}), encoding="utf-8",
            )
            sy_config.reset_cache()
            _EXTRA_WORDS = None
            assert _looks_like_secret_name("NM_BEARER"), "redaction.extra_words must widen the gate"
            assert decision("Bash", {"command": "echo $NM_BEARER"}) is not None
        finally:
            Path.home = original_home  # ty: ignore[invalid-assignment]
            sy_config.repo_root = original_repo_root
            sy_config.reset_cache()
            _EXTRA_WORDS = saved


def _test_unresolvable_config_warns_rather_than_dropping_silently() -> None:
    """A stale `CLAUDE_PROJECT_DIR` narrows this gate for the whole session and must say so.

    The fallback stays non-fatal — the built-in word list still applies and the command still runs —
    but dropping a configured security control with no signal at all is the failure this pins: the
    pointer is an environment fault nobody edits a file to cause, so nothing else would report it.
    The signal has to ride the hook's own JSON output, which is why `emit()` is asserted here too:
    an exit-0 `PreToolUse` hook's stderr reaches no human.
    """
    import contextlib
    import io
    from pathlib import Path
    import tempfile

    from sy_tools import config as sy_config

    global _EXTRA_WORDS, _CONFIG_WARNING
    saved, saved_warning = _EXTRA_WORDS, _CONFIG_WARNING
    saved_pointer = os.environ.get("CLAUDE_PROJECT_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["CLAUDE_PROJECT_DIR"] = str(Path(tmp) / "not-a-checkout")
        sy_config.reset_cache()
        _EXTRA_WORDS, _CONFIG_WARNING = None, None
        try:
            assert _extra_words() == frozenset(), "an unresolvable config must not be fatal here"
            warning = _CONFIG_WARNING or ""
            assert "redaction.extra_words" in warning, f"the drop must be reported: {warning!r}"
            assert "CLAUDE_PROJECT_DIR" in warning, f"the warning must name the cause: {warning!r}"
            assert decision("Bash", {"command": "git status"}) is None, "the guard must keep working"
            assert decision("Bash", {"command": "echo $ACLI_TOKEN"}) is not None, "built-ins still deny"
        finally:
            if saved_pointer is None:
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
            else:
                os.environ["CLAUDE_PROJECT_DIR"] = saved_pointer
            sy_config.reset_cache()
            _EXTRA_WORDS, _CONFIG_WARNING = saved, saved_warning

    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        emit("denied", "degraded")
    payload = json.loads(captured.getvalue())
    assert payload["systemMessage"] == "degraded", f"the warning must reach the user: {payload!r}"
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny", payload
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        emit(None, None)
    assert captured.getvalue() == "", "a clean allow must stay silent"


if __name__ == "__main__":
    main()
