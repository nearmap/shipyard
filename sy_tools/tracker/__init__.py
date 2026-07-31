"""The one place that knows which concrete tracker is selected.

`skills/tracker/CONTRACT.md` states that tracker selection happens in exactly one place. This is
that place for the MCP deployment: every tool in `sy_tools/server.py` asks `adapter()` for a
`TrackerAdapter` and speaks only canonical verbs to it. Nothing above this module names a
tracker, and `sy_tools/tests/test_seam.py` fails the build if anything does.
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
required per-repo setting so two repos on one machine can label the same lifecycle differently. Both
adapters read the same keys, which is why the table lives above them rather than in either one.
"""

TYPE_NAMES = {"epic": "Epic", "task": "Task", "bug": "Bug"}
"""Each canonical issue type and the name both current adapters happen to use for it.

Shared because it is currently the same table twice, not because a tracker is obliged to agree; an
adapter whose native names differ maps its own and ignores this.
"""

TIMEOUT_SECONDS = 30
"""How long any single tracker transport call may take before it is a failure.

The transports are async, so a stalled call no longer wedges the calls queued behind it — but it
still holds a connection open and leaves its own caller waiting forever, and a `gh` blocked on a
credential prompt never returns at all. Every adapter transport passes this to its own timeout
and turns the expiry into a `TrackerError`.
"""


class TrackerError(RuntimeError):
    """A tracker operation failed. Raised instead of exiting: the process serves other calls."""


@runtime_checkable
class TrackerAdapter(Protocol):
    """Every canonical verb of `skills/tracker/CONTRACT.md`, and the only surface core may speak.

    The per-method docstrings are where a reader learns what a verb *means*, independently of either
    tracker — an adapter's own docstring describes how it gets there, not what it owes the caller.
    Issue ids are opaque strings that round-trip untouched; only an adapter may interpret one.

    Three contract verbs have no method of their own, because each is one of these writes carrying
    different content: `create-child` is `create_issue` with `parent` set, `post-log` is a
    `post_comment` carrying only a fenced JSON block, and `link-pr`'s durable half is a
    `post_comment` carrying the PR URL. Keeping them out means one write path per effect.

    The verbs are `async` because both transports are I/O-bound and the server serves calls
    concurrently: a slow upload must not be able to block an unrelated tool call. An adapter whose
    transport is only available synchronously (`gh`) still presents an `async` verb and offloads
    the blocking work to a worker thread, so the seam stays uniform above this module.
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
    ) -> dict:
        """One page of issues in the configured project by canonical status, type, parent and/or free text."""
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

    async def preflight(self) -> dict:
        """Confirm credentials and account identifiers are usable, naming nothing secret."""
        ...


def column_names() -> dict[str, str]:
    """This repo's column name for each canonical status, from resolved config. Fails fast.

    An unset column is a configuration error, not a default to guess: guessing would move an issue
    to a column that happens to exist on someone else's board.
    """
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for canonical, key in STATUS_CONFIG_KEYS.items():
        value = str(config.get(key) or "").strip()
        if value:
            resolved[canonical] = value
        else:
            missing.append(key)
    if missing:
        raise TrackerError(
            "missing required column name(s): " + ", ".join(missing)
            + ". Set them in the repo's .shipyard/config.json; see docs/configuration.md."
        )
    return resolved


def canonical_status(native: str | None) -> str | None:
    """The canonical token for a column name, matched case-insensitively.

    A column this repo does not map passes through unchanged rather than being dropped: a caller
    reading an issue parked in some extra column must see where it actually is.
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
    "column_names",
    "native_status",
    "native_type",
]
