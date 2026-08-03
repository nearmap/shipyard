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

    A body carrying a `shipyard.ship_metrics.v1` block is validated against that schema before
    anything is posted; every other body passes through unchanged.
    """
    _required(issue=issue)
    _validate_machine_log(body)
    return await tracker.adapter().post_comment(issue, body)


FENCE = re.compile(r"^[ \t]*(?:`{3,}|~{3,})[^\n]*\n(.*?)^[ \t]*(?:`{3,}|~{3,})[ \t]*$", re.M | re.S)
"""Each fenced block's inner text, from a body whose fences may be backticks or tildes.

The info string is not matched on: `handoff-accounting.md` shows the metrics block as ```` ```json ````,
but a caller that omits the language must not thereby skip validation — and the check that decides
whether a block is a machine log is the `schema` key inside it, not the fence it arrived in.
"""


def _validate_machine_log(body: str) -> None:
    """Reject a malformed `shipyard.ship_metrics.v1` block before the comment is posted.

    The trigger is the parsed block's own `schema` key, not the `# Claude Code ship metrics` heading
    above it: the heading is prose a caller can vary, while the schema id is the thing every later
    reader keys off. A block that is not JSON, or whose JSON is not an object, or whose `schema` says
    something else, is left entirely alone — this tool posts prose comments too, and a comment
    quoting a code sample must not have to satisfy a metrics schema.

    A `ToolError` rather than a silent pass because the incident this closes off is a metrics comment
    that landed with a field name nobody noticed was wrong and was read as authoritative afterwards.
    """
    if SCHEMA_ID not in body:
        return
    for block in FENCE.findall(body):
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict) or parsed.get("schema") != SCHEMA_ID:
            continue
        try:
            ShipMetricsV1.model_validate(parsed)
        except ValidationError as exc:
            raise ToolError(
                f"this comment carries a {SCHEMA_ID} block that does not match the schema, so it was "
                f"not posted: {exc.error_count()} problem(s): "
                + "; ".join(f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors())
                + ". The field definitions are in skills/ship/references/handoff-accounting.md."
            ) from None


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

    Canonical verb `attachment-update`. Replace-by-filename: every existing attachment named like
    `path` is removed and `path` is uploaded in its place, so calling it where nothing matches is a
    plain upload. It runs the same gate and the same two sanitisation passes, in the same order, as
    `attach-artifact` — a second upload path that skipped them would be exactly the hole that keeping
    both passes inside one tool exists to close.

    Destructive: the attachments it replaces are deleted before the new one is uploaded, and there is
    no undo. Confirm the target first.
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


@mcp.tool(name="attachment-delete")
async def attachment_delete(
    issue: IssueId,
    filename_or_id: Annotated[
        str,
        Field(description="The attachment's filename, or its tracker-native id when duplicate names exist."),
    ],
) -> dict[str, Any]:
    """Delete one artifact from an issue, verified by reading the issue back without it.

    Canonical verb `attachment-delete`. Resolves by filename under the same exactly-one-match rule as
    `attachment-download`, and confirms the removal rather than trusting the delete's own status.
    Destructive and not undoable: an artifact deleted here is gone from the issue's durable record.
    """
    _required(issue=issue, filename_or_id=filename_or_id)
    return await tracker.adapter().attachment_delete(issue, filename_or_id)


@mcp.tool(name="reload_config")
def reload_config() -> dict[str, Any]:
    """Re-read the Shipyard configuration layer chain from disk and replace the server's hot copy.

    Reports whether the resolved values changed; never reports a value.
    """
    return config.reload()


@mcp.tool(name="validate_config")
def validate_config() -> dict[str, Any]:
    """Report every reason the resolved configuration would be rejected.

    Covers schema violations, missing required keys, an unknown tracker, a required credential
    absent from the environment, and model-floor breaches. Side-effect-free, and never prints a
    secret value.
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
