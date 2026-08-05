"""The one place that knows which concrete tracker is selected.

`skills/tracker/CONTRACT.md` states that tracker selection happens in exactly one place; this is that
place for the MCP deployment. Every tool in `sy_tools/server.py` asks `adapter()` for a
`TrackerAdapter` and speaks only canonical verbs to it, and `sy_tools/tests/test_tracker_seam.py`
fails the build if anything above this module names a tracker.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from .. import config

STATUS_CONFIG_KEYS = {
    "backlog": "columns.backlog",
    "ready": "columns.ready",
    "in-progress": "columns.in_progress",
    "in-review": "columns.in_review",
    "done": "columns.done",
}
"""Each canonical lifecycle status and the required config key naming this repo's column for it.

`skills/tracker/CONTRACT.md` is opinionated about the five columns, not their names: the name is a
required per-repo setting, and every adapter reads these same keys.
"""

TYPE_NAMES = {"epic": "Epic", "task": "Task", "bug": "Bug"}
"""Each canonical issue type and the native name every current adapter happens to use for it.

No tracker is obliged to agree: an adapter whose native names differ maps its own and ignores this.
"""

TIMEOUT_SECONDS = 30
"""How long any single tracker transport call may take before it is a failure.

The transports are async, so a stalled call no longer wedges the calls queued behind it — but it still
holds a connection open, and a transport blocked on a credential prompt never returns at all. Every
adapter transport passes this to its own timeout and turns the expiry into a `TrackerError`.
"""


class TrackerError(RuntimeError):
    """A tracker operation failed. Raised instead of exiting: the process serves other calls."""


@runtime_checkable
class TrackerAdapter(Protocol):
    """Every canonical verb of `skills/tracker/CONTRACT.md`, and the only surface core may speak.

    The per-method docstrings are what a verb *means*, independently of any tracker. Issue ids are
    opaque strings that round-trip untouched; only an adapter may interpret one.

    Three contract verbs have no method of their own, each being one of these writes carrying
    different content: `create-child` is `create_issue` with `parent` set, `post-log` is a
    `post_comment` carrying only a fenced JSON block, and `link-pr`'s durable half is a `post_comment`
    carrying the PR URL. Keeping them out means one write path per effect.

    The verbs are `async` because the transports are I/O-bound and the server serves calls
    concurrently: a slow upload must not block an unrelated tool call. An adapter with only a
    synchronous transport still presents an `async` verb and offloads the blocking work to a worker
    thread, so the seam stays uniform above this module.
    """

    name: str

    async def create_issue(self, issue_type: str, title: str, body: str = "", parent: str | None = None) -> dict:
        """Create an issue of canonical `issue_type` with `title` and Markdown `body`, under `parent` if given."""
        ...

    async def get_issue(self, issue: str) -> dict:
        """Read one issue: title, Markdown body, canonical status and type, relations, labels, comments."""
        ...

    async def update_issue(self, issue: str, body: str) -> dict:
        """Replace `issue`'s whole Markdown body with `body`; never appends."""
        ...

    async def find_issues(
        self,
        *,
        status: str | None = None,
        issue_type: str | None = None,
        parent: str | None = None,
        text: str | None = None,
        limit: int = 50,
        page_token: str | None = None,
    ) -> dict:
        """One page of issues in the configured project by canonical status, type, parent and/or free text.

        `page_token` is the opaque cursor a previous page returned as `next_page_token`; `None` asks for
        the first page. A cursor is only ever meaningful to the adapter that minted it, so a caller
        passes one back untouched and interprets nothing. An adapter whose transport exposes no cursor
        accepts `page_token`, ignores it, and reports `next_page_token: None`.
        """
        ...

    async def set_status(self, issue: str, status: str) -> dict:
        """Move `issue` into the column this repo names for canonical `status`, and report the native name."""
        ...

    async def assign(self, issue: str, assignee: str = "@me") -> dict:
        """Assign `issue` and report the resolved account. `@me` is the caller need both adapters serve."""
        ...

    async def link_parent(self, issue: str, parent: str) -> dict:
        """Re-parent the existing issue `issue` under `parent`."""
        ...

    async def add_dependency(self, issue: str, blocked_by: str) -> dict:
        """Record that `issue` is blocked by `blocked_by`, and verify the direction really took."""
        ...

    async def add_label(self, issue: str, label: str) -> dict:
        """Add `label` to `issue`, preserving the labels already on it, and return the resulting set."""
        ...

    async def post_comment(self, issue: str, body: str) -> dict:
        """Post `body` as a Markdown comment on `issue` and return the comment the write created."""
        ...

    async def attach_artifact(self, issue: str, path: Path) -> dict:
        """Upload an already-sanitised artifact to `issue` and return verified response evidence."""
        ...

    async def type_convert(self, issue: str, issue_type: str) -> dict:
        """Change an existing `issue`'s type to canonical `issue_type`, verified by reading it back.

        Changing a type is a write a workflow may quietly refuse, so an adapter that cannot confirm
        the new type fails rather than reporting the conversion it asked for.
        """
        ...

    async def attachment_download(self, issue: str, filename_or_id: str, output_path: Path) -> dict:
        """Write the one attachment on `issue` matching `filename_or_id` to `output_path`.

        Exactly one attachment must match, by filename or by the adapter's own attachment id; none, or
        several sharing a filename, is a `TrackerError` naming the candidates rather than a guess at
        which upload the caller meant. Returns the byte count and the destination, so a caller can tell
        an empty artifact from one that never arrived.
        """
        ...

    async def attachment_update(self, issue: str, path: Path) -> dict:
        """Replace the attachment(s) on `issue` named `path.name` with `path`; zero existing is fine.

        Takes no id and resolves purely by filename — a corrective overwrite, since the caller
        regenerating a transcript knows the name it writes and not the id the tracker minted (see
        `../../skills/tracker/CONTRACT.md`, "Attachment lifecycle has no delete"). How many namesakes
        one call replaces is adapter-specific, documented in each `ADAPTER.md`; the returned evidence
        says how many were, so a caller can see whether it superseded anything.
        """
        ...

    async def preflight(self) -> dict:
        """Confirm credentials and account identifiers are usable, naming nothing secret."""
        ...


def column_names() -> dict[str, str]:
    """This repo's column name for each canonical status, from resolved config. Fails fast.

    An unset column is a configuration error, not a default to guess: guessing would move an issue to
    a column that happens to exist on someone else's board. Two statuses sharing one column name,
    ignoring case, is refused too.
    """
    resolved, missing = _configured_columns()
    if missing:
        raise TrackerError(
            "missing required column name(s): " + ", ".join(missing)
            + ". Set them in the repo's .shipyard/config.json; see docs/configuration.md."
        )
    collisions = _collision_messages(resolved)
    if collisions:
        raise TrackerError(collisions[0])
    return resolved


def column_collisions() -> list[str]:
    """Every column-name collision among the columns that *are* configured, reported not raised.

    An unset column is not an error here: `config.validate()`'s own REQUIRED_PATHS check already
    reports it, and calling `column_names()` for this answer named that one fault twice. A `ConfigError`
    from `config.get` propagates, so a config no tracker verb can use cannot report `valid: true`.
    """
    resolved, _ = _configured_columns()
    return _collision_messages(resolved)


def _configured_columns() -> tuple[dict[str, str], list[str]]:
    """Each canonical status's configured column name, plus the config keys left unset or blank."""
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for canonical, key in STATUS_CONFIG_KEYS.items():
        value = str(config.get(key) or "").strip()
        if value:
            resolved[canonical] = value
        else:
            missing.append(key)
    return resolved, missing


def _collision_messages(resolved: dict[str, str]) -> list[str]:
    """One message naming every column name more than one canonical status claims, or nothing.

    Case-insensitive, because that is how `canonical_status` compares a column name.
    """
    sharing: dict[str, tuple[str, list[str]]] = {}
    for canonical, name in resolved.items():
        sharing.setdefault(name.lower(), (name, []))[1].append(STATUS_CONFIG_KEYS[canonical])
    collisions = sorted((name, keys) for name, keys in sharing.values() if len(keys) > 1)
    if not collisions:
        return []
    return [
        "column name(s) shared by more than one canonical status: "
        + "; ".join(f"{', '.join(sorted(keys))} all name {name!r}" for name, keys in collisions)
        + ". Each status needs its own column, or an issue in that column reports as only one of "
        "them and the others become unreachable. Names are compared ignoring case."
    ]


def canonical_status(native: str | None) -> str | None:
    """The canonical token for a column name, matched case-insensitively.

    A column this repo does not map passes through unchanged, so an issue parked in some extra column
    still reports where it actually is.
    """
    if not native:
        return native
    target = native.strip().lower()
    for canonical, name in column_names().items():
        if name.strip().lower() == target:
            return canonical
    return native


def canonical_type(native: str | None) -> str | None:
    """The canonical token for a native type name, matched case-insensitively; unmapped passes through."""
    if not native:
        return native
    target = native.strip().lower()
    for canonical, name in TYPE_NAMES.items():
        if name.strip().lower() == target:
            return canonical
    return native


def native_status(canonical: str) -> str:
    """The column name this repo uses for one canonical status. Rejects an unknown token."""
    names = column_names()
    if canonical not in names:
        raise TrackerError(f"unknown canonical status {canonical!r}; expected one of {sorted(names)}")
    return names[canonical]


def native_type(canonical: str) -> str:
    """The native type name for one canonical type. Rejects an unknown token."""
    if canonical not in TYPE_NAMES:
        raise TrackerError(f"unknown canonical type {canonical!r}; expected one of {sorted(TYPE_NAMES)}")
    return TYPE_NAMES[canonical]


def adapter() -> TrackerAdapter:
    """The adapter for the configured `tracker` key. The single selection point."""
    name = str(config.get("tracker"))
    if name == "jira":
        from .jira.adapter import JiraAdapter

        return JiraAdapter()
    if name == "github":
        from .github.adapter import GithubAdapter

        return GithubAdapter()
    raise TrackerError(f"no MCP adapter for tracker {name!r}; known adapters: github, jira")


__all__ = [
    "STATUS_CONFIG_KEYS",
    "TIMEOUT_SECONDS",
    "TYPE_NAMES",
    "TrackerAdapter",
    "TrackerError",
    "adapter",
    "canonical_status",
    "canonical_type",
    "column_collisions",
    "column_names",
    "native_status",
    "native_type",
]
