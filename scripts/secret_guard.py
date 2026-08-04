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
It also covers the same leak in an interpreter idiom — a `python -c` / `node -e` one-liner that reads
the environment and prints what it read — since that is a very plausible next move once the plain
`echo`/`env` form is denied, not a deliberate sandbox escape. The denial message points at the safe
alternatives that already exist: the `check_env` MCP tool, and for tracker credentials
`sy_preflight.py check` / the tracker's own `preflight` verb, which name what is missing or dead
without printing a value.

Name-based, not value-based, like `scrub_known_secrets.py`'s own discovery: this hook never reads
the actual environment, only the command string, so it fires the same way whether or not a secret
happens to be set right now.

This hook is best-effort defense-in-depth over pattern-matching a command string. It is NOT a
soundness guarantee and must not be read or relied on as one: it is a shell-shaped matcher, not a
shell. Three categories of route to the same leak are out of scope BY DESIGN rather than tracked as
bugs — wrappers this file does not recognise, encoded or nested indirection, and shell grouping and
command substitution. They are stated as categories and never as spellings, on purpose: an enumerated
list of working bypasses in a docstring that agents read is a bypass index rather than a control, so
the enumeration this docstring used to carry is gone. (The self-test's deny cases are the opposite
artefact — inputs this hook refuses — and stay.)

None of those categories can be closed by enumeration, because the set of things that can run another
command, or encode one, is unbounded; widening a list buys one spelling at a time and never finishes.
So the primary mitigation for a legitimate presence check is not a longer list here: it is the
`check_env` MCP tool, a presence-only environment check that reports whether a variable is set and
never its value. That gives the legitimate need a first-class answer which leaks nothing, so there is
nothing left worth routing around this hook for. What this file does owe the shapes it recognises is
being right about them: a recognised wrapper's or flag's argument arity is knowable and is accounted
for below, and getting it wrong lands the walk on the wrong token and leaves the command behind it
unchecked — a bug here, not a scope boundary.

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
    # `-h` is deliberately absent: sudo declares it `h::` (optional_argument), so `sudo -h` is its own
    # help flag and a hostname must be attached (`-hHOST`) rather than following as the next token —
    # `sudo -h somehost echo $VAR` never consumes `somehost` as a value. Listing it stepped the walk
    # past the real command. The long `--host` is required_argument and stays.
    "sudo": frozenset({
        "-u", "-g", "-p", "-C", "-r", "-t", "-U", "-D", "-R", "-T", "-a", "-c",
        "--user", "--group", "--prompt", "--close-from", "--host", "--role", "--type",
        "--other-user", "--chdir", "--chroot", "--command-timeout", "--auth-type", "--login-class",
    }),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({
        "-c", "-n", "-p", "-P", "-u", "--class", "--classdata", "--pid", "--pgid", "--uid",
    }),
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
_ENV_ARG_FLAGS: frozenset[str] = frozenset({
    "-u", "-C", "-S", "-a", "-P", "--unset", "--chdir", "--split-string", "--argv0",
})
"""`env`'s own options that take the *next* token as their value, which the walk must step over.

The value is not the command `env` runs for any of them except `-S`/`--split-string`, whose value *is*
a command: a whole shell-word-split command string, which this walk never lands on a token of. What
that string then goes on to do is the nested-indirection category this module's own docstring puts out
of scope by design, recorded here rather than closed. What the arity table owes it is being counted —
so the walk steps over the string instead of reading it as `env`'s own command name, which is the
under-consuming failure below.

Completed from both implementations' documented synopses, because the hook cannot know which `env` is
on the host: GNU coreutils contributes `-a ARG`/`--argv0=ARG` (sets `argv[0]`, and COMMAND still runs
after it), BSD contributes `-P utilpath`, and `-u`/`-C`/`-S` are common to both. Missing any of them is
the under-consuming failure `_leading_command` documents — the walk lands on the flag's value, reads
that as the command, and the leak behind it goes unchecked. Every value-less option either side
documents (`-i`, `-0`, `-v`/`--debug`, `--list-signal-handling`, the optional-argument `--*-signal`
forms, `--help`, `--version`) is a proper prefix of none of the above, so `env` needs no
`_WRAPPER_EXACT_ONLY_FLAGS`-style exception the way `sudo --login` does."""
INTERPRETER_CODE_FLAGS: dict[str, frozenset[str]] = {
    "python": frozenset({"-c"}),
    "python3": frozenset({"-c"}),
    "node": frozenset({"-e", "--eval", "-p", "--print"}),
    "ruby": frozenset({"-e"}),
    "perl": frozenset({"-e", "-E"}),
}
"""Each recognised interpreter's own flags that take a *code* argument, per its documented synopsis.

Per interpreter rather than one shared set, because the same spelling is a different option depending
on who reads it and a shared table gets both halves wrong. node's `-p`/`--print` evaluates and prints,
the same leak as `-e`; perl's and ruby's `-p` is a boolean line-loop wrapper that takes no value, so a
shared table would read the code argument of `ruby -pe CODE` as the cluster's attached `e` and miss the
real one. perl's `-E` is `-e` with modern features enabled and takes code identically; python's own `-E`
is a boolean (ignore `PYTHON*` env vars) and ruby's takes an encoding, so it too belongs to one
interpreter only.

Widening this is bounded in a way `WRAPPERS` is not: it completes the documented flag set of the five
interpreters already named, rather than chasing the unbounded set of things that can run code."""
_PYTHON_BASENAME = re.compile(r"python\d*(\.\d+)?t?")
"""A versioned python basename (`python3.13`, `python2.7`, the free-threaded `python3.13t`).

`INTERPRETER_CODE_FLAGS` is keyed by exact name, so lookup alone missed every alias a real installation
ships and `python3.13 -c "...print(os.environ['ACLI_TOKEN'])"` — verified on this host — was allowed."""
_VAR_REF = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
_ENV_ACCESS = re.compile(
    r"os\.environ(?:\.get)?\s*[\[\(]\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"
    r"|os\.getenv\s*\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"
    r"|process\.env\.([A-Za-z_][A-Za-z0-9_]*)"
    r"|process\.env\[['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\]"
)
_PRINT_CALL = re.compile(
    r"\b(print|console\.(?:log|info|dir|dirxml|table|debug|warn|error|trace|groupCollapsed|group"
    r"|count|assert)|(?:sys|process)\.std(?:out|err)\.write|puts|warn)\s*\("
)
"""A call that writes its argument out, so a one-liner that reads a secret without printing it allows.

node's console methods are listed one by one rather than as `log` alone, because they are not aliases
of it to a matcher that works by name: `console.info`, `console.dir`, `console.table` and
`console.debug` each print the value exactly as `log` does (verified live on node 24), and with `log`
alone every one of them was allowed. `console.warn` did deny, but only by coincidence — the bare
`warn` alternative here is perl's and ruby's own function, so a `console.warn` deny rested on another
language's name and would have vanished the moment that alternative was scoped per interpreter. Each
console method now denies for its own reason.

The list is the whole printing half of node's `console` API rather than the four spellings a previous
round happened to name, because naming spellings one at a time is what left `console.error` — the
plainest of them all — allowed after a fix whose own docstring claimed the class was closed. Every
method here was measured to write the value it is handed (node v24.11.1: `error`, `trace`, `group`,
`groupCollapsed`, `count`, `assert(false, v)` and `dirxml` all reproduce a sentinel on stdout or
stderr). `console.groupEnd` is the one member of that family deliberately absent: it ignores its
argument and prints nothing at all (measured the same way), so denying it would pin a leak that does
not happen — the same false pin `_generated_code_spellings` refuses to generate for `--print=<code>`.
`console.groupCollapsed` precedes `console.group` in the alternation only for legibility; either order
matches both, since `\\s*\\(` has to follow and the engine backtracks.

stderr counts exactly as stdout does, so the writers are one `(?:sys|process)\\.std(?:out|err)\\.write`
pattern instead of the two stdout spellings that were listed: a Bash tool call's result carries both
streams into transcript history, and `sys.stderr.write(...)`/`process.stderr.write(...)` — verified live
to reproduce a sentinel — were allowed by twin patterns that differed from the denied ones by four
characters."""
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=.*")
"""A leading `NAME=VALUE` or `NAME+=VALUE` assignment prefix, which names no command to check.

One named constant, deliberately, because the duplication was itself the bug: this pattern was also
spelled out inline inside `_env_reason`'s chain-unwinding loop, so widening it to `+=` in one place
left the other copy narrow, and `env FOO+=bar echo $VAR` still left `FOO+=bar` standing as the
apparent command — a token that matches no rule below, which allowed the leak behind it. Missing an
assignment prefix disarms the whole anti-pattern path rather than narrowing it, so both sites read
this one constant now.

`=` and `+=` are the only assignment-prefix operators there are to handle: both bash and zsh run
`NAME+=VALUE cmd` (verified live in both), while `-=`, `*=` and `/=` are not assignment syntax to
either — they come back as a command not found or a failed glob — so there is no third operator to
chase. Note that `env(1)` does not read `+=` as an append: it splits on the first `=`, so
`env FOO+=bar` sets a variable literally named `FOO+` (verified). The `env` layer consumes the token
as an assignment either way, which is all this walk needs from it."""
_SHORT_CLUSTER = re.compile(r"-[A-Za-z0-9]+")
"""A token shaped like a cluster of short options and nothing else — see `_is_short_cluster`."""
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

The checks here are not all linear. `_interpreter_reason` re-scanned the remaining tokens once per
interpreter-shaped token, so `"python " * 50000` took ~38s — a single pass now, since its answer never
depended on the pair — and `_env_reason` still re-joins and re-splits the remaining tokens once per
`env` layer, which is the term the ceiling is now standing in front of. This hook is a `PreToolUse` gate
on every Bash call in every session, so either shape is a stall of the whole session, and a hook that has
not written by the time anything gives up has written no decision at all — the same fail-open shape as a
crash. A ceiling on the input closes that class whatever a matcher's shape turns out to be, which
matters in a file whose matchers have each been found incomplete a round at a time.

The number is far above any plausible single Bash call (a long `&&`-joined pipeline is hundreds of
characters, not tens of thousands) and far below where the remaining quadratic term bites. The worst
input measured among those the ceiling admits is the `env` chain, not the interpreter one: `"env " *
5000` (20000 characters, 5000 layers) scans in ~0.74s, where the same length in interpreter-shaped
tokens is now ~0.01s. So the margin is ~50x against the ~38s this replaced, measured on the worst
admitted case, and that case is what `_test_an_oversized_command_is_refused_instead_of_scanned` times:
timing the refusal instead only shows that refusing is cheap, which it is by construction, since it
scans nothing at all."""
_UNEVALUABLE = (
    "secret guard: this command could not be evaluated safely, so it is refused rather than allowed "
    "unchecked. Deeply nested wrapper or `env` layers are the known cause; flatten them and retry."
)
"""The deny a crash inside `decision()` becomes, since the alternative is an allow.

A `PreToolUse` hook blocks only by exit code 2 or a `permissionDecision: "deny"` payload, so a hook
that dies mid-evaluation writes nothing and Claude Code reads that as no decision — the command runs
with the check silently skipped. The one fail-open path actually found (unbounded recursion through
`env` chains) is closed at its source in `_env_reason`; this is the backstop for the next one, and it
names no command text and no exception message, because either could carry the very value this hook
exists to keep out of the transcript."""
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

    Command text past `MAX_COMMAND_CHARS` is refused before any of those checks run, since all of them
    scale with it and one of them scales worse than linearly.

    A `command` that is not a string is refused rather than coerced. `str()` on an unexpected shape does
    not raise, so `main()`'s `except Exception` backstop never fired for it: a list-shaped
    `{"command": ["echo $ACLI_TOKEN"]}` became the text `['echo $ACLI_TOKEN']`, whose bracket-and-quote
    punctuation matches none of the patterns below, and the leak was allowed. An input this hook cannot
    read in the shape it expects is not an input it has cleared. A `tool` of a non-string shape is refused
    for the same reason: `tool != "Bash"` is an allow for every other tool, and was therefore an allow for
    a malformed value too.
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
    `UnicodeDecodeError` on malformed bytes and `OSError` on a broken pipe, neither of which the
    `JSONDecodeError` catch covered, so both escaped as an unhandled crash *before* the backstop around
    `decision()` — and a crashed `PreToolUse` hook writes nothing, which Claude Code reads as no
    decision, i.e. an allow. Even the caught case returned silently, which is the same allow by a
    tidier route. Both now emit `_UNEVALUABLE`, for the same reason the `decision()` backstop does: an
    input this hook cannot read is not an input it has cleared.

    An event that parses but is not a JSON object is the same rule: it also used to `return` silently,
    one line below this docstring, which is that same allow by the tidier route.
    """
    if len(sys.argv) > 1 and sys.argv[1] == "self-test":
        _self_test()
        print("secret_guard self-test passed")
        return
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
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


def _consumes_next(
    tok: str, flags: frozenset[str], exact_only: frozenset[str] = frozenset(), *, prefer_last: bool = False,
) -> bool:
    """Whether one option token takes the *next* token as its value, per `getopt`/`getopt_long` rules.

    Exact membership in `flags` alone is not the question, because the same flag has more spellings
    than the table lists and each one hides the command behind it from `_leading_command`'s walk:

    - A cluster (`-nu`) is scanned left to right, because a value-taking short option ends the
      cluster: whatever follows it in the same token *is* its value. So the next token is consumed
      only when the first listed value-taking character is the cluster's last (`sudo -nu root`),
      and never when something follows it (`stdbuf -o0`, `nice -n10` — attached values, one token).
      A character the table does not list is a boolean as far as this walk is concerned and the scan
      continues past it, which is the same "skip an unrecognised flag" the caller already does.
    - `prefer_last` changes only that cluster scan, for the one kind of table where first-match-wins is
      the wrong rule: `INTERPRETER_CODE_FLAGS["node"]` lists `-e` and `-p` as *interchangeable,
      combinable* code carriers, so node parses `-pe CODE` as both flags active with CODE the next token
      — not as `-p` with the attached value `e`. Stopping at the first match answered "attached value"
      for `-pe` and `_code_argument` never looked at the real code, which allowed
      `node -pe "process.env.ACLI_TOKEN"`. Scanning the whole cluster and asking whether the *last*
      matching character is the token's last is right for both orderings, and inert for the wrapper
      tables, where at most one listed character ever appears in a real cluster. It is off by default
      because the opposite rule is the correct one for a table whose value-taking short options are not
      interchangeable (`sudo`'s), where the rest of the token is the first match's own attached value.
    - A long option is a match when it equals a listed flag or, per `getopt_long`, when it is a
      prefix of one (`--us` for `--user`). An ambiguous prefix — one matching several listed flags —
      still consumes: `getopt_long` refuses such a command outright, so nothing runs either way, and
      under-consuming is the failure mode that already let real leaks through here. `--flag=value`
      carries its value in the one token and consumes nothing further, and `exact_only` names the
      value-less long options that must not be read as an abbreviation of a listed one.
    - The bare `--` is excluded from that prefix rule by name. It is `getopt`'s universal
      end-of-options marker, not an abbreviation of anything, but every long option starts with it, so
      the prefix test matched any table with a single long flag in it and consumed the token after `--`
      as its "value" — the over-consuming miss, on the one token guaranteed to be followed by the
      command. `_leading_command`'s walk happens to test `== "--"` before asking, but `_env_reason`'s
      does not, and there `env -- echo $ACLI_TOKEN` swallowed `echo`, left `$ACLI_TOKEN` as the
      apparent command, and allowed the leak this hook's own docstring leads with. Fixed here, once,
      rather than at each caller, since a matcher that is wrong about `--` is wrong for every table.
    """
    if tok in exact_only:
        return False
    if tok in flags:
        return True
    if tok.startswith("--"):
        return tok != "--" and "=" not in tok and any(f.startswith(tok) for f in flags if f.startswith("--"))
    last_match = -1
    for position, char in enumerate(tok[1:], start=1):
        if f"-{char}" in flags:
            if not prefer_last:
                return position == len(tok) - 1
            last_match = position
    return last_match == len(tok) - 1


def _is_short_cluster(tok: str) -> bool:
    """Whether `tok` could be a cluster of short options, which is what scopes `prefer_last` to it.

    `prefer_last` is only ever the right rule for a token that really is a cluster of short options,
    and `_consumes_next` cannot tell that on its own: its cluster scan reads any token's characters, so
    the caller has to say. Applied unconditionally it regressed this file's headline interpreter deny.
    `shlex` hands `python3 -c"<code>"` over as the single token `-c<code>`, and when that code happens
    to end in a character the interpreter's table lists (`...; import gc` ends in `c`), scanning to the
    *last* match answered "the value is the next token" — so `_code_argument` looked past the code it
    was already holding, found no next token at all, and the command was allowed. Requiring every
    character after the leading dash to be alphanumeric rejects that token, since real code carries
    punctuation, while every legitimate cluster still passes.

    That is a shape discriminator, not a heuristic, and it can lose no deny. For a token it rejects,
    the cluster scan now falls through to `_code_argument`'s attached-short-option read, which is the
    correct read for exactly that shape. For a token it accepts, behaviour is unchanged, and the read
    that `prefer_last` costs there — the attached value `tok[position + 1:]` — is a substring of an
    all-alphanumeric token and so is itself all-alphanumeric, which cannot be a leak: every
    `_ENV_ACCESS` alternative needs a `.` and/or a `[`, and `_PRINT_CALL` needs a `(`. The self-test
    asserts that property of both matchers rather than leaving it argued here, so adding an all-alnum
    env-access or print idiom to either one fails loudly instead of quietly voiding this reasoning.

    Deliberately no ceiling on the cluster's length: a bound would put an arbitrary cliff where a longer
    but perfectly legitimate cluster (`python -uIsSBc CODE`) silently stopped being recognised, and the
    all-alphanumeric argument closes the case without one. A `--`-prefixed token is not a cluster — its
    second character is a dash — which is both correct and inert, since `_consumes_next` answers for
    long options before its cluster scan ever runs.
    """
    return _SHORT_CLUSTER.fullmatch(tok) is not None


def _segment_reason(segment: str) -> str | None:
    leading = _leading_command(segment)
    if leading is None:
        return None
    cmd, rest = leading
    return _command_reason(cmd, rest, segment)


def _command_reason(cmd: str, rest: list[str], segment: str) -> str | None:
    """The reason for one already-unwrapped command, split out so no call path here recurses.

    `_env_reason` needs this dispatch for whatever an `env` layer wraps, and reaching it by calling
    `_segment_reason` again made the `env` chain mutually recursive with no bound on its depth. A hook
    that raises `RecursionError` writes nothing to stdout, and per Claude Code's `PreToolUse` contract
    no output is no decision — so the guarded command ran. `_env_reason` unwinds its own chain
    iteratively and lands here exactly once instead.

    `export`'s print mode is mostly a *shape* test, not a list of flag spellings: it prints when it is
    given no operand, whatever options precede that. Matching `[]` or `["-p"]` exactly missed `export --`,
    `export -n`, `export -np` and `export -p -p`, each verified to print `declare -x NAME="VALUE"` for
    every exported variable. An operand is exactly a token that does not start with `-`, since every
    option `export` takes is boolean.

    The one spelling that shape test alone still missed is `-p` *with* an operand, because "no operand"
    is not the whole of print mode in every shell: zsh's `export -p ACLI_TOKEN` prints
    `export ACLI_TOKEN=<value>` — verified live, as are `export -p -- ACLI_TOKEN` and
    `export -p FOO ACLI_TOKEN` — while bash's own `-p` prints only when no operand follows. This hook
    cannot know which shell runs the command (Claude Code's Bash tool runs zsh on this host), and denying
    is the safe direction for either, so `-p` anywhere among the options forces print mode here with or
    without an operand. It costs nothing on the allow side: `export FOO=bar` is not all-dash and neither
    `-n` nor an assignment carries a `p`. `export -np ACLI_TOKEN` denies for the same one-rule reason even
    though bash prints nothing for it and zsh rejects `-n` outright — an unrunnable or silent command
    denied is the deny-leaning direction, not a leak missed.
    """
    if cmd in {"env", "printenv"}:
        return _env_reason(cmd, rest)
    if cmd == "set":
        return "bare `set` dumps every shell variable's value" if not rest else None
    if cmd == "export":
        prints_named = any(t.startswith("-") and t != "--" and "p" in t for t in rest)
        prints_all = all(t.startswith("-") for t in rest)  # true of `[]`, i.e. bare `export`, too
        if prints_named or prints_all:
            return "`export -p`, or `export` with no name or assignment, prints an exported variable's value"
    if cmd in PRINTING_COMMANDS:
        for match in _VAR_REF.finditer(segment):
            if _looks_like_secret_name(match.group(1)):
                return f"`{cmd}` of ${{{match.group(1)}}} prints a secret-shaped variable's value"
    return None


def _env_reason(cmd: str, rest: list[str]) -> str | None:
    """`env`/`printenv` reasons. `env` also runs a command with a modified environment
    (`env FOO=bar somecmd`) — that usage is a wrapper, not a dump, so what it wraps is re-checked
    rather than blanket-allowed: `env echo $VAR` and `env printenv VAR` deny for exactly the reasons
    their unwrapped forms do, and a genuine wrapper use stays allowed because the wrapped command is.

    `env`'s own value-taking flags (`_ENV_ARG_FLAGS`) have to be counted for the same reason a
    wrapper's do, and for the same reason as there, exact membership in that table is not the test:
    `env --uns ACLI_SITE` is the `getopt_long` prefix abbreviation of `--unset` and `env -vu ACLI_SITE`
    clusters the boolean `-v` ahead of the value-taking `-u` — both verified to run. Testing membership
    saw neither, landed the walk on `ACLI_SITE`, read it as the command `env` runs, and allowed a
    command that still dumps the whole environment. So the arity decision is `_consumes_next`, shared
    with `_leading_command`, which recognises both spellings.

    The chain unwinds in this loop rather than through `_segment_reason` again: `env env env … cmd` is
    legal, and recursing per layer put an unbounded stack depth behind an attacker-chosen token count
    in a hook whose crash is read as no decision at all.
    """
    while True:
        names: list[str] = []
        wrapped: list[str] | None = None
        i = 0
        while i < len(rest):
            tok = rest[i]
            if _ASSIGNMENT.fullmatch(tok):
                i += 1
                continue
            if cmd == "env":
                bare = tok.split("=", 1)[0]
                if _consumes_next(bare, _ENV_ARG_FLAGS):
                    i += 1 if "=" in tok else 2
                    continue
                if tok.startswith("-"):
                    i += 1
                    continue
                wrapped = rest[i:]  # the command env runs — check it, don't excuse it
                break
            if tok.startswith("-"):
                i += 1
                continue
            names.append(tok)
            i += 1
        if wrapped is None:
            break
        segment = " ".join(wrapped)
        leading = _leading_command(segment)
        if leading is None:
            return None
        cmd, rest = leading
        if cmd in {"env", "printenv"}:
            continue  # another env layer: keep unwinding here instead of recursing
        return _command_reason(cmd, rest, segment)
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
    argument intact regardless of what punctuation it contains.

    Which token is the code flag is `_consumes_next`'s decision, the same arity matcher
    `_leading_command` and `_env_reason` already use, because a shell accepts more spellings of a code
    flag than a table lists and exact membership saw none of them: `python -uc CODE` clusters the
    boolean `-u` ahead of the value-taking `-c`, `python -Ic CODE` and `-Sc`/`-Bc` do the same, and
    `node --eval=CODE` attaches its value in the one token. All were verified to run on this host and
    all were allowed. This was the last exact-membership arity site in this file.

    Where the code lives depends on the spelling, so `_code_argument` reads it rather than assuming
    `tokens[j + 1]` — that assumption also needed the loop to stop one token early, which is why an
    attached-value flag in final position could not even be reached.

    Whether the result gets printed is asked of the invocation (`_prints_result` over every token), not of
    the flag that carried the code. node auto-prints when `-p`/`--print` is anywhere in its own argv, so
    asking the carrying flag allowed `node --print --eval CODE`: `--eval` is not a printing flag and the
    code holds no `console.log`, and the check concluded nothing was printed with `--print` sitting in the
    same command.

    One pass, not the nested one this used to be: whether a token carries leaking code depends on the
    token and on the *flag set* it is read against, never on which particular earlier token named that
    interpreter, so it is enough to remember which interpreters have been seen so far — at most the five
    entries in `INTERPRETER_CODE_FLAGS` — and ask each token once per set. Re-deciding it for every
    (interpreter, later token) pair was the quadratic term measured at ~38s for 50000 `python` tokens,
    and `_code_argument`'s heavier work per pair would have made that ~38x worse still at the ceiling.
    The deny set is unchanged: a pair denies exactly when the flag's interpreter appeared before it."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None  # unbalanced quoting: not this hook's problem to parse
    # One linear pass, not one per (interpreter, token) pair: whether this invocation auto-prints is a
    # property of the token list. Accepted imprecision, in the deny-leaning direction a security hook
    # wants: the scan is the whole command's tokens, not just the interpreter's own argv, so a `-p`-shaped
    # token belonging to something else in a compound command can deny an otherwise-non-printing `-c`/`-e`
    # one-liner that names a secret variable. A false deny of a command that reads a secret without
    # printing it is the side to be wrong on.
    has_print_flag = any(_prints_result(t) for t in tokens)
    seen: dict[str, str] = {}
    for j, tok in enumerate(tokens):
        base = tok.lstrip("\\").rsplit("/", 1)[-1]
        for key, spelled_as in seen.items():
            carried = _code_argument(tokens, j, INTERPRETER_CODE_FLAGS[key])
            if carried is None:
                continue
            flag, code = carried
            if not has_print_flag and not _PRINT_CALL.search(code):
                continue
            match = _ENV_ACCESS.search(code)
            if not match:
                continue
            name = next(g for g in match.groups() if g)
            if _looks_like_secret_name(name):
                return f"`{spelled_as} {flag}` prints ${{{name}}} via an env-access-plus-print one-liner"
        key = _interpreter_key(base)
        if key is not None:
            seen.setdefault(key, base)
    return None


def _interpreter_key(base: str) -> str | None:
    """The `INTERPRETER_CODE_FLAGS` entry a command basename resolves to, or None if it resolves to none.

    A versioned python basename resolves to python's own entry, so `python3.13`/`python2.7` are the same
    interpreter to this hook as the exact names are.
    """
    if base in INTERPRETER_CODE_FLAGS:
        return base
    return "python" if _PYTHON_BASENAME.fullmatch(base) else None


def _prints_result(tok: str) -> bool:
    """Whether this token, on its own, asks an interpreter to auto-print the result of its code.

    node's `-p`/`--print` prints that result with no `console.log` in the code at all, and it arrives
    standalone, clustered with `-e` (`-pe`, verified to run), or as its own long token beside a separately
    spelled `--eval` — so whether *this invocation* auto-prints is a property of the whole token list, not
    of whichever flag happened to carry the code. Asking only the carrying flag allowed
    `node --print --eval "process.env.ACLI_TOKEN"`, where `--eval` carries the code and is not itself a
    printing flag.

    A plain character test rather than `_consumes_next`, because the question here is whether `p` appears
    in this token's own cluster, not whether the token consumes a separate value — different questions for
    a flag that is only sometimes the one carrying the code.

    The long spelling is tested on its `=`-split head rather than for equality with `--print`, because
    `--print` also takes an attached value and every such spelling reported "does not auto-print" while
    the invocation printed. The value is not worth parsing: `--print=0` and `--print=false` auto-print
    exactly as `--print=1` does (all verified live on node 24), because node reads an attached value as
    present-and-therefore-on. `--eval` alone prints nothing, which is the control that keeps this test
    from collapsing into "any long option with a value".
    """
    if not tok.startswith("-"):
        return False
    if tok.startswith("--"):
        return tok.split("=", 1)[0] == "--print"
    return "p" in tok[1:]


def _code_argument(tokens: list[str], j: int, flags: frozenset[str]) -> tuple[str, str] | None:
    """One option token's flag and the code it carries, or None when it carries no code.

    Three shapes, and `_consumes_next` alone answers only the first: separated (`-c CODE`, `-uc CODE`,
    `--eval CODE`), attached to a long option (`--eval=CODE`, one token by construction — the same case
    `_env_reason` splits on `=` before asking), and attached to a short option (`-cCODE`, `-ucCODE`,
    which `_consumes_next` reports as consuming nothing because the value is in the token already —
    true for its callers, where only the arity matters, and not enough here, where the value is the
    thing being scanned). Bounds-checked rather than assumed: a separated flag can be the last token,
    which carries no code at all.

    The flag is returned apart from the code because the caller names it in a deny reason, and for the
    two attached shapes the flag and the code share a token: reporting that whole token would put
    arbitrary code into the deny reason, which is transcript history and is exactly what this hook
    exists to keep a value out of.

    Only an option-shaped token is asked about, which is `_consumes_next`'s own unstated precondition —
    its cluster scan reads any token's characters, so `foo.c` "clusters" a `-c` at its end and answered
    yes for a filename.

    The arity question goes to `_consumes_next` with `prefer_last` on, but only for a token that is
    actually cluster-shaped (`_is_short_cluster`), which is the whole of that rule's scope. It is needed
    because a code-flag table lists interchangeable carriers: node runs `-pe CODE` as `-p` and `-e` both
    active with CODE the next token, and first-match-wins read that as `-p` with the attached value `e`
    and never examined the real code. It must be scoped because an attached spelling arrives as one token
    whose tail is the code, and a code string ending in a listed flag character made an unscoped scan
    report "the value is the next token" — past the code, onto a token that need not even exist. Both
    halves are pinned in the self-test, since the two spellings of that same miss were found one at a
    time, in opposite directions. The attached-short-option fallback below stays first-match-wins, which
    is the correct rule for the shape it is for (`-cCODE`, where the rest of the token really is the
    value).
    """
    tok = tokens[j]
    if not tok.startswith("-"):
        return None
    head, separator, attached = tok.partition("=")
    if separator and _consumes_next(head, flags, prefer_last=_is_short_cluster(head)):
        return head, attached
    if _consumes_next(tok, flags, prefer_last=_is_short_cluster(tok)):
        return (tok, tokens[j + 1]) if j + 1 < len(tokens) else None
    if not tok.startswith("--"):
        for position, char in enumerate(tok[1:], start=1):
            if f"-{char}" in flags:
                return (tok[:position + 1], tok[position + 1:]) if position + 1 < len(tok) else None
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


_GENERATED_LEAKS = ("echo $ACLI_TOKEN", "printenv ACLI_TOKEN", "env")
"""The leaks a generated wrapper/`env` spelling is put in front of: one per anti-pattern this hook denies.

Three rather than one, because a spelling that lands the walk on the wrong token hides whatever is behind
it, and which of these three commands is behind it decides *which* matcher would have fired. `env` is here
as the wrapped command (`env -u VAL env`), not as a bare trailing option list, so every generated command
is one a real `env` actually runs."""
_GENERATED_HARMLESS = ("ls", "git status")
"""The allow side of the same spellings. A generator that only asserted denies would pass against a hook
that denied everything, which is a failure mode this file has come close to more than once (the `--`
prefix bug denied `env -- ls`), so every spelling is generated twice."""
_GENERATED_VALUE = "VAL"
"""The flag value a generated spelling carries: an ordinary token, deliberately not option-shaped.

That is the shape that makes an arity miss reachable — `_leading_command`'s own docstring: under-consuming
survives only where the wrongly-treated token is itself flag-shaped, because then the flag loop keeps
walking. `VAL` also carries no character that any table below lists as a short option, so the cluster scan
cannot accidentally read the value as more flags."""
_GENERATED_BOOLEAN_POOL = "vnfibqwz"
"""Preference order for the leading character of a generated short-option cluster (`-vu VAL`).

The character only has to be one the table under test does not list, because that is exactly the class of
character `_consumes_next`'s cluster scan must walk past ("a character the table does not list is a boolean
as far as this walk is concerned"). It is a preference order rather than a per-tool list of real booleans so
that no hand-curated table is introduced here; the order simply puts letters first that really are boolean
options of several of these tools (`sudo -v`, `env -v`, `python -v`, `timeout -v`, `time -v`), so most
generated clusters are literally runnable. Where a particular build rejects one, an unrunnable command
denied is the deny-leaning direction this module already accepts elsewhere (`export -np`)."""
_GENERATED_CODE_BASES: dict[str, dict[str, str]] = {
    "python": {
        "leak_print": "import os; print(os.environ['ACLI_TOKEN'])",
        "leak_silent": "import os; os.environ['ACLI_TOKEN']",
        "harmless": "print(1)",
    },
    "node": {
        "leak_print": "console.log(process.env.ACLI_TOKEN)",
        "leak_silent": "process.env.ACLI_TOKEN",
        "harmless": "console.log(1)",
    },
}
"""The three code payloads a generated interpreter spelling carries, per `_ENV_ACCESS`-recognised language.

`leak_silent` reads the environment without printing it, which is the control that keeps the generated deny
side from collapsing into "any one-liner naming a secret": node's `-e`/`--eval` alone evaluates without
printing (verified), so it legitimately allows, and denies only once the invocation asks for auto-print.

python and node only, because `_ENV_ACCESS` recognises python's `os.environ`/`os.getenv` and node's
`process.env` and nothing else. perl's `$ENV{X}` and ruby's `ENV['X']` are a recorded, still-open gap in
that matcher — see the `_code_argument` assertions in `_self_test`, which pin perl's and ruby's *arity* for
when it is closed — so generating a DENY for them would fail at every commit including this one. Excluded
deliberately, not forgotten."""
_GENERATED_CODE_SUFFIXES: dict[str, tuple[str, ...]] = {
    "python": ("  # {ch}", "; import g{ch}"),
    "node": (" // {ch}", "; typeof {ch}"),
}
"""Trailing payload variants whose last character is a character the interpreter's own flag table lists.

This is the half of generation that a flag-spelling matrix alone does not reach, and the reason it is here
is a measured regression: `python3 -c"<code>"` arrives from `shlex.split` as the single token `-c<code>`, so
when `<code>` ends in a listed flag character (`...; import gc` ends in `c`) an unscoped `prefer_last` cluster
scan answered "the value is the next token", `_code_argument` looked past the code it was already holding,
found no next token, and the plainest interpreter leak there is was ALLOWED. The ordinary attached spelling
denied correctly throughout, so a generator that varied only the spelling would have shipped that
regression exactly as the hand-curated deny list did. One comment form and one statement form per listed
character, so the collision is reached both with and without the code being syntactically complete."""


def _generated_spelling_cases() -> list[tuple[str, bool]]:
    """`(command, expect_deny)` pairs generated from this module's own tables, in place of a curated list.

    Every option-token classifier here has been found incomplete one spelling at a time, over seven review
    rounds, and round 6's own fix shipped a regression its hand-curated self-test did not catch — because
    the author who missed a spelling in the matcher is the same author who then wrote the case list. Hand
    curation cannot cover its author's blind spot. Generating the spellings from the tables the matchers
    read can: adding a flag to `_WRAPPER_ARG_FLAGS`, `_ENV_ARG_FLAGS` or `INTERPRETER_CODE_FLAGS`
    immediately produces every spelling a shell accepts for it, on both the allow and the deny side, with
    no list to remember to update.

    Three families, each generated against the table that drives it:

    - `WRAPPERS`/`_WRAPPER_ARG_FLAGS`: separated, attached-short, cluster-ending-in-the-value-taking-flag,
      long separated, long `=`-attached, and a `getopt_long` prefix abbreviation, times a leaking command
      behind it and a harmless one. `_WRAPPER_POSITIONALS` is honoured (`timeout`'s DURATION) and
      `_WRAPPER_EXACT_ONLY_FLAGS` is generated as its own *rule*: a value-less long option leaves the next
      token as the command, so `sudo --login VAL echo $ACLI_TOKEN` legitimately ALLOWS (the leak is behind
      `VAL`, which runs nothing) while `sudo --login echo $ACLI_TOKEN` denies.
    - `_ENV_ARG_FLAGS`: the same matrix, minus `-S`/`--split-string`, whose value is itself a command
      string — the nested-indirection gap this module documents as out of scope by design.
    - `INTERPRETER_CODE_FLAGS`: the same matrix for python and node, crossed with the payload variants in
      `_GENERATED_CODE_SUFFIXES`, which is what makes this generator a regression detector rather than a
      restatement of the deny list — see `_test_generated_option_spellings_deny_whenever_the_tool_prints`
      for the two historical commits this list is shown to fail against.

    1149 cases at the time of writing (667 deny, 482 allow); the self-test asserts a floor rather than an
    exact number, so widening a table is a pass and emptying the generator is a loud failure. The floors are
    asserted per family, not only in aggregate — see `_generated_case_families`.

    Module-level and importable on purpose, so a probe can import the case list from one version of this
    file and drive another version's hook as a subprocess, which is how that cross-commit check runs."""
    return [case for cases in _generated_case_families().values() for case in cases]


def _generated_case_families() -> dict[str, list[tuple[str, bool]]]:
    """The generated cases keyed by which table produced them, so each family can be counted on its own.

    An aggregate floor cannot see a missing family. `_generated_interpreter_cases` skips a language whose
    idioms `_ENV_ACCESS` does not know by `continue`-ing past it, and that same skip has already swallowed a
    whole language's matrix once (python3's, silently — see the comment there); the interpreter family is
    also the only one carrying the payload-suffix collision this generator exists to detect, and it is the
    smallest of the three. So with `len(cases) > 900` and friends asserted over the total, the wrapper and
    `env` families alone satisfied every floor and the freeze precondition was satisfiable with zero
    interpreter coverage. Split here rather than counted in the test, so there is one source of truth for
    what the families are and `_generated_spelling_cases` cannot drift from what gets floored."""
    return {
        "wrapper": _generated_wrapper_cases(),
        "env": _generated_env_cases(),
        "interpreter": _generated_interpreter_cases(),
    }


def _generated_cluster_prefix(flags: frozenset[str]) -> str | None:
    """A character to lead a generated short-option cluster with: the first pool letter this table omits."""
    return next((ch for ch in _GENERATED_BOOLEAN_POOL if f"-{ch}" not in flags), None)


def _generated_abbreviation(flag: str, longs: frozenset[str], exact_only: frozenset[str]) -> str | None:
    """The shortest `getopt_long` prefix abbreviation of `flag` unambiguous within this module's own tables.

    Unambiguous against `longs` and never a prefix of an `exact_only` flag, because both of those are what a
    real `getopt_long` refuses outright: `sudo --clas` is ambiguous and `sudo --log` matches the value-less
    `--login` too, so neither command runs and neither is a spelling to assert anything about. This is the
    same rule `_WRAPPER_EXACT_ONLY_FLAGS` exists for, applied from the generator's side.

    `longs` is the value-taking subset this module models plus `exact_only`, not the tool's real full option
    set, which the hook has no listing of — so an abbreviation generated here can still be ambiguous to the
    real tool (`sudo --a`/`--h`/`--p`/`--r` each match several of sudo's own options). That direction is
    harmless: such a command is one a real `getopt_long` refuses, so nothing runs, and asserting a deny for
    a command that cannot run is the deny-leaning imprecision `_GENERATED_BOOLEAN_POOL` already documents.
    The direction that would matter — abbreviating so *little* that a real tool reads the value as something
    else — is what the `exact_only` check closes."""
    for size in range(3, len(flag)):
        prefix = flag[:size]
        if any(f.startswith(prefix) for f in exact_only):
            continue
        if sum(1 for f in longs if f.startswith(prefix)) == 1:
            return prefix
    return None


def _generated_flag_spellings(flag: str, flags: frozenset[str], exact_only: frozenset[str]) -> list[str]:
    """Every spelling a shell accepts for one value-taking `flag` plus `_GENERATED_VALUE`.

    Short: separated, attached, and clustered behind a boolean — the three the walk must count as one
    consumed value, one consumed token, and one consumed value respectively. Long: separated, `=`-attached
    (which consumes nothing further), and a prefix abbreviation."""
    if flag.startswith("--"):
        longs = frozenset(f for f in flags if f.startswith("--"))
        abbreviated = _generated_abbreviation(flag, longs, exact_only)
        spellings = [f"{flag} {_GENERATED_VALUE}", f"{flag}={_GENERATED_VALUE}"]
        return spellings + ([f"{abbreviated} {_GENERATED_VALUE}"] if abbreviated else [])
    spellings = [f"{flag} {_GENERATED_VALUE}", f"{flag}{_GENERATED_VALUE}"]
    boolean = _generated_cluster_prefix(flags)
    return spellings + ([f"-{boolean}{flag[1]} {_GENERATED_VALUE}"] if boolean else [])


def _generated_option_prefixes(
    carried: frozenset[str], table: frozenset[str], exact_only: frozenset[str] = frozenset(),
) -> list[str]:
    """Every generated option run for one table, including the empty one: the bare command takes cases too.

    `carried` is the flags to spell out and `table` the whole option set they are spelled *against*, which
    are the same thing for a wrapper and not for `env`, where `-S`/`--split-string` is excluded from the
    former but still decides what a cluster or an abbreviation is ambiguous with in the latter."""
    prefixes = [""]
    for flag in sorted(carried):
        prefixes.extend(_generated_flag_spellings(flag, table, exact_only))
    return prefixes


def _generated_wrapper_cases() -> list[tuple[str, bool]]:
    """`WRAPPERS` × its own flag spellings × a leaking and a harmless command behind them."""
    cases: list[tuple[str, bool]] = []
    for wrapper in sorted(WRAPPERS):
        flags = _WRAPPER_ARG_FLAGS[wrapper]
        exact_only = _WRAPPER_EXACT_ONLY_FLAGS.get(wrapper, frozenset())
        positional = " ".join(["5"] * _WRAPPER_POSITIONALS.get(wrapper, 0))  # timeout's DURATION
        for prefix in _generated_option_prefixes(flags, flags, exact_only):
            head = " ".join(part for part in (wrapper, prefix, positional) if part)
            cases += [(f"{head} {leak}", True) for leak in _GENERATED_LEAKS]
            cases += [(f"{head} {ok}", False) for ok in _GENERATED_HARMLESS]
        # A value-less long option consumes no value, so the token after it *is* the command: the leak
        # behind `sudo --login VAL echo $ACLI_TOKEN` never runs, and asserting a deny for it would pin the
        # very over-consuming miss `_WRAPPER_EXACT_ONLY_FLAGS` exists to prevent. Without the value it is
        # an ordinary deny, so both directions are generated from the one table.
        for flag in sorted(exact_only):
            head = " ".join(part for part in (wrapper, flag, positional) if part)
            cases += [(f"{head} {leak}", True) for leak in _GENERATED_LEAKS]
            cases += [(f"{head} {_GENERATED_VALUE} {leak}", False) for leak in _GENERATED_LEAKS]
    return cases


def _generated_env_cases() -> list[tuple[str, bool]]:
    """`_ENV_ARG_FLAGS` × the same spelling matrix × the same commands behind it.

    `-S`/`--split-string` is excluded: its value is itself a whole command string, which is the nested
    indirection this module's own docstring puts out of scope by design and `_ENV_ARG_FLAGS` records rather
    than closes. Its arity is still counted by the walk, and the walk never lands on a token of it, so there
    is no spelling here to assert a deny for. Ambiguity and clustering are still computed against the full
    table, since that is what a real `env` parses against."""
    carried = frozenset(f for f in _ENV_ARG_FLAGS if f not in {"-S", "--split-string"})
    cases: list[tuple[str, bool]] = []
    for prefix in _generated_option_prefixes(carried, _ENV_ARG_FLAGS):
        head = " ".join(part for part in ("env", prefix) if part)
        cases += [(f"{head} {leak}", True) for leak in _GENERATED_LEAKS]
        cases += [(f"{head} {ok}", False) for ok in _GENERATED_HARMLESS]
    return cases


def _generated_code_spellings(name: str, flags: frozenset[str]) -> list[tuple[str, bool, bool]]:
    """Every spelling that hands `{code}` to one interpreter, with whether it auto-prints and where the code
    lands.

    The second element is whether the invocation asks the interpreter to print its result, which for node is
    the difference between a deny and a legitimate allow. It is read off the flag tokens with
    `_prints_result` — the only matcher this generator consults, and one the curated table in `_self_test`
    pins spelling by spelling — never off the arity walk, which is the part every round of review has found
    broken. It also decides which long options carry code at all: an auto-print flag's attached value is a
    boolean to node rather than a payload, so `--print="<code>"` is not generated as a spelling of anything
    (see below), while `--print` separated from its code is.

    The third element is whether the code shares a token with the flag (`-e<code>`). That shape is where an
    attached payload's own characters are read as more flags, which is deliberate and deny-leaning in
    `_interpreter_reason` ("a `-p`-shaped token belonging to something else … can deny an otherwise
    non-printing one-liner"), so the caller keeps its non-printing payloads out of it rather than asserting
    an allow the module intentionally does not give."""
    shorts = sorted(f for f in flags if not f.startswith("--"))
    longs = sorted(f for f in flags if f.startswith("--"))
    spellings: list[tuple[str, bool, bool]] = []
    boolean = _generated_cluster_prefix(flags)
    for flag in shorts:
        spellings.append((f'{name} {flag} "{{code}}"', _prints_result(flag), False))
        spellings.append((f'{name} {flag}"{{code}}"', _prints_result(flag), True))
        if boolean:
            cluster = f"-{boolean}{flag[1]}"
            spellings.append((f'{name} {cluster} "{{code}}"', _prints_result(cluster), False))
    for flag in longs:
        spellings.append((f'{name} {flag} "{{code}}"', _prints_result(flag), False))
        # Only for a long option that really carries a value. An auto-print flag's *attached* value is not
        # code to node: it reads any value there as present-and-therefore-on (which is why
        # `_prints_result` does not parse it), so `node --print="<code>"` prints the literal `undefined`
        # and never evaluates the payload at all — measured on node v24.11.1. Generating a deny for it
        # pinned a leak that does not happen, and inflated this generator's own detection counts with a
        # case that carries nothing. The separated `--print "<code>"` spelling above is the real one, and
        # `--print=<value> --eval "<code>"` below is how an attached value legitimately reaches a deny.
        if not _prints_result(flag):
            spellings.append((f'{name} {flag}="{{code}}"', False, False))
    # A table that lists several *interchangeable* code carriers accepts a cluster of them, and the print
    # flag leads it: node runs `-pe CODE` as both flags with CODE the next token and rejects the reverse
    # `-ep` outright, so the ordering is generated from which carrier prints rather than from both orders.
    # The same two flags spelled apart, and with `--print`'s own attached value, are the spellings that
    # made auto-print a property of the invocation instead of the carrying flag.
    for printer in (f for f in shorts if _prints_result(f)):
        for carrier in (f for f in shorts if not _prints_result(f)):
            spellings.append((f'{name} -{printer[1]}{carrier[1]} "{{code}}"', True, False))
    for printer in (f for f in longs if _prints_result(f)):
        for carrier in (f for f in longs if not _prints_result(f)):
            spellings.append((f'{name} {printer} {carrier} "{{code}}"', True, False))
            # node reads any attached value as present-and-therefore-on, so the value is not parsed and a
            # falsy one prints exactly as `1` does (verified live).
            for value in ("1", "false"):
                spellings.append((f'{name} {printer}={value} {carrier} "{{code}}"', True, False))
    return spellings


def _generated_interpreter_cases() -> list[tuple[str, bool]]:
    """`INTERPRETER_CODE_FLAGS` × every code spelling × every payload variant, for python and node.

    The expectation is a rule, not a list: a case denies exactly when the payload reads a secret-shaped
    variable *and* something prints it — a print call in the code, or an auto-print flag anywhere in the
    invocation. That is what each real interpreter does, and it is why `node -e "process.env.ACLI_TOKEN"`
    is generated as an ALLOW while `node -p` on the same payload is a DENY."""
    cases: list[tuple[str, bool]] = []
    for name, flags in sorted(INTERPRETER_CODE_FLAGS.items()):
        # Which *language* an entry's payloads come from, resolved through `_PYTHON_BASENAME` rather than
        # `_interpreter_key`: that returns the table's own key, so `python3` — a separate entry, and the
        # spelling this module's headline regression was measured in — resolved to `"python3"` and its whole
        # matrix was skipped without a word. Found by dumping the generated list, not by reading it.
        key = "python" if _PYTHON_BASENAME.fullmatch(name) else name
        if key not in _GENERATED_CODE_BASES:
            continue  # perl/ruby: `_ENV_ACCESS` knows neither idiom — see `_GENERATED_CODE_BASES`
        characters = sorted(flag[1] for flag in flags if len(flag) == 2)
        payloads: list[tuple[str, str]] = []
        for kind, base in sorted(_GENERATED_CODE_BASES[key].items()):
            payloads.append((kind, base))
            payloads += [
                (kind, base + suffix.format(ch=ch))
                for suffix in _GENERATED_CODE_SUFFIXES[key]
                for ch in characters
            ]
        for template, auto_prints, code_shares_the_flag_token in _generated_code_spellings(name, flags):
            for kind, code in payloads:
                if kind == "leak_silent" and code_shares_the_flag_token:
                    continue  # deliberately deny-leaning there — see `_generated_code_spellings`
                prints = kind == "leak_print" or auto_prints
                cases.append((template.format(code=code), kind.startswith("leak") and prints))
    return cases


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
        # An operand — an assignment or a bare name — is what makes `export` not print mode. Both
        # spellings print nothing at all, verified in bash and zsh.
        "export FOO=bar", "export PATH=$PATH:/usr/local/bin", "export FOO", "export -n FOO",
        "env FOO=bar python script.py", "env -i FOO=bar somecmd",
        "env -u ACLI_SITE python script.py", "env --unset=ACLI_SITE somecmd",
        "env FOO=bar echo hello", "sudo env FOO=bar python script.py",
        "printenv PATH", "printenv HOME SHELL",
        "timeout 5 ls", "timeout --signal SIGKILL 5 ls", "timeout -k 1 5 pytest -q",
        "nice -n 10 git status", "ionice -c 3 pytest -q",
        # ionice's other two value-taking selectors, per its synopsis: -p PID / -P PGRP / -u UID.
        "ionice -u 1000 ls", "ionice -P 500 ls", "ionice --uid 1000 ls",
        # env's own value-taking flags in both implementations' spellings, wrapping a harmless command.
        "env -a login /bin/sh -c true", "env --argv0=login /bin/sh -c true",
        "env -P /usr/bin ls", "env -vu ACLI_SITE ls", "env --uns ACLI_SITE ls",
        # `--` ends env's options and names no value, so what follows it is the command env runs, not a
        # flag value: reading it as one dumped the remaining tokens and denied this harmless wrapper.
        "env -- ls", "env -u ACLI_SITE -- ls",
        "sudo -u root ls", "sudo -- ls", "stdbuf -o0 pytest -q", "stdbuf -o 0 pytest -q",
        "sudo -h ls", "sudo --host somehost ls",
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
        # The clustered, attached and versioned interpreter spellings on the allow side: reading the code
        # argument out of a spelling the table did not list must not deny a one-liner that leaks nothing.
        '''python -uc "print(1)"''', '''python3 -Ic "print(1)"''', '''python3.13 -c "print(1)"''',
        '''python -c"print(1)"''', '''node --eval="console.log(42)"''', '''node -p "1 + 1"''',
        # `node -e`/`--eval` alone evaluates without printing — verified: `node -e "process.env.FOO"`
        # writes nothing — so a one-liner that reads a secret and neither prints it nor asks for
        # auto-print leaks nothing. The invocation-wide print test must not widen the gate to this.
        '''node --eval="process.env.ACLI_TOKEN"''', '''node -e "process.env.ACLI_TOKEN"''',
        # `console.groupEnd` takes no argument: it closes an indentation group and prints nothing at all,
        # whatever it is handed (measured on node v24.11.1). It is the one member of the console family
        # that must stay out of `_PRINT_CALL`, and this case is what keeps it out on the next widening.
        '''node -e "console.groupEnd(process.env.ACLI_TOKEN)"''',
        # `-p` is node's print-and-evaluate flag, but perl's and ruby's own `-p` is a boolean line loop
        # whose `-e` still carries the code: a shared code-flag table read the cluster's `e` as the code
        # and skipped the real argument, so the flags are per interpreter.
        "perl -pi -e s/a/b/ f", "ruby -pe 'puts $_' f", '''perl -E "say 1"''',
        # `NAME+=VALUE` is a real assignment prefix in both shells and is now stepped over as one, so the
        # widened `_ASSIGNMENT` must not deny the harmless commands behind it either.
        "FOO+=bar ls", "FOO+=bar git status", "env FOO+=bar ls", "sudo FOO+=bar ls",
    ]
    deny_cases = [
        "env", "env | grep -i acli", "env | grep -i token",
        "printenv", "printenv -0",
        "printenv ACLI_TOKEN", "printenv HOME ACLI_TOKEN",
        "set", "set;",
        # `export` prints when it is given no operand, whatever options come first. Exact list equality
        # against `[]`/`["-p"]` allowed every one of these, each verified to print `declare -x
        # NAME="VALUE"` for every exported variable in bash (`-n` is not a zsh option, the rest are).
        "export", "export -p", "export --", "export -n", "export -np", "export -p -p",
        # And `-p` prints the *named* variable in zsh — which is the shell Claude Code's Bash tool runs
        # here — where bash's `-p` prints only with no operand: `export -p ACLI_TOKEN` writes
        # `export ACLI_TOKEN=<value>`, verified live, as do the `--` and multi-operand forms. So `-p`
        # anywhere in the options is print mode, operand or not, since deny is safe for either shell.
        # `-np` is in that one rule too, though bash prints nothing for it and zsh rejects `-n` outright.
        "export -p ACLI_TOKEN", "export -p -- ACLI_TOKEN", "export -p FOO ACLI_TOKEN",
        "export -np ACLI_TOKEN",
        "echo $ACLI_TOKEN", 'echo "$ACLI_TOKEN"', "echo ${ACLI_TOKEN}",
        "echo $GITHUB_TOKEN", "echo $AWS_SECRET_ACCESS_KEY",
        "printf '%s\\n' \"$ACLI_TOKEN\"",
        "cd /tmp && env",
        "echo $ACLI_TOKEN > /dev/null",
        "env -u ACLI_SITE", "env -u ACLI_SITE -u ACLI_EMAIL",
        # env's own flag arity, missed the same two ways `_leading_command`'s was before `_consumes_next`:
        # a value-taking flag absent from the table (`-a`/`--argv0`, GNU; `-P`, BSD), and a spelling the
        # table does not carry — `--uns`/`--u` abbreviate `--unset` per `getopt_long`, and `-vu` clusters
        # the boolean `-v` ahead of `-u`. Each landed the walk on the flag's value, read that as the
        # command env runs, and allowed a command that still dumps the environment behind it. Verified
        # live that the trailing option really does eat the next argv: `env -vu FOO true` unsets FOO and
        # runs `true`, and `env -P /usr/bin true` resolves `true` under /usr/bin.
        "env -a x echo $ACLI_TOKEN", "env --argv0 x printenv ACLI_TOKEN",
        "env --argv0=x echo $ACLI_TOKEN", "env -P /usr/bin echo $ACLI_TOKEN",
        "env --uns ACLI_SITE", "env --u ACLI_SITE", "env -vu ACLI_SITE",
        "env -vu ACLI_SITE echo $ACLI_TOKEN", "env --uns=ACLI_SITE printenv ACLI_TOKEN",
        # sudo's `-h` is `optional_argument` (`sudo -h` alone prints usage), so it never eats the next
        # token; listing it as value-taking stepped the walk over `echo` onto `$ACLI_TOKEN`.
        "sudo -h echo $ACLI_TOKEN",
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
        # The bare `--` is getopt's end-of-options marker, but every long option starts with it, so the
        # prefix rule in `_consumes_next` matched it against any table and consumed the token after it —
        # here `echo`, leaving `$ACLI_TOKEN` as the apparent command, which matches nothing. That
        # allowed this file's own headline anti-pattern. `_leading_command`'s walk tests `== "--"` first
        # and so never reached the bug; `_env_reason`'s does not, and did.
        "env -- echo $ACLI_TOKEN", "env -- printenv ACLI_TOKEN", "env -- env",
        "env -u ACLI_SITE -- echo $ACLI_TOKEN", "env FOO=bar -- printenv GITHUB_TOKEN",
        "env echo $ACLI_TOKEN", "env printenv ACLI_TOKEN", "env FOO=bar echo $ACLI_TOKEN",
        "sudo env echo $ACLI_TOKEN", "sudo timeout 5 env",
        '''python -c "import os; print(os.environ['ACLI_TOKEN'])"''',
        '''node -e "console.log(process.env.GITHUB_TOKEN)"''',
        '''node -e "console.log(process.env['GITHUB_TOKEN'])"''',
        '''python3 -c "import os, sys; sys.stdout.write(os.getenv('ACLI_TOKEN'))"''',
        # The code flag's other spellings, all verified to execute on this host (Python 3.13, node 24,
        # perl 5.34, ruby 2.6): a cluster ending in `-c`, a long option carrying its value after `=`, and
        # a short option carrying it attached. Exact membership in the flag table recognised none of them,
        # and the loop that read `tokens[j + 1]` stopped one token early so a trailing attached-value flag
        # was never even reached.
        '''python -uc "import os; print(os.environ['ACLI_TOKEN'])"''',
        '''python3 -Ic "import os; print(os.environ['ACLI_TOKEN'])"''',
        '''python -Sc "import os; print(os.environ['ACLI_TOKEN'])"''',
        '''python -Bc "import os; print(os.environ['ACLI_TOKEN'])"''',
        '''node --eval="console.log(process.env.GITHUB_TOKEN)"''',
        '''python -c"import os; print(os.environ['ACLI_TOKEN'])"''',
        # Completions of the named interpreters' own documented flag sets: node's `-p`/`--print` prints its
        # result with no print call in the code at all, and a versioned python basename is the same
        # interpreter under a name exact membership never matched. perl's `-E` is pinned on the matcher
        # below instead, for the reason given there.
        '''node -p "process.env.GITHUB_TOKEN"''', '''node --print "process.env.GITHUB_TOKEN"''',
        # Auto-print is a property of the invocation, not of the flag carrying the code, and each of these
        # was allowed while the bare `-p` above denied. `-pe CODE` runs both flags with CODE as the *next*
        # token (verified: `node -pe "process.env.FOO"` prints the value), which first-match cluster arity
        # read as `-p` with the attached value `e`, so the real code was never scanned. `--print --eval`
        # spells the two flags apart, and `--eval` is not itself a printing flag. The reversed cluster
        # `-ep` needs no case: node rejects it outright ("node: bad option: -ep", verified), so nothing
        # runs — though the arity rule below handles it anyway.
        '''node -pe "process.env.GITHUB_TOKEN"''',
        '''node --print --eval "process.env.GITHUB_TOKEN"''',
        '''python3.13 -c "import os; print(os.environ['ACLI_TOKEN'])"''',
        '''python2.7 -c "import os; print(os.environ['ACLI_TOKEN'])"''',
        '''python3.13t -c "import os; print(os.environ['ACLI_TOKEN'])"''',
        # The regression an unscoped `prefer_last` caused, and its siblings. `shlex` hands the attached
        # spelling over as the single token `-c<code>`; when that code ends in a character the table lists
        # (`...; import gc` ends in `c`), scanning to the last match reported "the code is the next token",
        # there was no next token, and this — the plainest interpreter leak there is — was allowed. The
        # third case denied throughout (its code ends in `)`), which is exactly why the first two shipped
        # past a self-test that had a case for the attached spelling. The fourth carries an `=` as well, so
        # it also exercises the `=`-split branch ahead of the cluster scan.
        '''python3 -c"import os; print(os.environ['ACLI_TOKEN']); import gc"''',
        '''python -c"import os; print(os.environ['ACLI_TOKEN']); import gc"''',
        '''python3 -c"import os; print(os.environ['ACLI_TOKEN'])"''',
        '''python3 -c"import os; x=os.environ['ACLI_TOKEN']; print(x); import gc"''',
        # An unrecognised assignment prefix does not narrow `_segment_reason`, it disarms its whole
        # anti-pattern path: the walk stops on `FOO+=bar`, reads that as the command, and matches nothing.
        # `NAME+=VALUE cmd` runs in both bash and zsh (verified live), and the pattern was spelled out
        # twice, so `+=` reached one copy and not the other.
        "FOO+=bar echo $ACLI_TOKEN", "FOO+=bar env", "FOO+=bar printenv ACLI_TOKEN",
        "FOO+=bar set", "FOO+=bar export",
        "env FOO+=bar printenv ACLI_TOKEN", "env FOO+=bar echo $ACLI_TOKEN",
        "sudo FOO+=bar echo $ACLI_TOKEN",
        # `--print` takes an attached value too, and every such spelling reported "does not auto-print"
        # while node printed: `--print=0` and `--print=false` auto-print exactly as `--print=1` does
        # (verified live), since node reads an attached value as present-and-therefore-on.
        '''node --print=1 --eval "process.env.GITHUB_TOKEN"''',
        '''node --print=false --eval "process.env.GITHUB_TOKEN"''',
        # node's other console methods print the value exactly as `log` does (verified live), and were all
        # allowed while `_PRINT_CALL` listed `log` alone. `console.warn` denied, but only via the bare
        # `warn` alternative that belongs to perl and ruby — a deny resting on another language's name.
        '''node -e "console.info(process.env.GITHUB_TOKEN)"''',
        '''node -e "console.dir(process.env.GITHUB_TOKEN)"''',
        '''node -e "console.table(process.env.GITHUB_TOKEN)"''',
        '''node -e "console.debug(process.env.GITHUB_TOKEN)"''',
        '''node -e "console.warn(process.env.GITHUB_TOKEN)"''',
        # The rest of that same family, each measured on node v24.11.1 to reproduce a sentinel value and
        # each allowed by a matcher that had closed the four spellings its docstring named instead of the
        # class they belong to. `console.error` is the plainest of them and was the one still open.
        '''node -e "console.error(process.env.GITHUB_TOKEN)"''',
        '''node -e "console.trace(process.env.GITHUB_TOKEN)"''',
        '''node -e "console.group(process.env.GITHUB_TOKEN)"''',
        '''node -e "console.groupCollapsed(process.env.GITHUB_TOKEN)"''',
        '''node -e "console.count(process.env.GITHUB_TOKEN)"''',
        '''node -e "console.assert(false, process.env.GITHUB_TOKEN)"''',
        '''node -e "console.dirxml(process.env.GITHUB_TOKEN)"''',
        # A tool call's result carries stderr as well as stdout into transcript history, so the stderr
        # twins of the two stdout writers already matched are the same leak. Both verified live.
        '''python3 -c "import os, sys; sys.stderr.write(os.environ['ACLI_TOKEN'])"''',
        '''node -e "process.stderr.write(process.env.GITHUB_TOKEN)"''',
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
    # Pinned on the matcher itself, not only through the commands above: the `--` bug shipped past a
    # self-test that had command-level cases for every other spelling, because none of them put a bare
    # `--` in front of `env`. Both tables, since the fault was that the flags argument never mattered.
    for table in (_ENV_ARG_FLAGS, _WRAPPER_ARG_FLAGS["sudo"], _WRAPPER_ARG_FLAGS["timeout"]):
        assert not _consumes_next("--", table), (
            "`--` ends option processing and names no value, whatever the table holds"
        )
    assert _consumes_next("--unset", _ENV_ARG_FLAGS), "and the prefix rule it rides on still works"
    assert _consumes_next("--uns", _ENV_ARG_FLAGS)
    # Pinned on the matcher for the same reason: which token carries code, in each spelling, and per
    # interpreter. perl's `-E` and ruby's `-e` reach no command-level deny case above, because
    # `_ENV_ACCESS` recognises python's and node's env-access syntax only and neither perl's `$ENV{...}`
    # nor ruby's `ENV[...]` is in it — a separate, still-open gap in a different matcher. `-E` is
    # completed here anyway so the arity half is not the thing missing when that one is closed.
    perl, ruby = INTERPRETER_CODE_FLAGS["perl"], INTERPRETER_CODE_FLAGS["ruby"]
    python, node = INTERPRETER_CODE_FLAGS["python"], INTERPRETER_CODE_FLAGS["node"]
    assert _code_argument(["perl", "-E", "CODE"], 1, perl) == ("-E", "CODE"), "perl's -E takes code like -e"
    assert _code_argument(["ruby", "-E", "utf-8", "-e", "CODE"], 1, ruby) is None, (
        "ruby's own -E is an encoding, not code: a shared flag table would read the encoding as the code"
    )
    assert _code_argument(["ruby", "-pe", "CODE"], 1, ruby) == ("-pe", "CODE"), (
        "and `-p` must stay out of ruby's table, or this cluster's code argument is read as `e`"
    )
    assert _code_argument(["python", "-uc", "CODE"], 1, python) == ("-uc", "CODE")
    assert _code_argument(["python", "-cCODE"], 1, python) == ("-c", "CODE"), "attached to a short option"
    assert _code_argument(["node", "--eval=CODE"], 1, node) == ("--eval", "CODE"), "and to a long one"
    assert _code_argument(["python", "-c"], 1, python) is None, (
        "a separated code flag in final position carries no code, and must not index past the tokens"
    )
    assert _interpreter_key("python3.13") == "python", "a versioned alias is the same interpreter"
    assert _interpreter_key("pythonista") is None, "a versioned alias, not any name that starts so"
    assert _code_argument(["python", "foo.c", "CODE"], 1, python) is None, (
        "`_consumes_next` reads any token as a cluster, so only an option-shaped one may be asked"
    )
    # A code-flag table lists interchangeable carriers, so the cluster scan has to run to the end of the
    # token: node's `-pe CODE` is both flags with CODE as the next token, and first-match-wins returned
    # `-p` with the attached value `e` instead. Pinned on the matcher as well as through the command above,
    # because the two spellings of the same bypass were found one at a time.
    assert _code_argument(["node", "-pe", "CODE"], 1, node) == ("-pe", "CODE"), "both flags, code follows"
    assert _consumes_next("-pe", node, prefer_last=True), "the last listed character is the token's last"
    assert not _consumes_next("-pe", node), (
        "and the default stays first-match-wins, which is the right rule for `sudo`-style tables"
    )
    assert _consumes_next("-nu", _WRAPPER_ARG_FLAGS["sudo"]), "unchanged for every existing call site"
    # And the other direction, which the `-pe` fix above regressed: `prefer_last` belongs to a token that
    # is actually a short-option cluster, not to an attached code string whose last character happens to
    # name a listed flag. Pinned on the discriminator itself, because every command-level case for the
    # attached spelling carried code ending in `)` and so never reached it.
    assert _is_short_cluster("-pe") and _is_short_cluster("-uIsSBc") and _is_short_cluster("-c")
    assert not _is_short_cluster("-cimport os; print(os.environ['X']); import gc")
    assert not _is_short_cluster("--print") and not _is_short_cluster("--") and not _is_short_cluster("-")
    assert not _is_short_cluster("foo.c"), "and it is not the option-shape test, which is the caller's"
    # The proof that discriminator rests on: for a token it accepts, the read `prefer_last` costs is an
    # all-alphanumeric substring, and no all-alphanumeric string can access the environment or print —
    # every `_ENV_ACCESS` alternative needs a `.` and/or a `[`, and `_PRINT_CALL` needs a `(`. Checked, not
    # just argued, so an all-alnum env-access or print idiom added to either matcher fails here rather than
    # silently voiding the reasoning.
    import itertools

    alnum_samples = [
        *("".join(p) for n in (1, 2, 3) for p in itertools.product("acepnEV0", repeat=n)),
        "print", "consolelog", "osenviron", "osenvironget", "osgetenv", "processenv",
        "processenvACLITOKEN", "sysstdoutwrite", "puts", "warn", "ACLITOKEN",
    ]
    for sample in alnum_samples:
        assert sample.isalnum(), sample
        assert not _ENV_ACCESS.search(sample), f"an all-alnum string must not read as env access: {sample!r}"
        assert not _PRINT_CALL.search(sample), f"an all-alnum string must not read as a print call: {sample!r}"
    for tok, prints in (
        ("-p", True), ("--print", True), ("-pe", True), ("-ep", True), ("-np", True),
        ("-e", False), ("--eval", False), ("--", False), ("--eval=CODE", False), ("code", False),
        # `--print` carries an attached value too, and node treats any attached value as on: `--print=0`
        # and `--print=false` print (verified live), so the value is not parsed. `--printer` keeps the
        # `=`-split head from degrading into a prefix test.
        ("--print=1", True), ("--print=0", True), ("--print=true", True), ("--print=false", True),
        ("--print==", True), ("--printer", False),
    ):
        assert _prints_result(tok) is prints, f"{tok!r} auto-print should be {prints}"
    # An attached spelling puts the flag and the code in one token, and a deny reason naming that token
    # would copy arbitrary code — the very thing this hook keeps out of transcript history — into the
    # transcript itself. Every reason names the flag alone.
    for command in (
        '''python -c"import os; print(os.environ['ACLI_TOKEN'])"''',
        '''node --eval="console.log(process.env.GITHUB_TOKEN)"''',
        # Scoping `prefer_last` sends this one to the attached-short-option read, which narrows the reason
        # from the whole token to `-c` alone — so the hygiene half of that fix is pinned here too.
        '''python3 -c"import os; print(os.environ['ACLI_TOKEN']); import gc"''',
    ):
        reason = decision("Bash", {"command": command})
        assert reason is not None, command
        assert "os.environ" not in reason and "process.env" not in reason, (
            f"the reason must name the flag, not echo the code back: {reason!r}"
        )
    _EXTRA_WORDS = saved_extra_words

    _test_generated_option_spellings_deny_whenever_the_tool_prints()
    _test_a_deep_env_chain_terminates_and_an_unevaluable_command_denies()
    _test_an_oversized_command_is_refused_instead_of_scanned()
    _test_stdin_that_cannot_be_read_denies_rather_than_returning_silently()
    _test_a_command_that_is_not_a_string_denies_rather_than_being_coerced()
    _test_an_in_place_edit_excuses_no_segment_and_denies_none()
    _test_extra_words_from_config()
    _test_unresolvable_config_warns_rather_than_dropping_silently()


def _test_generated_option_spellings_deny_whenever_the_tool_prints() -> None:
    """The structural check: option spellings generated from the tables, not curated by hand.

    Deliberately its own test rather than more entries in the curated `allow`/`deny_cases` lists above,
    because the two artefacts prove different things. Those lists are a record of spellings review has
    already found; this one asks the tables what spellings exist. Seven rounds of review found exactly one
    more hand-missed spelling each, and round 6's own fix regressed the headline interpreter deny — an
    unscoped `prefer_last` made `python3 -c"...; import gc"` allow — past a curated list that already had a
    case for the attached spelling. That regression is why the payload varies here too and not only the
    flag spelling: the collision is between the *code's last character* and a listed flag character (see
    `_GENERATED_CODE_SUFFIXES`), so a flags-only matrix would have shipped it as well.

    Verified to be a real detector rather than a restatement of current behaviour, by driving this same
    generated list against two historical commits as real hook subprocesses: of 1149 cases it fails 22
    against `a0add099` — the regressed commit, on the payload-suffix and attached-code cases plus
    `--print=<value> --eval` — and 25 against `ff78dcf`, the pre-regression commit, on node's `-pe`,
    `--print --eval` and `--print=<value>` spellings that round 6 closed. It passes here with no
    crash-denies. Neither historical failure set is a subset of the other — `a0add099` fails
    `python -c"…  # c"` where `ff78dcf` passes it, and `ff78dcf` fails `node -pe` where `a0add099` passes —
    which is the property a curated list kept failing to have: each round's list covered the round before
    it.

    Both sides are asserted, and both are asserted to be non-trivially populated: a deny-only list would
    pass against a hook that denied everything, which is a real failure mode this file has flirted with
    (`env -- ls` was denied by the fix for `env -- echo $ACLI_TOKEN`), and the count assertions mean a
    refactor that silently empties the generator fails loudly instead of passing vacuously.

    The counts are floored per family and not only in aggregate, because an aggregate floor is blind to a
    missing family: the interpreter family is the smallest of the three (233 of 1149) and the only one
    carrying the S3-shape payload collision, and `_generated_interpreter_cases` reaches it through a
    `continue` that has already dropped a whole language's matrix once — so wrapper and `env` alone cleared
    every aggregate floor while interpreter coverage was zero. Each payload in `_GENERATED_CODE_BASES` is
    asserted to appear in a generated command as well as counted, since that is the dict the skip selects
    on: a language dropped from generation then fails by name rather than by an arithmetic near-miss.
    """
    global _EXTRA_WORDS
    saved = _EXTRA_WORDS
    _EXTRA_WORDS = frozenset()  # built-in words only, for the reason the curated lists are pinned so
    try:
        families = _generated_case_families()
        # `(total, deny, allow)` floors as measured today, per family. Widening a table stays a pass; losing
        # one is the failure this replaced an aggregate-only floor to catch.
        for family, (total, deny, allow) in (
            ("wrapper", (806, 483, 323)), ("env", (110, 66, 44)), ("interpreter", (233, 118, 115)),
        ):
            produced = families[family]
            denied = sum(1 for _, expect_deny in produced if expect_deny)
            assert len(produced) >= total, f"the {family} family produced only {len(produced)} cases"
            assert denied >= deny, f"the {family} family produced only {denied} deny cases"
            assert len(produced) - denied >= allow, f"the {family} family produced only {len(produced) - denied} allows"
        interpreter = [command for command, _ in families["interpreter"]]
        for language, bases in _GENERATED_CODE_BASES.items():
            for kind, base in bases.items():
                assert any(base in command for command in interpreter), (
                    f"no generated case carries {language}'s {kind} payload, so the family-selection "
                    "`continue` in `_generated_interpreter_cases` dropped it: the counts above cannot see "
                    "that, and this payload is the leak shape the whole matrix exists to exercise"
                )
        # The S3 detector rests entirely on this: `_generated_interpreter_cases` substitutes a listed flag
        # character into each template, and the collision it detects is between the *code's last character*
        # and that flag. A template with anything after the placeholder — `"  # {ch} ok"` — still generates
        # cases, still passes every floor above, and detects nothing at all.
        for language, suffixes in _GENERATED_CODE_SUFFIXES.items():
            for suffix in suffixes:
                assert suffix.endswith("{ch}"), (
                    f"{language}'s payload suffix {suffix!r} must end in the substituted flag character, or "
                    "the attached-code collision it exists to reach is never spelled"
                )
        cases = _generated_spelling_cases()
        denies = sum(1 for _, expect_deny in cases if expect_deny)
        assert len(cases) > 900, f"the generator produced only {len(cases)} cases: something emptied it"
        assert denies > 500, f"only {denies} generated deny cases"
        assert len(cases) - denies > 200, f"only {len(cases) - denies} generated allow cases"
        for command, expect_deny in cases:
            got = decision("Bash", {"command": command})
            assert (got is not None) is expect_deny, (
                f"generated case expected {'deny' if expect_deny else 'allow'}: {command!r} got {got!r}"
            )
    finally:
        _EXTRA_WORDS = saved


def _test_a_deep_env_chain_terminates_and_an_unevaluable_command_denies() -> None:
    """A crash in this hook is an allow, so the depth `env env env …` reaches must not be the stack's.

    `env`-unwrapping used to recurse per layer, so a command carrying a few hundred `env` tokens raised
    `RecursionError` out of `main()`: nothing on stdout, which Claude Code's `PreToolUse` contract reads
    as no decision, so the command ran with the guard skipped entirely. The chain is unwound in
    `_env_reason`'s own loop now, and `main()` turns any remaining unexpected crash into a deny rather
    than into that silence — checked here by forcing one, since the whole point is that no input is
    supposed to produce it.

    The layer counts are chosen to stay under `MAX_COMMAND_CHARS`, so the harmless case here proves the
    loop terminates rather than proving the length ceiling fires — 2000 layers is already twice the
    default recursion limit the old per-layer recursion died at.
    """
    import contextlib
    import io

    global _EXTRA_WORDS
    saved = _EXTRA_WORDS
    _EXTRA_WORDS = frozenset()
    try:
        chain = "env " * 2000
        assert len(f"{chain}echo $ACLI_TOKEN") < MAX_COMMAND_CHARS, "must test the loop, not the ceiling"
        assert decision("Bash", {"command": chain.strip()}) is not None, "a bare env chain still dumps"
        assert decision("Bash", {"command": f"{chain}echo $ACLI_TOKEN"}) is not None, (
            "a leak behind any number of env layers is still the leak"
        )
        assert decision("Bash", {"command": f"{chain}ls"}) is None, "and a harmless one is still harmless"
        assert decision("Bash", {"command": "env -u A " * 2000 + "printenv ACLI_TOKEN"}) is not None
    finally:
        _EXTRA_WORDS = saved

    def _raise(tool: object, args: dict) -> str | None:
        raise RecursionError("forced")

    saved_decision, saved_stdin, saved_argv = globals()["decision"], sys.stdin, sys.argv
    globals()["decision"] = _raise
    captured = io.StringIO()
    try:
        sys.argv = [saved_argv[0]]  # the hook's own stdin path, not the self-test this runs inside
        sys.stdin = io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}))
        with contextlib.redirect_stdout(captured):
            main()
    finally:
        globals()["decision"], sys.stdin, sys.argv = saved_decision, saved_stdin, saved_argv
    payload = json.loads(captured.getvalue())
    decided = payload["hookSpecificOutput"]
    assert decided["permissionDecision"] == "deny", f"a crash must not fall through as an allow: {payload}"
    assert "forced" not in decided["permissionDecisionReason"], (
        f"the reason must carry no exception text, which could echo the command: {decided}"
    )


def _test_an_oversized_command_is_refused_instead_of_scanned() -> None:
    """A hook slow enough to stall a session is the same fail-open shape as one that crashes.

    `_interpreter_reason` re-scanned the remaining tokens once per interpreter-shaped token, so 50000
    `python` tokens took ~38s on every Bash call that shape reached — no output for long enough that
    Claude Code has nothing to act on. The ceiling in `decision()` refuses that input outright, so the
    timing is part of the claim here, not just the verdict — and it is timed on the worst input the
    ceiling *admits*, since timing the refusal only proves that refusing is cheap.
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
        # The refusal above does no scanning by construction, so timing it pins nothing about the
        # ceiling's own choice of number. The bound that matters is on the worst input the ceiling
        # *admits*, and that is not the interpreter case: `_env_reason` re-joins and re-splits the
        # remaining tokens per `env` layer, so a maximal `env` chain costs ~0.74s against ~0.01s for the
        # same length in interpreter-shaped tokens. 5s is deliberately loose against that ~0.74s — the
        # property is a latency budget that has to hold on slower hardware than whatever runs this, not a
        # benchmark — and it is still well under the ~38s the ceiling replaced.
        worst = "env " * (MAX_COMMAND_CHARS // 4)
        assert len(worst) == MAX_COMMAND_CHARS, "the worst admitted case must sit right at the ceiling"
        started = time.perf_counter()
        reason = decision("Bash", {"command": worst})
        elapsed = time.perf_counter() - started
        assert reason is not None, "a maximal env chain still dumps the environment"
        assert reason != _TOO_LONG, "and must be scanned, not refused: the ceiling is exclusive"
        assert elapsed < 5.0, f"the worst input the ceiling admits took {elapsed:.2f}s"
        assert decision("Bash", {"command": "x" * MAX_COMMAND_CHARS}) is None, (
            "the ceiling is exclusive: a command exactly at it is still evaluated normally"
        )
        # Nothing a human writes in one Bash call comes near this, including a long compound one.
        compound = " && ".join(["git status", "pytest -q -k something_fairly_long", "ruff check ."] * 40)
        assert len(compound) < MAX_COMMAND_CHARS // 4, f"a real compound command is small: {len(compound)}"
        assert decision("Bash", {"command": compound}) is None, "and must not trip the ceiling"
    finally:
        _EXTRA_WORDS = saved


def _test_stdin_that_cannot_be_read_denies_rather_than_returning_silently() -> None:
    """Every way `main()` can fail to decide must deny, including the read that happens before the try.

    `json.load(sys.stdin)` raises `UnicodeDecodeError` on malformed bytes and `OSError` on a broken
    pipe, neither of which the `JSONDecodeError` catch covered — so both escaped as an unhandled crash
    ahead of the backstop around `decision()`, and a `PreToolUse` hook that writes nothing is read as no
    decision, i.e. an allow. The caught case returned silently, which is that same allow — and so did
    input that parsed into something other than a JSON object, one line further down.
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
        ("a JSON list", io.StringIO("[]")),
        ("a JSON string", io.StringIO('"env"')),
        ("a JSON number", io.StringIO("5")),
        ("JSON null", io.StringIO("null")),
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
    """`str()` on an unexpected shape does not raise, so the crash backstop never covered this one.

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
