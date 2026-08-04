#!/usr/bin/env python3
"""PreToolUse guard: deny a Bash command that would print a secret-shaped env var's value.

Once any command prints a secret, that value is a permanent, byte-for-byte part of this
session's transcript from that point on — every later render (a HANDOFF attachment, an export)
reproduces it, regardless of whether it was ever used or uploaded anywhere. `scrub_known_secrets.py`
cleans this up after the fact, right before a rendered transcript is scanned and attached; this
hook exists to stop the value from ever reaching a tool-call result in the first place.

It denies exactly the two anti-patterns this repo's own docs already warn against
(docs/configuration.md, the tracker adapters' attachment references): dumping the environment
(`env`, `printenv`, `set`, `export` with no args) and echoing a secret-shaped variable directly.
The denial message points at the safe alternatives that already exist — the `check_env` MCP tool, a
presence-only environment check that never returns a value, and for tracker credentials
`sy_preflight.py check` / the tracker's own `preflight` verb, which name what is missing or dead
without printing a value.

Name-based, not value-based, like `scrub_known_secrets.py`'s own discovery: this hook never reads
the actual environment, only the command string, so it fires the same way whether or not a secret
happens to be set right now.

Also covers the same leak in an interpreter idiom — `python -c "import os; print(os.environ['TOKEN'])"`,
`node -e "console.log(process.env.TOKEN)"` — since that's a very plausible next move once the
plain `echo`/`env` form is denied, not a deliberate sandbox escape.

This is a backstop over the documented anti-patterns, not a shell sandbox: encoded indirection
(`base64`, sourcing a script that does the printing, a nested `bash -c` two levels deep) is a
known gap, the same category `review_guard.py` documents for its own `bash -c` indirection gap.

The recognised wrapper list (`WRAPPERS`) is the other named gap, and unlike a wrapper's argument
arity — which is knowable and is accounted for below — this one cannot be closed by enumeration: the
set of things that run another command is unbounded (`pixi run`, `xargs`, `flock`, `setsid`, `doas`,
`taskset`, any repo's own task runner), so a wrapper this file does not know leaves the walk on the
wrong token and the command behind it unchecked. Widening the list buys one bypass at a time and
never finishes, so the primary mitigation is not a longer list here: it is the `check_env` MCP tool,
a presence-only environment check that returns whether a variable is set and never its value, which
leaves nothing worth wrapping in the first place.

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

from secret_words import looks_like_secret_name as _base_looks_like_secret_name

_WRAPPER_ARG_FLAGS: dict[str, frozenset[str]] = {
    "sudo": frozenset({
        "-u", "-g", "-p", "-C", "-h", "-r", "-t", "-U", "-D", "-R", "-T", "-a", "-c",
        "--user", "--group", "--prompt", "--close-from", "--host", "--role", "--type",
        "--other-user", "--chdir", "--chroot", "--command-timeout", "--auth-type", "--login-class",
    }),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "-n", "-p", "--class", "--classdata", "--pid"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "stdbuf": frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}),
    "nohup": frozenset(),
    "time": frozenset({"-o", "-f", "--output", "--format"}),
    "command": frozenset(),
}
_WRAPPER_POSITIONALS = {"timeout": 1}  # timeout's mandatory DURATION, consumed after its flags
_WRAPPER_EXACT_ONLY_FLAGS: dict[str, frozenset[str]] = {"sudo": frozenset({"--login"})}
"""Long options that take no value and must not be read as an abbreviation of one that does.

`getopt_long` resolves an exact match before it considers any abbreviation, so `sudo --login` is
sudo's own boolean `-i` and never a short spelling of the `--login-class` listed above it. The
prefix matching in `_consumes_next` cannot know that from `_WRAPPER_ARG_FLAGS` alone — every long
option there takes a value — so the few value-less options that are a proper prefix of a listed one
are named here. Without this, `sudo --login echo $VAR` consumed `echo` as a flag value and the leak
behind it went unchecked: the same landing-miss as the bug prefix matching exists to fix, in the
opposite direction."""
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


def decision(tool: str, args: dict) -> str | None:
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
    """
    if tool != "Bash":
        return None
    command = str(args.get("command", ""))
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

    Both ways of miscounting can fail open, so neither direction is a safe default. Over-consuming —
    treating a flag as value-taking when it isn't — steps past the real command onto its first
    argument. Under-consuming lands the walk on the value of a value-taking flag it failed to
    recognise, and if that value is an ordinary token rather than itself flag-shaped, the walk stops
    there and reads it as the command: `sudo -T 5 echo $VAR` read `5` as the command and allowed the
    leak until `-T` was listed. Only where the wrongly-treated token is itself flag-shaped does
    under-consuming survive, because the flag loop keeps walking.

    A complete flag table is necessary but not sufficient, because a shell does not require a flag to
    be spelled the way the table spells it: `sudo -nu root echo $VAR` clusters the boolean `-n` with
    the value-taking `-u`, and `sudo --us root echo $VAR` abbreviates `--user` the way `getopt_long`
    permits. Testing a token for exact membership in the table saw neither, consumed one token, landed
    on `root` and let the leak behind it run — with `-u` and `--user` both listed the whole time. So
    the arity decision is `_consumes_next`, which recognises clustered short options and long-option
    prefix abbreviation too; without that, this same landing-miss recurs no matter how complete the
    tables are. A wrapper's flag set is still completed from its documented synopsis rather than from
    whatever this host has installed, and an unrecognised `-x` is skipped rather than treated as the
    command, since landing on the real command is what makes the deny reachable.
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
        exact_only = _WRAPPER_EXACT_ONLY_FLAGS.get(base, frozenset())
        i += 1
        while i < len(tokens) and tokens[i].startswith("-"):
            if tokens[i] == "--":
                i += 1
                break
            i += 2 if _consumes_next(tokens[i], flags, exact_only) else 1
        i += _WRAPPER_POSITIONALS.get(base, 0)
    if i >= len(tokens):
        return None
    return tokens[i].lstrip("\\").rsplit("/", 1)[-1], tokens[i + 1:]


def _consumes_next(tok: str, flags: frozenset[str], exact_only: frozenset[str] = frozenset()) -> bool:
    """Whether one option token takes the *next* token as its value, per `getopt`/`getopt_long` rules.

    Exact membership in `flags` alone is not the question, because the same flag has more spellings
    than the table lists and each one hides the command behind it from `_leading_command`'s walk:

    - A cluster (`-nu`) is scanned left to right, because a value-taking short option ends the
      cluster: whatever follows it in the same token *is* its value. So the next token is consumed
      only when the first listed value-taking character is the cluster's last (`sudo -nu root`),
      and never when something follows it (`stdbuf -o0`, `nice -n10` — attached values, one token).
      A character the table does not list is a boolean as far as this walk is concerned and the scan
      continues past it, which is the same "skip an unrecognised flag" the caller already does.
    - A long option is a match when it equals a listed flag or, per `getopt_long`, when it is a
      prefix of one (`--us` for `--user`). An ambiguous prefix — one matching several listed flags —
      still consumes: `getopt_long` refuses such a command outright, so nothing runs either way, and
      under-consuming is the failure mode that already let real leaks through here. `--flag=value`
      carries its value in the one token and consumes nothing further, and `exact_only` names the
      value-less long options that must not be read as an abbreviation of a listed one.
    """
    if tok in exact_only:
        return False
    if tok in flags:
        return True
    if tok.startswith("--"):
        return "=" not in tok and any(f.startswith(tok) for f in flags if f.startswith("--"))
    for position, char in enumerate(tok[1:], start=1):
        if f"-{char}" in flags:
            return position == len(tok) - 1
    return False


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
    every `Bash` call, so importing `sy_config` costs nothing beyond that). A misconfigured repo
    must not turn every command into a hard failure: fall back to the built-in word list alone,
    exactly as an unresolvable `debug.evals` does in `scripts/eval_events.py`.

    Every resolution failure degrades, including an `OSError`. `SystemExit` alone was not enough: the
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
            from sy_config import get as _config_get
            words = _config_get("redaction.extra_words", default=[])
        except (SystemExit, OSError) as exc:
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
        "sudo -T 5 ls", "sudo -R /some/root ls", "time -o /tmp/x pytest -q", "time -f %e ls",
        "sudo timeout 5 git status", "nohup python script.py",
        # Clustered and abbreviated spellings of the same wrapper flags, on the allow side: the arity
        # matcher must not over-consume its way past a harmless command either.
        "sudo -nu root ls", "sudo --us root ls", "sudo --login ls",
        "timeout -fs KILL 5 ls", "time -ao /tmp/x pytest -q", "nice -n10 git status",
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
        # Separated values whose own token is not flag-shaped: an unlisted flag here left the walk
        # holding "5"/"/some/root"/"/tmp/x" as the command, which allowed the leak behind it.
        "sudo -T 5 echo $ACLI_TOKEN", "sudo --command-timeout 5 echo $ACLI_TOKEN",
        "sudo -R /some/root echo $ACLI_TOKEN", "sudo --chroot /some/root printenv ACLI_TOKEN",
        # The same flags spelled the ways a shell also accepts: a cluster ending in a value-taking
        # short option, and a `getopt_long` prefix abbreviation. Exact-membership arity saw neither,
        # landed on `root`/`KILL`/`/tmp/x` and let the command behind it run unchecked. Verified live
        # that the trailing option really does eat the next argv: `sudo -nu NOSUCHUSER_XYZ true` and
        # `sudo -n --us NOSUCHUSER_XYZ true` both fail with "sudo: unknown user NOSUCHUSER_XYZ".
        "sudo -nu root echo $ACLI_TOKEN", "sudo --us root echo $ACLI_TOKEN",
        "sudo -nu root printenv ACLI_TOKEN", "sudo -nu root env",
        "timeout -fs KILL 5 echo $ACLI_TOKEN", "time -ao /tmp/x echo $ACLI_TOKEN",
        # The opposite miss the prefix rule can introduce: sudo's own value-less `--login` is a prefix
        # of the listed `--login-class`, and consuming a value for it steps over `echo`.
        "sudo --login echo $ACLI_TOKEN", "sudo -i echo $ACLI_TOKEN",
        "/usr/bin/time -o /tmp/x echo $ACLI_TOKEN", "time -f %e echo $ACLI_TOKEN",
        "time --output /tmp/x printenv ACLI_TOKEN", "time --output=/tmp/x echo $ACLI_TOKEN",
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


def _test_extra_words_from_config() -> None:
    """`redaction.extra_words` from a real (faked) config layer must actually widen the gate."""
    from pathlib import Path
    import tempfile

    import sy_config

    global _EXTRA_WORDS
    saved = _EXTRA_WORDS
    original_home, original_repo_root = Path.home, sy_config.repo_root
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        repo = Path(tmp) / "repo"
        (home / ".shipyard").mkdir(parents=True)
        (repo / ".shipyard").mkdir(parents=True)
        Path.home = staticmethod(lambda: home)  # type: ignore[method-assign]
        sy_config.repo_root = lambda: repo
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
            Path.home = original_home  # type: ignore[method-assign]
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

    import sy_config

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
