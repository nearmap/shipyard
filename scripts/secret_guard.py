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
The denial message points at the safe alternatives that already exist — `sy_preflight.py check`
and the tracker's own `preflight` verb, which name what is missing without printing a value, or a
bare presence check (`[ -n "$VAR" ]`) for anything else.

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
import os
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
_IN_PLACE_EDITORS = {"sed", "perl"}
_IN_PLACE_FLAG = re.compile(r"^-[A-Za-z]*i|^--in-place")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
_ADVICE = (
    'this can print a secret value into this command\'s own tool-call result, which becomes '
    'permanent transcript history. Use a presence-only check instead (`[ -n "$VAR" ]`), or for '
    "the tracker: `sy_preflight.py check` / the adapter's `preflight` command, which names what's "
    "missing or dead without ever printing a value."
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

    The in-place-edit exemption (`sed -i`, `perl -pi` — review_guard's concern, not this hook's) is
    applied per segment, not to the command string as a whole. Matched against the whole string and
    returning early, it disarmed the entire gate for any compound command that merely *contained*
    such a token: `sed -i.bak s/a/b/ f && echo $ACLI_TOKEN` was allowed, so the cheapest way past
    this hook was to prefix a harmless in-place edit. Every other segment is still checked.

    Within a segment the exemption keys on the segment's actually-invoked command, not on its text.
    A text search reproduced the same fail-open one level down, because the token can appear in a
    segment whose real command is a printing one: `echo "sed -i" $ACLI_TOKEN`, `echo $ACLI_TOKEN
    # sed -i` and `printf "ran sed -i %s" "$ACLI_TOKEN"` were all allowed, each of them the exact
    leak this hook exists to deny with the exempting token as a quoted argument or a comment.
    """
    if tool != "Bash":
        return None
    command = str(args.get("command", ""))
    reason = _interpreter_reason(command)
    if reason:
        return f"secret guard: {reason} — {_ADVICE}"
    for segment in re.split(r"[;&|\n]+", command):
        if _is_in_place_edit(segment):
            continue
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

    Leading `FOO=bar` assignments and `WRAPPERS` (`sudo somecmd`, `timeout 5 somecmd`) are stepped
    over so the name returned is the one that runs, and it is basenamed so `/bin/echo` reads as `echo`.
    """
    tokens = [t.strip("\"'") for t in segment.split()]
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        base = tok.lstrip("\\").rsplit("/", 1)[-1]
        if _ASSIGNMENT.fullmatch(tok) or base in WRAPPERS:
            i += 1
            continue
        break
    if i >= len(tokens):
        return None
    return tokens[i].lstrip("\\").rsplit("/", 1)[-1], tokens[i + 1:]


def _is_in_place_edit(segment: str) -> bool:
    """Whether this segment's own invoked command is an in-place edit (`sed -i`, `perl -pi`).

    Decided from the leading command rather than by searching the segment's text, so the token has to
    be the thing being run: quoted inside an `echo`'s argument, in a trailing `#` comment or in a
    `printf` format string it names no in-place edit and exempts nothing.
    """
    leading = _leading_command(segment)
    if leading is None or leading[0] not in _IN_PLACE_EDITORS:
        return False
    return any(_IN_PLACE_FLAG.match(arg) for arg in leading[1])


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
    _EXTRA_WORDS = saved_extra_words

    _test_in_place_edit_exemption_is_per_segment()
    _test_in_place_edit_exemption_needs_to_be_the_invoked_command()
    _test_extra_words_from_config()
    _test_unresolvable_config_warns_rather_than_dropping_silently()


def _test_in_place_edit_exemption_is_per_segment() -> None:
    """The `sed -i`/`perl -pi` exemption may only excuse the segment the token appears in.

    Matched against the whole command string it was a general bypass of this hook: prefixing any
    denied command with a harmless in-place edit allowed the whole thing, so every anti-pattern here
    exists to stop was one `sed -i` away from becoming permanent transcript history. Pinned with the
    built-in word list alone, for the same reason the pass/fail lists above are.
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
        ):
            assert decision("Bash", {"command": command}) is not None, (
                f"an in-place edit must not excuse another segment: {command!r}"
            )
        for command in ("sed -i.bak s/a/b/ f", "perl -pi -e s/a/b/ f", "sed -i s/x/y/ f && ls"):
            assert decision("Bash", {"command": command}) is None, (
                f"the exemption itself must survive for the segment it applies to: {command!r}"
            )
    finally:
        _EXTRA_WORDS = saved


def _test_in_place_edit_exemption_needs_to_be_the_invoked_command() -> None:
    """Mentioning `sed -i` inside a segment may not exempt it; only actually invoking one does.

    Searched for in the segment's text, the exemption was fail-open a second way after being scoped
    per segment: the token needs no command of its own to appear, so a quoted `echo` argument, a
    trailing `#` comment or a `printf` format string carried it and disarmed the check for a segment
    whose real command was the leak. Cheaper than the compound-command bypass it replaced, since it
    needs no extra process — just three characters of text somewhere in the same segment.
    """
    global _EXTRA_WORDS
    saved = _EXTRA_WORDS
    _EXTRA_WORDS = frozenset()
    try:
        for command in (
            'echo "sed -i" $ACLI_TOKEN',
            "echo $ACLI_TOKEN # sed -i",
            'printf "ran sed -i %s" "$ACLI_TOKEN"',
            "echo $ACLI_TOKEN # perl -pi",
        ):
            assert decision("Bash", {"command": command}) is not None, (
                f"a mentioned in-place edit must not exempt the segment that prints a secret: {command!r}"
            )
        for command in ("sed -i.bak s/a/b/ f", "sudo sed -i s/a/b/ f", "perl -i -pe s/a/b/ f"):
            assert decision("Bash", {"command": command}) is None, (
                f"a real in-place edit, wrapped or not, stays exempt: {command!r}"
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
