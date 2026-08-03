"""The `sy` MCP server, built on the official `mcp` SDK's `MCPServer`.

The SDK owns the protocol: framing, `initialize`, protocol-version negotiation, `tools/list`
schema generation from these functions' type hints, `tools/call` dispatch, and the tool-failure
`isError` result. **No code in this package implements or overrides any of that** — a protocol
concern that appears here is a bug, and the version string this server speaks is deliberately not
findable in this repo.

Tools are `async` wherever they do I/O, so a slow attachment upload cannot block an unrelated tool
call. `reload_config` and `validate_config` stay synchronous: they read small local files.
`secrets.sanitize` also stays synchronous inside the async tool — it is local disk work bounded by
the artifact size, and making it awaitable would buy nothing while adding a way to interleave a
scrub with the upload it must strictly precede.

**stdout carries protocol frames and nothing else.** Anything a helper prints to stdout corrupts
the stream and desynchronises the client, which is why the ported adapter code raises instead of
printing and why every diagnostic goes to stderr.

Run it with `pixi run sy-server` (which is `python -m sy_tools.server`); `.mcp.json` registers
exactly that, and passes `--manifest-path ${CLAUDE_PLUGIN_ROOT}/pyproject.toml` because it has to —
`${CLAUDE_PLUGIN_ROOT}` interpolates inside the manifest's JSON strings, which is what makes the
absolute form work regardless of launch cwd; `"cwd"` is not an option, because Claude Code ignores
that key silently.

**Measured, not assumed:** `pixi run <declared-task>` does not inherit the caller's working
directory — it runs the task from the manifest's own directory, where a bare `pixi run <command>`
does inherit it (confirmed with a discriminating control: the same probe reports the manifest
directory when registered as a task, the call site when passed as a command). So this process's
cwd is always the *plugin's* checkout, never the consumer project's — which would break
`config.repo_root()` if it depended on cwd, since a marketplace install's plugin checkout holds no
`.shipyard/` at all. It doesn't: `repo_root()` reads `CLAUDE_PROJECT_DIR` first, the pointer Claude
Code sets for every MCP stdio server it launches (matching hooks), and that env var is immune to
pixi's cwd reset — only falling back to a cwd-derived `git rev-parse --show-toplevel` for the
invocations Claude Code doesn't launch (manual `pixi run sy-server`, `docs/smoke_mcp.py`, pytest),
where cwd is already correct. Switching the launch line to an ad-hoc command instead of the
declared task was the other candidate fix and was rejected: it does dodge the cwd reset, but
`python -m sy_tools.server` then can't find the `sy_tools` package at all in a real install, since
importability here relies on Python's implicit cwd-as-`sys.path[0]` and the consumer project has no
reason to contain a `sy_tools/` package of its own.

The manifest carries no `env` block, which is deliberate and was settled empirically rather than
from documentation: a stdio server inherits the launching process's environment, so the one real
secret (the tracker credential) arrives without ever being named in a committed file. Verified with
a discriminating control — the same `validate_config` call reports the credential present when it
is exported and missing when it is not.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from pydantic import Field, ValidationError

from . import SERVER_NAME, SERVER_VERSION, config, secrets, tracker
from .ship_metrics import SCHEMA_ID, ShipMetricsV1

mcp = MCPServer(name=SERVER_NAME, version=SERVER_VERSION)


class ToolError(RuntimeError):
    """A tool failed in a way the caller should see as a tool result, not a protocol error."""


IssueId = Annotated[
    str,
    Field(
        description="Opaque issue id as the tracker gave it out, e.g. PROJ-123. Passed through "
        "verbatim; never build one or take one apart."
    ),
]
"""The one argument nearly every verb takes, described once so all of them describe it the same way."""


def _required(**fields: str) -> None:
    """Reject an empty or whitespace-only required string argument before any tracker call happens.

    Whitespace counts as empty: a title of `"\\n"` passes schema validation and would otherwise
    create a permanently blank issue that no search can find again.
    """
    for name, value in fields.items():
        if not value.strip():
            raise ToolError(f"{name!r} is required and must be a non-empty string")


@mcp.tool(name="create-issue")
async def create_issue(
    issue_type: Annotated[str, Field(description="Kind of issue to create: `epic`, `task` or `bug`.")],
    title: Annotated[str, Field(description="The issue's one-line title, as plain text.")],
    body: Annotated[str, Field(description="The issue's body, written as Markdown. Omit for no body.")] = "",
    parent: Annotated[
        str | None,
        Field(description="Opaque id of the issue to file this one under. Omit to create it unparented."),
    ] = None,
) -> dict[str, Any]:
    """Create an issue in the configured tracker and return its opaque id and URL.

    Canonical verb `create-issue`. Passing `parent` is also the canonical verb `create-child`:
    there is deliberately no separate tool for a child, because it is this same write.
    """
    _required(title=title)
    return await tracker.adapter().create_issue(issue_type=issue_type, title=title, body=body, parent=parent)


@mcp.tool(name="get-issue")
async def get_issue(issue: IssueId) -> dict[str, Any]:
    """Read one issue in full: body, status, type, relations, labels and every comment on it.

    Canonical verb `get-issue`. Status and type come back as canonical tokens, so a caller can
    branch on them without knowing the board's column names; a column this repo does not map
    passes through under its own name rather than being dropped.
    """
    _required(issue=issue)
    return await tracker.adapter().get_issue(issue)


@mcp.tool(name="update-issue")
async def update_issue(
    issue: IssueId,
    body: Annotated[
        str,
        Field(description="The issue's complete new body as Markdown. Replaces the old one outright."),
    ],
) -> dict[str, Any]:
    """Replace an issue's body with new Markdown.

    Canonical verb `update-issue`. A whole-body replacement, never an append: to keep any of the
    existing body, read it with `get-issue` first and send it back as part of `body`.
    """
    _required(issue=issue)
    return await tracker.adapter().update_issue(issue, body)


@mcp.tool(name="find-issues")
async def find_issues(
    status: Annotated[
        str | None,
        Field(description="Keep only issues at this stage: `backlog`, `ready`, `in-progress`, `in-review`, `done`."),
    ] = None,
    issue_type: Annotated[
        str | None, Field(description="Keep only issues of this kind: `epic`, `task` or `bug`.")
    ] = None,
    parent: Annotated[
        str | None, Field(description="Opaque parent issue id; keeps only that issue's children.")
    ] = None,
    text: Annotated[str | None, Field(description="Words to match against issue title and body.")] = None,
    limit: Annotated[int, Field(description="How many issues at most to bring back in this page.")] = 50,
) -> dict[str, Any]:
    """Search the configured project for issues matching any combination of filters.

    Canonical verb `find-issues`. Every filter is optional and they combine as AND; with none set
    this lists the project's recent issues. One page only: `is_last` says whether more remain, and
    `next_page_token` is the cursor to ask for them where the tracker supports one.
    """
    return await tracker.adapter().find_issues(
        status=status, issue_type=issue_type, parent=parent, text=text, limit=limit
    )


@mcp.tool(name="set-status")
async def set_status(
    issue: IssueId,
    status: Annotated[
        str,
        Field(description="Stage to move the issue to: `backlog`, `ready`, `in-progress`, `in-review` or `done`."),
    ],
) -> dict[str, Any]:
    """Move an issue to a canonical lifecycle status.

    Canonical verb `set-status`. The five canonical stages are the whole vocabulary; each maps to
    the column name this repo configures, which comes back as `native` so the move is auditable.
    `blocked` is not a status — record a blocking relationship with `add-dependency` instead.
    """
    _required(issue=issue)
    return await tracker.adapter().set_status(issue, status)


@mcp.tool(name="assign")
async def assign(
    issue: IssueId,
    assignee: Annotated[
        str,
        Field(description="Who to assign it to. `@me` means the authenticated account and is the only value served."),
    ] = "@me",
) -> dict[str, Any]:
    """Assign an issue and report the account it resolved to.

    Canonical verb `assign`. Self-assignment is the caller need this serves; any other assignee is
    refused rather than quietly landing the issue on the wrong person.
    """
    _required(issue=issue)
    return await tracker.adapter().assign(issue, assignee)


@mcp.tool(name="link-parent")
async def link_parent(
    issue: IssueId,
    parent: Annotated[str, Field(description="Opaque id of the issue to become the parent.")],
) -> dict[str, Any]:
    """Re-parent an existing issue under another issue.

    Canonical verb `link-parent`. Use this for an issue that already exists; to create one already
    parented, pass `parent` to `create-issue`.
    """
    _required(issue=issue, parent=parent)
    return await tracker.adapter().link_parent(issue, parent)


@mcp.tool(name="add-dependency")
async def add_dependency(
    issue: IssueId,
    blocked_by: Annotated[str, Field(description="Opaque id of the issue that must land first.")],
) -> dict[str, Any]:
    """Record that one issue is blocked by another, and verify the direction took.

    Canonical verb `add-dependency`. This, not a status, is how blocking is expressed. The
    direction matters: `issue` waits for `blocked_by`. The result carries `verified` from a
    read-back, so a link recorded the wrong way round cannot pass silently.
    """
    _required(issue=issue, blocked_by=blocked_by)
    return await tracker.adapter().add_dependency(issue, blocked_by)


@mcp.tool(name="add-label")
async def add_label(
    issue: IssueId,
    label: Annotated[str, Field(description="The single label to add, exactly as it should read.")],
) -> dict[str, Any]:
    """Add one label to an issue, keeping the labels already on it.

    Canonical verb `add-label`. Additive by contract, and the full resulting label set comes back
    so the caller can confirm nothing was displaced.
    """
    _required(issue=issue, label=label)
    return await tracker.adapter().add_label(issue, label)


@mcp.tool(name="post-comment")
async def post_comment(
    issue: IssueId,
    body: Annotated[str, Field(description="The comment as Markdown, leading with its TL;DR.")],
) -> dict[str, Any]:
    """Post a Markdown comment on an issue.

    Canonical verb `post-comment`, and the tool for two more canonical verbs that have no tool of
    their own. `post-log` is this call carrying a fenced JSON block and nothing else — a machine
    log is always its own comment, never appended to prose, and honouring that is yours to do.
    `link-pr`'s durable half is this call carrying the PR URL.

    A body that claims `shipyard.ship_metrics.v1` — by naming it, literally or as a `\\uXXXX` escape, or
    by carrying a block that parses as it — must carry exactly one fenced JSON block that validates
    against that schema, and must not name the id anywhere else: none, several, one that does not
    match, or a mention loose in the body or in a fence left unclosed, and nothing is posted. Every
    other body passes through unchanged.
    """
    _required(issue=issue)
    _validate_machine_log(body)
    return await tracker.adapter().post_comment(issue, body)


_FENCE_OPENER = re.compile(r"[ \t]*(?:`{3,}|~{3,})")
"""A line that opens a fenced block: the marker, then anything at all in the info-string position."""

_FENCE_CLOSER = re.compile(r"[ \t]*(?:`{3,}|~{3,})[ \t]*")
"""A line that closes one: the marker alone. Text after the marker leaves the block open."""


def _fenced_contents(body: str) -> list[tuple[int, int]]:
    """Every properly closed fenced block's *content* span, as offsets into `body`.

    One forward pass over the lines, because the equivalent backtracking pattern was quadratic: a
    lazy `(.*?)` reaching for the next closing marker re-scans the whole rest of the body once per
    opener that never closes, which measured 93 seconds on a 320 KB body holding 40,000 unclosed
    openers. That is not merely slow — `_validate_machine_log` runs synchronously inside the
    `post_comment` coroutine, so one malformed body stalls the event loop for every other tool call
    in flight. This scan is linear in the body's length and keeps no state beyond the open fence.

    The matching rules are the pattern's, unchanged, and each is load-bearing:

    * Lines are split on `\\n` alone. `str.splitlines` also splits on `\\r`, `\\f` and more, which
      would newly *find* the block in a CRLF body — where `` ```\\r `` is not the marker alone, so
      nothing closes and the whole body stays unfenced text, which is the refusal that body earns.
    * The info string is anything. `handoff-accounting.md` writes the metrics block as ```` ```json ````,
      but a caller that omits the language must not thereby skip validation, and what decides whether
      a block is a machine log is the `schema` key inside it, not the fence it arrived in.
    * An opener needs a newline after it, so a marker on an unterminated last line opens nothing.
    * Backticks and tildes are interchangeable within a pair and the counts need not match. Loose on
      purpose: a looseness here can only ever *find* a block, and a block found is a block validated,
      where a block missed is one that reaches the tracker unread.
    """
    spans: list[tuple[int, int]] = []
    lines = body.split("\n")
    last = len(lines) - 1
    content_start: int | None = None
    offset = 0
    for index, line in enumerate(lines):
        line_start, offset = offset, offset + len(line) + 1
        if content_start is None:
            if index < last and _FENCE_OPENER.match(line):
                content_start = offset
        elif _FENCE_CLOSER.fullmatch(line):
            spans.append((content_start, line_start))
            content_start = None
    return spans


def _outside_fences(body: str, spans: list[tuple[int, int]]) -> str:
    """Whatever is left of the body once every properly closed block's *content* is cut out.

    Cut by span rather than by substring replacement: two identical blocks in one body would make
    `str.replace` remove the wrong copies and leave the count short.

    The cut is the content span, not the whole block, so the fence delimiter lines stay in what comes
    back. That matters for the opening line: content starts after its newline, so anything sitting in
    the info-string position — conventionally a bare language tag, but anything is allowed there — is
    not content and is never parsed or validated. Cutting the whole block swallowed it, which let one
    fence carry a second machine log there that reached the tracker unread. Leaving it in makes it a
    stray mention like any other unfenced text.
    """
    kept, cursor = [], 0
    for start, end in spans:
        kept.append(body[cursor:start])
        cursor = end
    kept.append(body[cursor:])
    return "".join(kept)


def _as_json(block: str) -> object:
    """One fenced block's parsed JSON, or None when it is not JSON at all (a shell sample, prose).

    `json.loads` fails in three ways and only one is a `JSONDecodeError`: an integer literal past
    `sys.int_info.str_digits_check_threshold` raises a bare `ValueError` from the digit-conversion
    guard, and nesting deeper than the decoder's recursive descent raises `RecursionError`. All three
    mean the same thing here — this content does not parse, so fall back to reading its text — and
    catching only the decode error let a body pick which uncaught exception escaped the tool instead.
    """
    try:
        return json.loads(block)
    except (ValueError, RecursionError):  # JSONDecodeError is a ValueError.
        return None


_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
"""A JSON `\\uXXXX` escape. Fixed width, so matching it cannot backtrack and the scan stays linear."""


def _json_unescaped(text: str) -> str:
    """`text` with every `\\uXXXX` JSON escape decoded to the character it names.

    Not a JSON string decode — a backslash here is only ever itself — just enough that a literal search
    for `SCHEMA_ID` cannot be evaded by spelling one of its ASCII characters as an escape instead. That
    is what the parse-based identity in `_record` settles for content that parses; this is the same
    question answered for the text that no parse can speak to, so both spellings get one answer.

    One replacement per match is exact for this purpose: `SCHEMA_ID` is pure ASCII, so no surrogate
    pair has to be rejoined (that matters only for a non-BMP character, and it has none). Decoding
    cannot hide a literal occurrence either — a match starts `\\u` and `SCHEMA_ID` holds neither
    character, and it is too long and too non-hex to sit inside one match's four digits.
    """
    return _UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)


def _record(parsed: object) -> dict[Any, Any] | None:
    """The `shipyard.ship_metrics.v1` record a parsed block *is*, or None when it is not one.

    Identity is decided on the **parsed** value, never on the raw text, so it holds however the JSON
    spelled that string: `"shipyard.ship_metrics.v\\u0031"` decodes to this id and is this record.
    """
    if isinstance(parsed, dict) and parsed.get("schema") == SCHEMA_ID:
        return parsed
    return None


def _claims_within(parsed: object) -> bool:
    """Whether a parsed block carries a `shipyard.ship_metrics.v1` object at any depth inside it.

    A record one level down is not a machine log this can validate — `_record` is what a machine log
    *is* — but it is unmistakably a claim, and the only alternative to counting it is posting it
    unread. Escaping is why the depth matters: a literal `[{"schema": "shipyard.ship_metrics.v1"}]`
    is caught by the raw-text fallback, so leaving the escaped spelling of that same block to pass
    would be the identity-versus-text gap reopened one level down.

    Iterative rather than recursive: `json.loads` accepts nesting deep enough to blow a recursive
    walk's stack, and a body should not be able to choose which exception this check raises.
    """
    stack = [parsed]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            if value.get("schema") == SCHEMA_ID:
                return True
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return False


def _validate_machine_log(body: str) -> None:
    """Reject a malformed `shipyard.ship_metrics.v1` block before the comment is posted.

    Naming the schema id anywhere in the body arms this check, and so does carrying a fenced block
    that parses as this schema however it spelled the id; a body that does either must then carry
    exactly one fenced block that validates against it. Everything else — prose, a code sample,
    another machine log's schema id — never claims this id and passes through untouched.

    Arming on the id rather than only on a *valid* block is deliberate, and is what closes the
    bypasses this had: a trailing comma inside the fence, a CRLF body whose closing fence the pattern
    cannot see, a heading with the block pasted as prose, all reached the tracker unvalidated because
    each one failed to produce a block to validate and a missing block was read as "nothing to check".
    The cost is that a prose comment quoting the schema id must now carry a valid block or be reworded;
    the incident this closes off is a metrics comment that landed with a field name nobody noticed was
    wrong and was read as authoritative afterwards, and an unparseable one is that same incident.

    *Exactly* one, because two candidate blocks are ambiguous rather than one to validate and one to
    ignore: a body quoting an earlier valid metrics block above the new one it means to post would
    otherwise validate off whichever came first and post the other unread, which is the same
    unvalidated-block incident wearing a valid block as cover. Nothing is chosen for the caller —
    the comment is refused and the extra block has to go.

    A block whose content will not parse counts as claiming the schema when its *raw text* names the
    id — the same reason the body-level check arms on the id. Counting only blocks that parsed
    into a matching object reopened the bypass one level down: a valid block beside a second block
    that named this id but held a trailing comma left the malformed one invisible to both the
    ambiguity count and the schema check, so the valid one validated alone and the comment posted
    carrying an unread machine log. Unparseable-but-claiming is a refusal, never a block to skip.

    Counting only what `_fenced_contents` finds left that same bypass open one level further down, since
    a block is only found once its closing marker is a bare fence on a line of its own: an unclosed
    fence, or a closing marker with text after it, produced nothing to tally at all. So the id is also
    looked for in what is left of the body after every found block is cut out, and a mention there is
    a refusal on its own terms — with a valid block beside it, that mention is one more candidate the
    caller has to resolve, and without one it is the malformed-log case the body-level arming already
    caught. The narrowness that keeps this usable is unchanged: unfenced *text* is only a candidate
    when it names this id, so quoting someone else's broken JSON beside a valid log still posts.

    "Raw text" means the block's *content*, and the split between content and delimiter is where that
    bypass surfaced next: a fence's opening line can carry text after the marker, and that text is not
    content, so it is never parsed or validated. Tallying candidates off the whole block counted a
    fence whose info string held a complete second machine log — while validating only the content
    beside it, which was itself valid — so the comment posted with the info string's log inside it
    verbatim. Both halves of the answer are one change: candidates are counted in the content span,
    and `_outside_fences` cuts only that span, which leaves the info string to the stray-mention
    check. A plain language tag mentions nothing and changes nothing.

    All of which was decided by *literal text* while what counts as the record was decided by a
    *parse* — two rules for one question, and the bypass lives in the gap between them, because JSON
    can spell the same string many ways. A block reading `{"schema": "shipyard.ship_metrics.v\\u0031",
    ...}` holds no literal occurrence of the id at all, so it armed nothing and posted whole: blank
    task, negative counts, misspelled field names, none of it looked at. Beside a valid block it was
    worse, being invisible to the tally too, so the valid one validated alone. So identity is settled
    once, on the parsed value (`_record`), for both arming and counting — immune to any escaping,
    because `json.loads` has already undone it. Literal text survives only as the *fallback* for
    content a parse cannot speak to, which is what keeps an unparseable claim a refusal, and as the
    only available signal for a stray mention, which is text and nothing else.

    One rule, one question, at every depth: `_claims_within` looks for that same parsed identity below
    the top level too, because the fallback catches `[{"schema": "shipyard.ship_metrics.v1"}]` on its
    text while the top-level parse alone would let the escaped spelling of that same block through —
    the gap this closes, reopened one level down. Such a claim is counted but never validated: what a
    machine log *is* stays the top-level object, so a buried one is refused as no block at all.

    And one rule in every *shape*, because settling identity on a parse only answers the shapes where a
    parse happens — a properly closed fence holding clean JSON. Everywhere else this reads text and
    nothing else: arming, an unparseable block's claim, a mention outside the fences. Each of those was
    a plain substring search, so the escaped spelling walked through every shape that produces no block
    to parse — an unclosed fence, a closing marker with trailing text, a CRLF body, unfenced prose — and
    through one that produces a block `json.loads` refuses, a leading BOM being enough. So every one of
    those searches asks `_json_unescaped` first: the fallback answers the escaped spelling exactly as it
    answers the literal one, which is the whole of the rule the parse settles for content it can read.
    """
    # A JSON string decodes to this id either by naming it outright or by escaping part of it, and every
    # JSON escape is a backslash — so a body with neither cannot hold a claim in any spelling, and is
    # answered without scanning or unescaping it at all. Almost every body is this one.
    if SCHEMA_ID not in body and "\\" not in body:
        return
    named = SCHEMA_ID in _json_unescaped(body)
    spans = _fenced_contents(body)
    records: list[dict[Any, Any]] = []
    unread = 0
    for start, end in spans:
        content = body[start:end]
        parsed = _as_json(content)
        record = _record(parsed)
        if record is not None:
            records.append(record)
        elif SCHEMA_ID in _json_unescaped(content) or _claims_within(parsed):
            unread += 1
    # The other half of arming: a body that never names the id in plain text is still this check's
    # business the moment a block claims this schema, which is the escaped-record case.
    if not named and not records and not unread:
        return
    stray = SCHEMA_ID in _json_unescaped(_outside_fences(body, spans))
    blocks = len(records) + unread
    if blocks + stray > 1:
        raise ToolError(
            f"this comment claims {SCHEMA_ID} in {blocks + stray} places, so it was not posted: "
            f"{blocks} in a properly closed fenced block"
            + (", and once outside any such block" if stray else "")
            + ". Which one is the machine log is ambiguous, and validating one of them would post the "
            "others unchecked. A machine log is always its own comment carrying exactly one such block: "
            "post the log on its own, and when quoting earlier numbers as prose, leave the literal id "
            "out — say `the ship metrics log` instead, because naming the id arms this check."
        )
    if records:
        try:
            ShipMetricsV1.model_validate(records[0])
        except ValidationError as exc:
            raise ToolError(
                f"this comment carries a {SCHEMA_ID} block that does not match the schema, so it was "
                f"not posted: {exc.error_count()} problem(s): "
                + "; ".join(f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors())
                + ". The field definitions are in skills/ship/references/handoff-accounting.md."
            ) from None
        return
    # Exactly one claim and it is not a record: an unparseable block, a claim buried inside a block that
    # parsed as something else, or a mention outside every block. Zero claims cannot arrive here — a
    # named id sits either in some block's content, which makes that block a claim, or outside them all.
    raise ToolError(
        f"this comment claims {SCHEMA_ID} but carries no fenced block that parses as one, so it was not "
        "posted. A machine log is a fenced JSON object whose `schema` key is that id: check the JSON "
        "parses (a trailing comma is the usual culprit), that the closing fence is on a line of its own "
        "with nothing after it and Unix line endings, and that the block is fenced at all. If the comment "
        "is prose that merely mentions the schema, say `the ship metrics log` instead — the id arms this "
        "check. The "
        "field definitions are in skills/ship/references/handoff-accounting.md."
    )


@mcp.tool(name="preflight")
async def preflight() -> dict[str, Any]:
    """Check that the configured tracker's credential and account are usable before relying on them.

    Canonical verb `preflight`. Reports what it confirmed and never echoes a secret value. Run it
    once up front so a credential problem surfaces there instead of as a half-finished workflow.
    """
    return await tracker.adapter().preflight()


@mcp.tool(name="attach-artifact")
async def attach_artifact(
    issue: IssueId,
    path: Annotated[str, Field(description="Path to the artifact to attach.")],
    kind: Annotated[
        str, Field(description="Artifact kind. `transcript` is gated; anything else is ungated.")
    ] = "transcript",
    process_tier: Annotated[
        Literal["full", "light"] | None,
        Field(description="The calling workflow's process tier. `ship` requires `full`."),
    ] = None,
    caller: Annotated[
        str, Field(description="Workflow asking for the attachment, e.g. ship, spec, plan.")
    ] = "",
) -> dict[str, Any]:
    """Sanitise a local file and attach it to a tracker issue as a durable artifact.

    Canonical verb `attach-artifact`. Runs the known-value scrub and the pattern scanner, in that
    order, before anything leaves the machine. Gated by the `transcript.attach` config key; for
    `ship` callers the `full` process tier is required on top of it. When the gate is off the call
    is a no-op skip: nothing is read, scrubbed, scanned, or uploaded.
    """
    _required(issue=issue)

    skip = _gate_skip_reason(kind, caller, process_tier)
    if skip is not None:
        return {"attached": False, "skipped": True, "reason": skip, "issue": issue}

    _required(path=path)
    artifact = Path(path)
    if not artifact.is_file():
        raise ToolError(f"artifact not found: {artifact}")
    backend = tracker.adapter()
    required = tuple(config.adapter_map().get("secret_env", []))
    report = secrets.sanitize(artifact, require=required, extra_words=config.extra_secret_words())
    evidence = await backend.attach_artifact(issue, artifact)
    return {"attached": True, "skipped": False, "issue": issue, "sanitize": report, "evidence": evidence}


def _gate_skip_reason(kind: str, caller: str, tier: object) -> str | None:
    """Why this attachment must not happen, or None to proceed.

    Mirrors the adapter attachments reference under `skills/tracker/`: `transcript.attach` gates
    transcript attachment everywhere, and a `ship` caller additionally requires the `full`
    process tier on top of it.
    """
    if kind != "transcript":
        return None
    if not config.get("transcript.attach"):
        return "transcript.attach is false"
    if caller == "ship" and tier != "full":
        return f"ship requires the full process tier; got {tier!r}"
    return None


@mcp.tool(name="type-convert")
async def type_convert(
    issue: IssueId,
    issue_type: Annotated[str, Field(description="Kind to convert the issue to: `epic`, `task` or `bug`.")],
) -> dict[str, Any]:
    """Change an existing issue's kind in place, verified by reading the new kind back.

    Canonical verb `type-convert`. Best-effort by nature: a tracker may refuse the change outright
    (a workflow rule, a required field, a hierarchy constraint), and this fails loudly naming the
    kind the issue still has rather than reporting a conversion that did not happen. Side effects
    follow the kind — parent links and board membership among them — and are not reversible by
    converting back, so confirm the target before calling. When a tracker refuses, create the new
    issue, link it, and close the old one instead.
    """
    _required(issue=issue, issue_type=issue_type)
    return await tracker.adapter().type_convert(issue, issue_type)


@mcp.tool(name="attachment-download")
async def attachment_download(
    issue: IssueId,
    filename_or_id: Annotated[
        str,
        Field(description="The attachment's filename, or its tracker-native id when duplicate names exist."),
    ],
    output_path: Annotated[str, Field(description="Local path to write the downloaded bytes to.")],
) -> dict[str, Any]:
    """Download one artifact already attached to an issue, to a local path.

    Canonical verb `attachment-download`. Resolution is by filename with an exactly-one-match rule;
    pass the tracker-native id instead when an issue carries several attachments of the same name. An
    ambiguous or absent match fails rather than picking one, because the wrong artifact downloaded
    under the right name is indistinguishable from the right one afterwards.
    """
    _required(issue=issue, filename_or_id=filename_or_id, output_path=output_path)
    return await tracker.adapter().attachment_download(issue, filename_or_id, Path(output_path))


@mcp.tool(name="attachment-update")
async def attachment_update(
    issue: IssueId,
    path: Annotated[str, Field(description="Path to the replacement artifact. Its filename picks the target.")],
    kind: Annotated[
        str, Field(description="Artifact kind. `transcript` is gated; anything else is ungated.")
    ] = "transcript",
    process_tier: Annotated[
        Literal["full", "light"] | None,
        Field(description="The calling workflow's process tier. `ship` requires `full`."),
    ] = None,
    caller: Annotated[
        str, Field(description="Workflow asking for the replacement, e.g. ship, spec, plan.")
    ] = "",
) -> dict[str, Any]:
    """Replace an issue's attachment of the same filename, sanitising the replacement first.

    Canonical verb `attachment-update`. Replace-by-filename, taking no id: calling it where nothing
    already matches `path`'s filename is a plain upload. Where more than one existing attachment
    shares that filename, what happens is adapter-specific (see the tracker's own `ADAPTER.md`) — the
    two trackers offer no common primitive for "replace all of these". It runs the same gate and the
    same two sanitisation passes, in the same order, as `attach-artifact` — a second upload path that
    skipped them would be exactly the hole that keeping both passes inside one tool exists to close.

    Destructive: the artifact it replaces is irrecoverable once the replacement lands, and there is
    no undo — how each tracker performs the replacement is in its own `ADAPTER.md`. Confirm the
    target first.
    """
    _required(issue=issue)

    skip = _gate_skip_reason(kind, caller, process_tier)
    if skip is not None:
        return {"updated": False, "skipped": True, "reason": skip, "issue": issue}

    _required(path=path)
    artifact = Path(path)
    if not artifact.is_file():
        raise ToolError(f"artifact not found: {artifact}")
    backend = tracker.adapter()
    required = tuple(config.adapter_map().get("secret_env", []))
    report = secrets.sanitize(artifact, require=required, extra_words=config.extra_secret_words())
    evidence = await backend.attachment_update(issue, artifact)
    return {"updated": True, "skipped": False, "issue": issue, "sanitize": report, "evidence": evidence}


@mcp.tool(name="reload_config")
def reload_config() -> dict[str, Any]:
    """Re-read the Shipyard configuration layer chain from disk and replace the server's hot copy.

    Reports whether the resolved values changed; never reports a value.
    """
    return config.reload()


@mcp.tool(name="validate_config")
def validate_config() -> dict[str, Any]:
    """Report every reason the resolved configuration would be rejected.

    Covers schema violations, missing required keys, an unknown tracker, a required credential absent
    from the environment, an environment variable that outranks the resolved per-agent models, and
    model-floor breaches. Side-effect-free, and never prints a secret value.
    """
    errors = config.validate()
    report: dict[str, Any] = {"valid": not errors, "errors": errors}
    try:
        report["tracker"] = config.get("tracker")
        report["fingerprint"] = config.fingerprint()
    except config.ConfigError:
        pass  # unresolvable config: the errors list already carries the reason, and this
        # tool's whole contract is to report a broken config rather than crash on one
    return report


if __name__ == "__main__":
    mcp.run("stdio")
