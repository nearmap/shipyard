"""The `sy` MCP server: every tool the plugin exposes, built on the `mcp` SDK's `MCPServer`.

The SDK owns the protocol — framing, `initialize`, version negotiation, `tools/list` schemas from
these functions' type hints, `tools/call` dispatch, the `isError` result — and no code here
implements or overrides any of it, which is why no protocol version string is findable in this repo.
Tools are `async` wherever they do I/O, so a slow attachment upload cannot block an unrelated call;
the configuration tools stay synchronous, reading small local files and mostly the resolver's hot
copy.

**stdout carries protocol frames and nothing else.** A helper that prints to stdout corrupts the
stream and desynchronises the client, so failures raise and every diagnostic goes to stderr.

Run it with `pixi run sy-server` (which is `python -m sy_tools.server`). `.mcp.json` registers that
and passes `--manifest-path ${CLAUDE_PLUGIN_ROOT}/pyproject.toml` absolutely, because
`${CLAUDE_PLUGIN_ROOT}` interpolates inside the manifest's JSON strings while a `"cwd"` key there is
ignored silently. The manifest carries no `env` block, settled empirically: a stdio server inherits
the launching process's environment, so the tracker credential arrives without ever being named in a
committed file. Measured, not assumed: `pixi run <declared-task>` runs the task from the manifest's
own directory and does not inherit the caller's cwd, where a bare `pixi run <command>` does — so this
process's cwd is always the plugin's checkout, never the consumer project's. Launching an ad-hoc
command instead of the declared task was the other candidate and was rejected: it dodges the cwd
reset, but `python -m sy_tools.server` then cannot import `sy_tools` at all in a real install.
"""
from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import re
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from pydantic import Field, ValidationError

from . import SERVER_NAME, SERVER_VERSION, config, memory, secrets, tracker, usage
from . import preflight as preflight_cache  # aliased: the `preflight` tool below shadows the module name
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
    """Reject an empty or whitespace-only required string argument before any tracker call happens."""
    for name, value in fields.items():
        # Whitespace counts as empty: a title of `"\n"` passes schema validation and would make a
        # permanently blank issue that no search can find again.
        if not value.strip():
            raise ToolError(f"{name!r} is required and must be a non-empty string")


def _scrub_texts(*texts: str) -> tuple[list[str], dict[str, Any]]:
    """Caller-supplied fields with every credential value this process holds replaced by a marker.

    Returns the texts in the order given, plus one report naming the variables it redacted and counting
    occurrences, never disclosing a value. Call it once per write, with every field that write carries:
    a report covering some of a write's fields is read as covering all of them. Call it *before*
    `_validate_machine_log`, on the scrubbed text — the body that gets validated has to be the body
    that gets sent, or a scrub rewriting values inside a machine log ships a body no check ever saw.
    """
    known = secrets.discover_secret_vars(extra_words=config.extra_secret_words())
    absent: list[str] = []
    too_short: list[str] = []
    # Forced in over auto-discovery: `discover_secret_vars`'s name heuristic can miss the credential
    # this repo declares, which is the one value that must never reach a tracker body.
    for declared in config.adapter_map().get("secret_env", []):
        name = str(declared)
        value = os.environ.get(name, "")
        # A declaration overrides the name heuristic, never the length floor: forcing a sub-floor value in
        # redacted every space in a body (measured), and `secrets.sanitize` treats one as absent anyway.
        if len(value) >= secrets.DEFAULT_MIN_LENGTH:
            known[name] = value
        elif value:
            too_short.append(name)
        else:
            # Reported, never raised as `secrets.sanitize`'s `require=` does: a value this environment
            # lacks cannot be in a body composed here, and raising would refuse every tracker write.
            absent.append(name)
    scrubbed: list[str] = []
    totals: Counter[str] = Counter()
    # Variadic because a per-field helper got wired to the body alone: `create-issue`'s title reached the
    # adapter unscrubbed while the report still said `redactions: 1`, which reads as full coverage.
    for text in texts:
        # The in-memory known-value pass only, never `sanitize`'s scanner pass: that shells out over a
        # *file*, and a body is a string composed here that no file exists for.
        clean, counts = secrets.scrub_text(text, known)
        scrubbed.append(clean)
        totals.update(counts)
    return scrubbed, {
        "scrubbed_vars": sorted(totals),  # names only, never a value
        "redactions": sum(totals.values()),
        "declared_absent_from_env": sorted(absent),
        "declared_below_length_floor": sorted(too_short),
    }


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

    Canonical verb `create-issue`. Passing `parent` is also the canonical verb `create-child`: there
    is deliberately no separate tool for a child, because it is this same write.

    A `body` that claims `shipyard.ship_metrics.v1` is validated exactly as a comment's is. Title and
    body are credential-scrubbed, and `scrub` reports the variable names it redacted.
    """
    _required(title=title)
    # The title is scrubbed with the body: every search result echoes it back, and it cannot be edited
    # back out of a tracker's own history.
    (title, body), scrub = _scrub_texts(title, body)
    _validate_machine_log(body)
    created = await tracker.adapter().create_issue(issue_type=issue_type, title=title, body=body, parent=parent)
    return {**created, "scrub": scrub}


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
    existing body, read it with `get-issue` first and send it back as part of `body`. A `body` that
    claims `shipyard.ship_metrics.v1` is validated exactly as a comment's is, and the body is
    credential-scrubbed, with `scrub` reporting the variable names it redacted.
    """
    _required(issue=issue)
    (body,), scrub = _scrub_texts(body)
    _validate_machine_log(body)
    updated = await tracker.adapter().update_issue(issue, body)
    return {**updated, "scrub": scrub}


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
    page_token: Annotated[
        str | None,
        Field(description="The `next_page_token` a previous page returned; omit it to ask for the first page."),
    ] = None,
) -> dict[str, Any]:
    """Search the configured project for issues matching any combination of filters.

    Canonical verb `find-issues`. Every filter is optional and they combine as AND; with none set
    this lists the project's recent issues. One page only: `is_last` says whether more remain, and
    `next_page_token` is the cursor to ask for them where the tracker supports one — send it back as
    `page_token`, unread and unmodified, to get the page after it. A tracker with no cursor reports
    `next_page_token: null`, so there is nothing to send back and `page_token` does nothing.
    """
    return await tracker.adapter().find_issues(
        status=status, issue_type=issue_type, parent=parent, text=text, limit=limit, page_token=page_token
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

    Canonical verb `add-label`. Additive by contract, and the full resulting label set comes back so
    the caller can confirm nothing was displaced. The `label` goes through the same credential scrub
    every body does, reported under `scrub`.
    """
    _required(issue=issue, label=label)
    # Scrubbed like a body: a caller-supplied string that lands in durable tracker state and in the
    # tracker's own error output, so exempting it would rest on a claim about what a caller pastes.
    (label,), scrub = _scrub_texts(label)
    added = await tracker.adapter().add_label(issue, label)
    return {**added, "scrub": scrub}


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
    other body passes through unchanged apart from the credential scrub, whose `scrub` key reports the
    variable names it redacted.
    """
    _required(issue=issue)
    (body,), scrub = _scrub_texts(body)
    _validate_machine_log(body)
    posted = await tracker.adapter().post_comment(issue, body)
    return {**posted, "scrub": scrub}


# Loose on purpose — markers are interchangeable and the counts need not match: looseness can only ever
# *find* a block, and a block found is a block validated, where a block missed reaches the tracker unread.
_FENCE_OPENER = re.compile(r"[ \t]*(?:`{3,}|~{3,})")
"""A line that opens a fenced block: the marker, then anything at all in the info-string position."""

_FENCE_CLOSER = re.compile(r"[ \t]*(?:`{3,}|~{3,})[ \t]*")
"""A line that closes one: the marker alone. Text after the marker leaves the block open."""


def _fenced_contents(body: str) -> list[tuple[int, int]]:
    """Every properly closed fenced block's *content* span, as offsets into `body`."""
    # One forward pass, not the equivalent lazy `(.*?)` reaching for the next closing marker: that
    # re-scans the rest of the body once per opener that never closes, measured at 93 seconds on a 320 KB
    # body holding 40,000 of them, and this runs synchronously inside the `post_comment` coroutine, so one
    # malformed body would stall the event loop for every other tool call in flight.
    spans: list[tuple[int, int]] = []
    # Split on `\n` alone: `str.splitlines` also splits on `\r` and `\f`, which would newly *find* the
    # block in a CRLF body, where nothing closes and staying unfenced text is the refusal that body earns.
    lines = body.split("\n")
    last = len(lines) - 1
    content_start: int | None = None
    offset = 0
    for index, line in enumerate(lines):
        line_start, offset = offset, offset + len(line) + 1
        if content_start is None:
            # Any info string opens a block — a caller that omits the language must not thereby skip
            # validation — and an opener needs a newline after it, so an unterminated last line opens none.
            if index < last and _FENCE_OPENER.match(line):
                content_start = offset
        elif _FENCE_CLOSER.fullmatch(line):
            spans.append((content_start, line_start))
            content_start = None
    return spans


def _outside_fences(body: str, spans: list[tuple[int, int]]) -> str:
    """Whatever is left of the body once every properly closed block's *content* is cut out."""
    kept, cursor = [], 0
    # Cut by span, not by `str.replace`: two identical blocks in one body would remove the wrong copies.
    # The span is the content, so the fence lines stay in — anything in the opening line's info-string
    # position is never parsed, and cutting the whole block swallowed a second machine log parked there,
    # which reached the tracker unread. Left in, it is a stray mention like any other unfenced text.
    for start, end in spans:
        kept.append(body[cursor:start])
        cursor = end
    kept.append(body[cursor:])
    return "".join(kept)


def _as_json(block: str) -> object:
    """One fenced block's parsed JSON, or None when it is not JSON at all (a shell sample, prose)."""
    try:
        return json.loads(block)
    # `json.loads` fails three ways and only one is a `JSONDecodeError` (itself a `ValueError`): an integer
    # literal past `sys.int_info.str_digits_check_threshold` raises a bare `ValueError`, and nesting deeper
    # than the decoder's recursive descent raises `RecursionError`. Catching only the decode error let a
    # body pick which uncaught exception escaped the tool.
    except (ValueError, RecursionError):
        return None


_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
"""A JSON `\\uXXXX` escape. Fixed width, so matching it cannot backtrack and the scan stays linear."""


def _json_unescaped(text: str) -> str:
    """`text` with every `\\uXXXX` JSON escape decoded to the character it names."""
    # Not a JSON string decode — just enough that a literal `SCHEMA_ID` search cannot be evaded by
    # spelling one of its ASCII characters as an escape, which is what `_record`'s parse-based identity
    # settles for content that parses. One replacement per match is exact for that: the id is pure ASCII
    # (no surrogate pair to rejoin) and holds no `\u`, so decoding cannot hide a literal occurrence.
    return _UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)


def _record(parsed: object) -> dict[Any, Any] | None:
    """The `shipyard.ship_metrics.v1` record a parsed block *is*, or None when it is not one."""
    # Identity on the **parsed** value, never the raw text, so it holds however the JSON spelled the
    # string: a JSON `"shipyard.ship_metrics.v\u0031"` decodes to this id and is this record.
    if isinstance(parsed, dict) and parsed.get("schema") == SCHEMA_ID:
        return parsed
    return None


def _claims_within(parsed: object) -> bool:
    """Whether a parsed block carries a `shipyard.ship_metrics.v1` object at any depth inside it."""
    # A record below the top level is not a machine log `_record` can validate, but it is unmistakably a
    # claim, and the only alternative to counting it is posting it unread: the raw-text fallback catches
    # the literal spelling of a nested block, so letting the escaped spelling pass would reopen the
    # identity-versus-text gap one level down.
    stack = [parsed]
    # Iterative, not recursive: `json.loads` accepts nesting deep enough to blow a recursive walk's stack,
    # and a body must not get to choose which exception this check raises.
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
    """Reject a malformed `shipyard.ship_metrics.v1` block before the body it sits in is written.

    Every caller that accepts a body runs this — `post-comment`, `create-issue`, `update-issue` — so an
    issue body is gated identically to a comment's. A body *claims* this schema when it names the id in
    any JSON spelling, or carries a fenced block that parses as this schema however that block spelled
    the id. A body that claims it must carry exactly one fenced JSON object that validates against the
    schema and nothing else that claims it; a second block, or a bare mention outside every block, is
    refused as ambiguous rather than resolved for the caller. A body that claims nothing passes through
    untouched: prose, a code sample, another schema's id.
    """
    # Every JSON spelling of this id either names it outright or escapes part of it with a backslash, so a
    # body holding neither cannot hold a claim and is answered unscanned. Almost every body is this one.
    if SCHEMA_ID not in body and "\\" not in body:
        return
    # Armed by the id, not by finding a valid block: a trailing comma, a CRLF body whose closing fence the
    # pattern cannot see, or a block pasted as prose each leave nothing to validate, and a missing block
    # read as "nothing to check" is how an unvalidated log reached the tracker. Text searches go through
    # `_json_unescaped` because a JSON string can spell the id's last character as an escape, decoding to
    # the id while holding no literal occurrence of it, which walked past every plain substring search.
    named = SCHEMA_ID in _json_unescaped(body)
    spans = _fenced_contents(body)
    records: list[dict[Any, Any]] = []
    unread = 0
    for start, end in spans:
        # The content span, never the whole block: text after the opening marker is never parsed, and an
        # info string holding a second complete machine log posted verbatim beside a valid one.
        # `_outside_fences` cuts this same span, so an info string falls to the stray check.
        content = body[start:end]
        parsed = _as_json(content)
        record = _record(parsed)
        if record is not None:
            records.append(record)
        # A block that claims the id but will not parse is a candidate, never a skip: tallying only parsed
        # matches left a malformed block invisible to this count and to the schema check, so a valid one
        # beside it validated alone. A claim nested below the top level counts too, but is never validated.
        elif SCHEMA_ID in _json_unescaped(content) or _claims_within(parsed):
            unread += 1
    # The other half of arming: a body that never names the id in plain text is still this check's
    # business the moment a block claims this schema, which is the escaped-record case.
    if not named and not records and not unread:
        return
    # An unclosed fence, or a closing marker with trailing text, yields no block to tally at all, so a
    # claim in what is left over is a candidate on its own terms. Narrow enough to stay usable because
    # unfenced text only counts when it names this id: quoting someone else's broken JSON still posts.
    stray = SCHEMA_ID in _json_unescaped(_outside_fences(body, spans))
    blocks = len(records) + unread
    if blocks + stray > 1:
        raise ToolError(
            f"this body claims {SCHEMA_ID} in {blocks + stray} places, so it was refused: "
            f"{blocks} in a properly closed fenced block"
            + (", and once outside any such block" if stray else "")
            + ". Which one is the machine log is ambiguous, and validating one of them would write the "
            "others unchecked. A machine log carries exactly one such block and nothing else: post it "
            "on its own, as its own comment, and when quoting earlier numbers as prose, leave the "
            "literal id out — say `the ship metrics log` instead, because naming the id arms this check."
        )
    if records:
        try:
            ShipMetricsV1.model_validate(records[0])
        except ValidationError as exc:
            raise ToolError(
                f"this body carries a {SCHEMA_ID} block that does not match the schema, so it was "
                f"refused: {exc.error_count()} problem(s): "
                + "; ".join(f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors())
                + ". The field definitions are in skills/ship/references/handoff-accounting.md."
            ) from None
        return
    # Exactly one claim and it is not a record. Zero claims cannot arrive here: a named id sits either in
    # some block's content, which makes that block a claim, or outside them all.
    raise ToolError(
        f"this body claims {SCHEMA_ID} but carries no fenced block that parses as one, so it was "
        "refused. A machine log is a fenced JSON object whose `schema` key is that id: check the JSON "
        "parses (a trailing comma is the usual culprit), that the closing fence is on a line of its own "
        "with nothing after it and Unix line endings, and that the block is fenced at all. If the body "
        "is prose that merely mentions the schema, say `the ship metrics log` instead — the id arms this "
        "check. The "
        "field definitions are in skills/ship/references/handoff-accounting.md."
    )


@mcp.tool(name="preflight")
async def preflight(
    force: Annotated[
        bool,
        Field(
            description="Do the live read even when a recent success is still cached. For a caller "
            "that must see the tracker answer right now, such as a smoke run."
        ),
    ] = False,
) -> dict[str, Any]:
    """Check that the configured tracker's credential and account are usable before relying on them.

    Canonical verb `preflight`. Reports what it confirmed and never echoes a secret value. Run it once
    up front so a credential problem surfaces there instead of as a half-finished workflow.

    A live read is the only thing that tells a present-but-dead credential from a working one and it is
    not free, so a success is cached for `ttl_hours` against the plugin build, the tracker, the resolved
    config and the values of the secret variables the selected adapter declares; any of those changing
    invalidates it by itself, and `force` demands the live read regardless. `cached: false` means the
    tracker was just read and the rest of the result is that read's report; `true` means a read inside
    the window already succeeded and nothing touched the network.
    """
    ttl_hours = preflight_cache.DEFAULT_TTL_HOURS
    try:
        name = str(config.get("tracker"))
        # Resolved here, never passed in, so a caller cannot key the cache on a credential the adapter
        # does not read with.
        var_names = [str(declared) for declared in config.adapter_map().get("secret_env", [])]
        cached = not force and preflight_cache.check(name, var_names, ttl_hours * 3600)
    except config.ConfigError as exc:
        raise ToolError(str(exc)) from None
    if cached:
        return {"tracker": name, "cached": True, "ttl_hours": ttl_hours}
    # Recorded after the read and never before it: an adapter reports a dead credential by raising,
    # so this line is unreachable on failure and a broken credential is never cached as verified.
    confirmed = await tracker.adapter().preflight()
    preflight_cache.record(name, var_names)
    return {**confirmed, "tracker": name, "cached": False, "ttl_hours": ttl_hours}


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
    # Synchronous inside the async tool on purpose: the scrub must strictly precede the upload, and making
    # it awaitable would buy nothing while adding a way to interleave the two.
    report = secrets.sanitize(artifact, require=required, extra_words=config.extra_secret_words())
    evidence = await backend.attach_artifact(issue, artifact)
    return {"attached": True, "skipped": False, "issue": issue, "sanitize": report, "evidence": evidence}


def _gate_skip_reason(kind: str, caller: str, tier: object) -> str | None:
    """Why this attachment must not happen, or None to proceed.

    The rules mirror the adapter attachments reference under `skills/tracker/`.
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

    Canonical verb `attachment-download`. Resolution is by filename with an exactly-one-match rule; pass
    the tracker-native id instead when an issue carries several attachments of the same name. An
    ambiguous or absent match fails rather than picking one.
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
    already matches `path`'s filename is a plain upload, and where more than one existing attachment
    shares that filename what happens is adapter-specific (see the tracker's own `ADAPTER.md`), since the
    trackers offer no common primitive for "replace all of these". It runs
    the same gate and the same two sanitisation passes, in the same order, as `attach-artifact`.

    Destructive: the artifact it replaces is irrecoverable once the replacement lands and there is no
    undo, so confirm the target first.
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
    # Synchronous before the await for the same reason as `attach-artifact`: scrub, then upload.
    report = secrets.sanitize(artifact, require=required, extra_words=config.extra_secret_words())
    evidence = await backend.attachment_update(issue, artifact)
    return {"updated": True, "skipped": False, "issue": issue, "sanitize": report, "evidence": evidence}


@mcp.tool(name="reload_config")
def reload_config() -> dict[str, Any]:
    """Re-read the Shipyard configuration layer chain from disk and replace the server's hot copy.

    Reports whether the resolved values changed; never reports a value.
    """
    return config.reload()


@mcp.tool(name="check_env")
def check_env(
    name: Annotated[
        str,
        Field(
            description="Name of the environment variable to check, e.g. a credential the configured "
            "tracker needs. Only its presence is reported; its value is never returned or logged."
        ),
    ],
) -> dict[str, Any]:
    """Report whether an environment variable is set, without ever returning or logging its value.

    For diagnosing a missing credential without printing one: dumping the environment or echoing a
    variable writes the value into permanent transcript history, so the `PreToolUse` guard in
    `sy_tools/guards/secret_guard.py` denies those commands and names this tool as the safe alternative.
    Neither the result nor any error it raises can carry the value, and a variable exported empty reports
    as unset.
    """
    _required(name=name)
    return {"name": name, "present": config.env_present(name)}


@mcp.tool(name="validate_config")
def validate_config() -> dict[str, Any]:
    """Report every reason the resolved configuration would be rejected.

    Covers schema violations, missing required keys, an unknown tracker, a required credential absent
    from the environment, an environment variable that outranks the resolved per-agent models, a
    retired `SY_*` setting variable still set in the environment, model-floor breaches, and two board
    columns configured under one name. Side-effect-free, and never prints a secret value.
    """
    errors = config.validate()
    # A config with two statuses under one column name validated clean here and then broke on the first
    # `canonical_status` call, which is the one fault this tool exists to name. `column_collisions()`
    # reports that and only that: `column_names()` answers the same question but adds a "missing required
    # column name(s)" line for every key `config.validate()` already reported unset, naming one fault
    # twice on any unconfigured repo. A `ConfigError` from reading those keys is itself a reason no
    # tracker verb can use this config and `config.validate()` never reaches it, so it is reported here —
    # but both calls raise the *same* message, so it is appended only when it is not already there.
    try:
        errors.extend(tracker.column_collisions())
    except config.ConfigError as exc:
        if str(exc) not in errors:
            errors.append(str(exc))
    report: dict[str, Any] = {"valid": not errors, "errors": errors}
    try:
        report["tracker"] = config.get("tracker")
        report["fingerprint"] = config.fingerprint()
    except config.ConfigError:
        pass  # the errors list carries the reason, and reporting a broken config is this tool's contract
    return report


@mcp.tool(name="get_config")
def get_config(
    key: Annotated[
        str,
        Field(description="Dotted config key to read, e.g. `columns.ready`, `worktree.root`, `ci.poll_timeout`."),
    ],
    default: Annotated[
        str | None,
        Field(
            description="Value to return when the key is not configured. Omit it to make an unknown "
            "key an error, which is what a caller that believes the key exists wants."
        ),
    ] = None,
) -> dict[str, Any]:
    """Read one resolved configuration value by dotted key.

    Resolution is the merged layer chain, so this is the only correct way to learn a setting: reading a
    layer file directly misses whatever a higher layer overrode. An unknown key is an error unless
    `default` is given — a key an adapter documents as optional has no entry to resolve, and a caller
    that knows it is optional says so by supplying one. A credential-shaped key is refused outright:
    secrets are never read from a config file, and `check_env` is how to ask about one.
    """
    _required(key=key)
    try:
        value = config.get(key) if default is None else config.get(key, default=default)
    except config.ConfigError as exc:
        raise ToolError(str(exc)) from None
    return {"key": key, "value": value}


@mcp.tool(name="show_config")
def show_config() -> dict[str, Any]:
    """Report every resolved configuration value together with the layer each one came from.

    For seeing the whole resolved config at once — which setting a layer overrode and where it came
    from — rather than one key at a time. Also reports the digest of the resolved values and the layer
    chain on disk with whether each file is present. Refuses to report anything at all when the
    resolved configuration carries a credential-shaped key, naming only the key.
    """
    try:
        return config.show()
    except config.ConfigError as exc:
        raise ToolError(str(exc)) from None


@mcp.tool(name="agent_model")
def agent_model(
    name: Annotated[
        str,
        Field(description="Agent to resolve, as named under `models.agents`, e.g. `gate`, `ship-build`."),
    ],
) -> dict[str, Any]:
    """The model and effort one agent must be dispatched with, after floor clamping.

    Dispatch with what this returns, never with the configured value read raw: a per-agent floor is a
    quality floor rather than a cost dial, so cost-scaling may raise one and never lower it, and the
    report says whether either value was clamped and which layer the request came from. An agent the
    configuration does not name is an error listing the ones it does.
    """
    _required(name=name)
    try:
        return config.agent_binding(name)
    except config.ConfigError as exc:
        raise ToolError(str(exc)) from None


@mcp.tool(name="scratch_dir")
def scratch_dir(
    identifier: Annotated[
        str,
        Field(description="The one identifier to resolve a scratch directory for, e.g. a ticket key."),
    ] = "",
    repo: Annotated[
        bool,
        Field(
            description="Resolve this repository's own scratch directory instead — the same path from "
            "every worktree of it. Mutually exclusive with `identifier`."
        ),
    ] = False,
) -> dict[str, Any]:
    """Resolve the ephemeral working directory for one identifier, or for this repository, creating it.

    Everything a workflow writes that is not part of the repository belongs under here, so nothing
    lands in the consuming checkout. Takes either one identifier or `repo`, not both and not neither.
    An identifier must be a relative name that stays strictly inside the resolved scratch root;
    anything resolving to the root itself or outside it is refused, because two identifiers would then
    collide there and the first caller to clean up what it was handed would delete the other's data.
    """
    if bool(identifier.strip()) == repo:
        raise ToolError("'scratch_dir' takes either one identifier or repo, not both and not neither")
    try:
        directory = config.repo_scratch_dir() if repo else config.scratch_dir(identifier)
    except config.ConfigError as exc:
        raise ToolError(str(exc)) from None
    return {"path": str(directory)}


@mcp.tool(name="fingerprint_config")
def fingerprint_config() -> dict[str, Any]:
    """A stable digest of every resolved configuration value, disclosing none of them.

    Equal digests mean the resolved configuration has not changed, so a caller holding one can tell
    whether an edit landed without re-reading anything. It changes with any resolved value, including
    one derived rather than written, and is not a hash of any single file.
    """
    try:
        return {"fingerprint": config.fingerprint()}
    except config.ConfigError as exc:
        raise ToolError(str(exc)) from None


SessionId = Annotated[
    str,
    Field(
        description="Claude Code session id whose transcript tree to read. Give this or `transcript`, "
        "not both and not neither."
    ),
]
"""Both transcript tools take the same source pair, described once so they describe it the same way."""

TranscriptPath = Annotated[
    str,
    Field(description="Absolute path to a main session `.jsonl` transcript, instead of `session_id`."),
]


def _transcript_source(session_id: str, transcript: str) -> Path:
    """The main transcript the caller named, refusing both-or-neither before any file is read."""
    if bool(session_id.strip()) == bool(transcript.strip()):
        raise ToolError("give either session_id or transcript, not both and not neither")
    try:
        return usage.resolve_main_transcript(session_id.strip() or None, transcript.strip() or None)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ToolError(str(exc)) from None


# This and `export_transcript` stay synchronous: local disk reads bounded by the calling session's own
# transcript tree, with no network in them, so a thread offload would buy only interleaving.
@mcp.tool(name="usage_summarize")
def usage_summarize(
    session_id: SessionId = "",
    transcript: TranscriptPath = "",
    phase: Annotated[
        str,
        Field(description="Workflow phase the roll-up is attributed to, as it appears in the output."),
    ] = "ship",
    task: Annotated[
        str | None,
        Field(description="Issue id to record in the output, when the roll-up belongs to one."),
    ] = None,
    require_agent: Annotated[
        list[str] | None,
        Field(
            description="Agent types that must appear in the roll-up. Naming an agent that dispatched "
            "turns an absent transcript into an error instead of a quietly-low total."
        ),
    ] = None,
    output: Annotated[
        str | None,
        Field(description="Path to also write the summary JSON to. The summary is returned either way."),
    ] = None,
) -> dict[str, Any]:
    """Roll up token usage across one session's main transcript and every subagent transcript under it.

    Reads the on-disk transcript tree, so it also works on a resumed session and counts subagent turns
    the caller never saw. Counts are de-duplicated by message id and grouped by agent type and model,
    small enough to post as a standalone machine-log comment. An agent named in `require_agent` but
    absent from the tree is an error, since a roll-up missing a dispatched agent's transcript
    under-reports rather than fails.
    """
    main = _transcript_source(session_id, transcript)
    result = usage.summarize(main, phase=phase, task=task)
    present = {row["agent_type"] for row in result["by_agent"]}
    missing = sorted(set(require_agent or []) - present)
    if missing:
        raise ToolError("usage roll-up missing expected agent transcript(s): " + ", ".join(missing))
    if output:
        try:
            Path(output).write_text(json.dumps(result, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"summary could not be written to {output}: {exc}") from None
    return result


@mcp.tool(name="export_transcript")
def export_transcript(
    output: Annotated[
        str,
        Field(description="Path to write the rendered transcript to. Required; the text is never returned."),
    ],
    session_id: SessionId = "",
    transcript: TranscriptPath = "",
    task: Annotated[
        str | None,
        Field(description="Issue id to record in the rendered header, when the export belongs to one."),
    ] = None,
) -> dict[str, Any]:
    """Render one session's whole transcript tree as readable text on disk, and report where it landed.

    Replaces the manual `/export` step for the attachment flow: bulky tool output is truncated per
    `transcript.truncation_limits`, raw JSONL noise is dropped, and subagent sections are ordered by
    first timestamp, so the result is audit-readable rather than a machine dump. Run it as late as
    possible so the captured tail is maximal; it reads on-disk transcripts, so it also works on a
    resumed session.

    `output` is mandatory and the rendered text is never part of the result: the transcript is meant to
    be scanned, redacted and attached by path without ever being read back into the caller's context.
    """
    _required(output=output)
    main = _transcript_source(session_id, transcript)
    text = usage.render(main, task=task)
    destination = Path(output)
    try:
        destination.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise ToolError(f"transcript could not be written to {output}: {exc}") from None
    return {"path": str(destination), "bytes": len(text.encode("utf-8")), "lines": text.count("\n")}


MEMORY_REFUSALS = (ValueError, config.ConfigError)
"""What the memory store raises for the caller's own mistake, described once for all three tools.

A rejected field and a `memory.dir` that will not resolve are both answers the caller has to see as a
tool result, so they are surfaced as `ToolError`; anything else (an unwritable root, say) is a real
failure and propagates."""


@mcp.tool(name="memory_add")
def memory_add(
    title: Annotated[
        str,
        Field(
            description="The lesson in one line. Becomes the kebab-slug filename, so re-using a title "
            "already stored replaces that lesson instead of adding a second copy of it."
        ),
    ],
    scope: Annotated[
        str,
        Field(description="Where the lesson applies, e.g. a tool, skill, or workflow area."),
    ],
    body: Annotated[
        str,
        Field(description="The lesson itself: what to do differently next time, and why."),
    ],
    tags: Annotated[
        str,
        Field(description="Comma-separated tags, stored as frontmatter and searchable like the body."),
    ] = "",
) -> dict[str, Any]:
    """Store one durable lesson in cross-session memory and report the file it landed in.

    For a tool/skill-level trap that outlives this repo and this session; repo trivia belongs in that
    repo instead. The store is user-global, so a lesson written here is what an unrelated session in
    another checkout reads back. `path` is the Markdown file holding it — the same path on a re-add
    under an existing title, because the write is idempotent by title rather than append-only. An
    empty title, scope, or body is refused, and so is a title with no letters or digits in it, since
    it would leave the lesson under a nameless file.
    """
    try:
        return {"path": str(memory.add(title, scope, tags, body))}
    except MEMORY_REFUSALS as exc:
        raise ToolError(str(exc)) from None


@mcp.tool(name="memory_search")
def memory_search(
    query: Annotated[
        str,
        Field(
            description="Substring to look for, matched case-insensitively against each lesson's whole "
            "text and its filename. Not a regex and not a word search."
        ),
    ],
) -> dict[str, Any]:
    """Find stored lessons whose text or filename contains a substring.

    Each match is one `path: title` line, so a caller can read the interesting ones by path without
    pulling the whole store into context. `root` is the store the search actually ran against, which
    is what makes an empty `matches` diagnosable: no lesson matched a populated store, rather than the
    resolver having pointed at a root the lessons are not in. An empty query is refused, because it
    matches everything and `memory_list` is the way to ask for that.
    """
    try:
        return {"query": query, "root": str(memory.root()), "matches": memory.search(query)}
    except MEMORY_REFUSALS as exc:
        raise ToolError(str(exc)) from None


@mcp.tool(name="memory_list")
def memory_list() -> dict[str, Any]:
    """Report the whole memory index: every stored lesson with its scope, tags, and date.

    The cheap way to see what memory holds before a search, and what `/sy:plan`, `/sy:spec`, and
    `/sy:ship` read at the start of a task. `index` is the greppable index file's Markdown text, which
    is rebuilt first whenever it disagrees with the lessons on disk, so a lesson deleted by hand is
    absent here rather than a dead link; an empty store reports `(no entries)`. `root` is the store it
    was read from.
    """
    try:
        return {"root": str(memory.root()), "index": memory.index_text()}
    except MEMORY_REFUSALS as exc:
        raise ToolError(str(exc)) from None


if __name__ == "__main__":
    mcp.run("stdio")
