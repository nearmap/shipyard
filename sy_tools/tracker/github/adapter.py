"""GitHub tracker adapter, spoken to only through `sy_tools.tracker.adapter()`.

Ports `skills/tracker/github/gh_project.py`'s `gh` transport rather than importing it: the CLI
deployment stays byte-identical, and this copy differs in two ways the server requires. It never
writes to stdout (that stream carries JSON-RPC frames, so one stray line desynchronises the
client), and a failure raises `TrackerError` instead of `SystemExit`, because this process has
other calls to serve after a bad one.

Issue `Type` and `Status` are Projects v2 single-select fields, so the board-resolution logic in
`skills/tracker/github/gh_project.py` is ported here too, one behaviour at a time: resolve, look
the option up case-insensitively, and on a miss re-resolve once before failing, so a column added
minutes ago works without a restart. Two differences from that helper are deliberate. The disk
cache becomes a per-instance dict, and the `item-edit` response is not parsed, because a
human-readable success line must not turn a completed write into a failure.

`attach-artifact` is the deliberate asymmetry `skills/tracker/github/ADAPTER.md` documents: this
tracker has no CLI-scriptable file attachment, so an artifact becomes a secret gist that a
comment on the work item links to. Privacy is verified by reading the created gist back, not
assumed from the flags passed: a public gist would publish a transcript irrevocably.

Credentials are `gh`'s own business. Nothing here reads, passes, or echoes a token, and every
message built from command output is scrubbed of any credential this process holds.

The canonical verbs are `async` because the seam above this module is uniformly async: the server
serves calls concurrently, and a slow attachment must not block an unrelated tool call. `gh`
offers no async transport, so the synchronous transport below is kept verbatim — `_sync_*` bodies
calling `subprocess.run` — and each verb offloads it to a worker thread. The `subprocess` timeout
still bounds that thread: a thread blocked forever is a leaked thread, not a served call.
"""
from __future__ import annotations

import functools
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Any

from anyio import to_thread

from ... import config
from ...secrets import discover_secret_vars, scrub_text
from .. import (
    TIMEOUT_SECONDS,
    TrackerError,
    canonical_status,
    canonical_type,
    native_status,
    native_type,
)

STDERR_LIMIT = 500
STATUS_FIELD = "Status"
TYPE_FIELD = "Type"
ITEM_LIMIT = "10000"
ISSUE_FIELDS = "number,title,body,url,labels,parent,subIssues,blockedBy,comments"
SUMMARY_FIELDS = "number,title,url,labels,parent"

VERIFY_ATTEMPTS = 4
VERIFY_BACKOFF_SECONDS = 0.75
"""How hard a board write is re-read before it is called a failure.

The board's item list is eventually consistent, so the first read after a write can legitimately
miss the card entirely. The bound matters as much as the retry: an unset field has to stay a
failure rather than becoming a hang."""


class GithubAdapter:
    """Canonical tracker verbs, mapped onto the `gh` CLI.

    An issue is identified by its URL in every returned `id`: `gh` accepts a URL wherever it
    accepts a number, and `project item-add --url` accepts nothing else, so the URL is the one
    reference that works for every call this adapter makes. The GraphQL node id `gh` reports as
    `id` is deliberately not used — no `gh issue` command accepts it.

    `tracker.adapter()` builds a fresh adapter per tool call, so the resolved-board cache below
    lives for one call: long enough to stop `create_issue` resolving the same board twice, short
    enough that no id survives a board edit between calls.
    """

    name = "github"

    def __init__(self) -> None:
        self._boards: dict[str, dict[str, Any]] = {}

    async def create_issue(self, issue_type: str, title: str, body: str = "", parent: str | None = None) -> dict:
        """Create an issue, set its board `Type` to `issue_type`, and link `parent` when given."""
        return await to_thread.run_sync(self._sync_create_issue, issue_type, title, body, parent)

    async def get_issue(self, issue: str) -> dict:
        """Read one issue: its Markdown body, board status and type, relations, labels and comments."""
        return await to_thread.run_sync(self._sync_get_issue, issue)

    async def update_issue(self, issue: str, body: str) -> dict:
        """Replace `issue`'s Markdown description with `body`."""
        return await to_thread.run_sync(self._sync_update_issue, issue, body)

    async def find_issues(
        self,
        *,
        status: str | None = None,
        issue_type: str | None = None,
        parent: str | None = None,
        text: str | None = None,
        limit: int = 50,
    ) -> dict:
        """Search issues, optionally by canonical status, canonical type, parent or free text.

        `gh` has no cursor, so `next_page_token` is always None rather than a cursor that cannot be
        resumed. With no status or type filter, this fetches up to `limit` issues from `gh issue list`
        and reports `is_last` from what came back. A status or type filter names a board value, so the
        board becomes the candidate set: every matching card is enumerated, `limit` bounds the page
        rather than the fetch, `is_last` says whether a further match exists, and `text` is matched
        here against title and body instead of by GitHub's server-side search.
        """
        return await to_thread.run_sync(
            functools.partial(
                self._sync_find_issues,
                status=status,
                issue_type=issue_type,
                parent=parent,
                text=text,
                limit=limit,
            )
        )

    async def set_status(self, issue: str, status: str) -> dict:
        """Move `issue` to the board column this repo names for the canonical `status`."""
        return await to_thread.run_sync(self._sync_set_status, issue, status)

    async def assign(self, issue: str, assignee: str = "@me") -> dict:
        """Assign `issue` to the authenticated account. Only `@me` is supported."""
        return await to_thread.run_sync(self._sync_assign, issue, assignee)

    async def link_parent(self, issue: str, parent: str) -> dict:
        """Make `parent` the parent issue of `issue`, using GitHub's native sub-issue relation."""
        return await to_thread.run_sync(self._sync_link_parent, issue, parent)

    async def add_dependency(self, issue: str, blocked_by: str) -> dict:
        """Record that `issue` is blocked by `blocked_by`, verified by re-reading the relation."""
        return await to_thread.run_sync(self._sync_add_dependency, issue, blocked_by)

    async def add_label(self, issue: str, label: str) -> dict:
        """Add `label` to `issue` and return the full label set the re-read reports."""
        return await to_thread.run_sync(self._sync_add_label, issue, label)

    async def post_comment(self, issue: str, body: str) -> dict:
        """Post a Markdown comment on `issue` and return the comment the write created."""
        return await to_thread.run_sync(self._sync_post_comment, issue, body)

    async def attach_artifact(self, issue: str, path: Path) -> dict:
        """Upload `path` as a secret gist and link it from a comment on `issue`, off the event loop."""
        return await to_thread.run_sync(self._sync_attach_artifact, issue, path)

    async def preflight(self) -> dict:
        """Confirm `gh` is installed and authenticated, off the event loop."""
        return await to_thread.run_sync(self._sync_preflight)

    def _sync_create_issue(self, issue_type: str, title: str, body: str, parent: str | None) -> dict:
        """Create the issue, then put it on the board with its `Type` set.

        The type is mapped before the write: an unknown canonical token must not leave an issue
        created with no type on it.
        """
        option = native_type(issue_type)
        url = _gh(["issue", "create", *_repo_args(), "--title", title, "--body", body])
        if not url.startswith("https://"):
            raise TrackerError(f"gh issue create returned no issue URL for {title!r}; nothing was created.")
        self._set_field(url, TYPE_FIELD, option)
        if parent:
            _edit(url, "--parent", parent)
        return {"id": url, "url": url, "type": issue_type, "title": title, "parent": parent}

    def _sync_get_issue(self, issue: str) -> dict:
        """Read `issue` from `gh`, with status and type taken from the board.

        `gh issue view --json` exposes no project single-select value, so the board item is read
        separately — the same source `skills/tracker/github/gh_project.py get` reads.
        """
        data = _view(issue, ISSUE_FIELDS)
        url = str(data.get("url") or "")
        if not url:
            raise TrackerError(f"gh issue view {issue} returned no issue; treat the read as failed.")
        owner, number = _project_ref()
        return {
            **_summary(data, _item_index(owner, number).get(url, {})),
            "body": str(data.get("body") or ""),
            "children": _refs(data.get("subIssues")),
            "dependencies": _refs(data.get("blockedBy")),
            "comments": _comments(data),
        }

    def _sync_update_issue(self, issue: str, body: str) -> dict:
        """Replace the description, taking the URL `gh` prints as the confirmation of the write."""
        url = _edit(issue, "--body", body)
        return {"id": url, "updated": True, "url": url}

    def _sync_find_issues(
        self,
        *,
        status: str | None,
        issue_type: str | None,
        parent: str | None,
        text: str | None,
        limit: int,
    ) -> dict:
        """List issues and filter them on the fields `gh` cannot filter on.

        `--state all` is deliberate: a done issue is still an issue a caller may be searching for,
        and `gh issue list` would otherwise hide every closed one. Status, type and parent are
        filtered here because they are board values and a sub-issue relation, not list flags.

        With no board filter this is one `gh issue list` page: `--limit limit` is the page, `--search`
        is GitHub's, and `is_last` is read from whether that page came back full.

        A status or type filter names a board value, so the board — not the repository — is the
        candidate set, and `_board_page` enumerates it board-first. Two different truncations made
        the previous repo-wide read dishonest here. `--limit limit` on `issue list` asks for the
        newest `limit` issues and filters after, so the one `ready` issue older than that window came
        back as `count: 0` — a caller reading "nothing to pick up" from a board that has work on it.
        Widening that list to `ITEM_LIMIT` then bounded the call by the repository's size instead of
        the board's, which both costs about a second per hundred rows (a few thousand all-state issues
        and the call alone exceeds `TIMEOUT_SECONDS`) and, with `--search`, routes through the Search
        API, whose silent 1,000-row cap is invisible in `--json` output: board items beyond it were
        dropped from the candidate set while `is_last` still reported the result complete.
        """
        if limit <= 0:
            raise TrackerError(f"limit must be a positive number of issues, got {limit}")
        if status is not None or issue_type is not None:
            return _board_page(status=status, issue_type=issue_type, parent=parent, text=text, limit=limit)
        args = [
            "issue", "list", *_repo_args(), "--state", "all", "--limit", str(limit), "--json", SUMMARY_FIELDS
        ]
        if text:
            args += ["--search", text]
        rows = [row for row in _as_list(_gh_data(args), "issues") if isinstance(row, dict)]
        owner, number = _project_ref()
        index = _item_index(owner, number)
        matched = [
            item
            for item in (_summary(row, index.get(str(row.get("url") or ""), {})) for row in rows)
            if parent is None or _same_ref(item["parent"], parent)
        ]
        page = matched[:limit]
        return {
            "issues": page,
            "count": len(page),
            "is_last": len(rows) < limit,
            "next_page_token": None,
        }

    def _sync_set_status(self, issue: str, status: str) -> dict:
        """Set the board `Status` field, reporting both the canonical token and the column name."""
        option = native_status(status)
        url = _url_of(issue)
        self._set_field(url, STATUS_FIELD, option)
        return {"id": url, "status": status, "native": option}

    def _sync_assign(self, issue: str, assignee: str) -> dict:
        """Self-assign, reporting the account the write actually landed on.

        Any other assignee is refused rather than silently redirected to `@me`. The returned
        `assignee` is read back rather than echoing the request: `@me` names an intent, and only the
        resolved account evidences which identity now owns the issue — which is also what the other
        adapter reports, so one caller-visible shape covers both.

        `@me` is resolved to a login before the write, because that is the only thing the read-back
        can be checked against: `--add-assignee` on an already-assigned issue is a no-op that exits
        zero, so a non-empty assignee list proves someone owns the issue, not that this account does.
        """
        if assignee != "@me":
            raise TrackerError(
                f"only self-assignment is supported by this adapter; got {assignee!r}. Pass '@me', or "
                "assign someone else with `gh issue edit --add-assignee`."
            )
        target = str(_gh_json(["api", "user"]).get("login") or "")
        if not target:
            raise TrackerError(
                "gh reported no login for the authenticated account, so an assignment to '@me' could not "
                "be confirmed against one; nothing was assigned."
            )
        url = _edit(issue, "--add-assignee", assignee)
        logins = [
            login
            for entry in _as_list(_view(url, "assignees").get("assignees"), "nodes")
            if isinstance(entry, dict) and (login := str(entry.get("login") or ""))
        ]
        if not any(login.strip().lower() == target.strip().lower() for login in logins):
            raise TrackerError(
                f"{url} does not read back as assigned to {target}; the assignment is unconfirmed "
                f"(assignees: {logins or 'none'})."
            )
        return {"id": url, "assignee": target}

    def _sync_link_parent(self, issue: str, parent: str) -> dict:
        """Set the parent issue, GitHub's native sub-issue relation."""
        return {"id": _edit(issue, "--parent", parent), "parent": parent}

    def _sync_add_dependency(self, issue: str, blocked_by: str) -> dict:
        """Add the `blocked by` relation and prove it took by reading the relation back."""
        url = _edit(issue, "--add-blocked-by", blocked_by)
        found = _refs(_view(url, "blockedBy").get("blockedBy"))
        if not any(_same_ref(ref, blocked_by) for ref in found):
            raise TrackerError(
                f"{issue} does not read back as blocked by {blocked_by}; the relation was not recorded "
                f"(blocked by: {found or 'nothing'})."
            )
        return {"id": url, "blocked_by": blocked_by, "verified": True}

    def _sync_add_label(self, issue: str, label: str) -> dict:
        """Add one label and return every label the re-read reports, so nothing looks dropped."""
        url = _edit(issue, "--add-label", label)
        labels = _labels(_view(url, "labels"))
        if not any(name.strip().lower() == label.strip().lower() for name in labels):
            raise TrackerError(
                f"{label!r} is not on {issue} after the write; labels read back as {labels or 'none'}. "
                "A label that does not exist on the repository is rejected rather than created."
            )
        return {"id": url, "labels": labels}

    def _sync_post_comment(self, issue: str, body: str) -> dict:
        """Post a comment, taking its id from the URL the write printed."""
        url = _gh(["issue", "comment", _checked_ref(issue), *_repo_args(), "--body", body])
        issue_url, _, fragment = url.partition("#issuecomment-")
        if not fragment:
            raise TrackerError(f"commenting on {issue} returned no comment URL, so the comment is unconfirmed.")
        return {"id": issue_url, "comment_id": fragment, "url": url}

    def _set_field(self, issue_url: str, field_name: str, option_name: str) -> str:
        """Ensure the issue is a board item, set one single-select field, and read the value back.

        Ported from `gh_project.set_field`. The refresh retry is the load-bearing part: an option
        added to the board after this board was resolved must work without restarting the server,
        so a lookup miss re-resolves once before it is treated as a real miss.
        """
        owner, number = _project_ref()
        resolved = self._resolve(owner, number, refresh=False)
        ids = _option_id(resolved, field_name, option_name)
        if ids is None:
            resolved = self._resolve(owner, number, refresh=True)
            ids = _option_id(resolved, field_name, option_name)
        if ids is None:
            available = sorted((resolved["fields"].get(field_name) or {}).get("options", {}))
            raise TrackerError(
                f"project {owner}/{number} field {field_name!r} has no option matching {option_name!r} "
                f"(case-insensitive); available: {available}. Fix the board option or the columns.* config "
                f"key. See docs/github-setup.md."
            )
        field_id, option_id = ids
        item_id = self._find_or_add_item(owner, number, issue_url)
        _gh([
            "project", "item-edit",
            "--id", item_id,
            "--project-id", resolved["project_id"],
            "--field-id", field_id,
            "--single-select-option-id", option_id,
        ])
        _verify_field(owner, number, issue_url, field_name, option_name)
        return item_id

    def _resolve(self, owner: str, number: str, *, refresh: bool) -> dict[str, Any]:
        """The board's node id and its single-select fields, cached for the life of this adapter.

        Only fields carrying options are kept: everything else on the board is a text, number or
        date field this adapter never writes. The fields are asked for at `gh`'s maximum rather than
        its 30-row default, because a board wide enough to push `Status` past the thirtieth field
        would otherwise resolve as a board that has no `Status` at all.
        """
        key = f"{owner}/{number}"
        if not refresh and key in self._boards:
            return self._boards[key]
        project = _gh_json(["project", "view", number, "--owner", owner, "--format", "json"])
        project_id = project.get("id")
        if not project_id:
            raise TrackerError(
                f"gh project view {number} --owner {owner} reported no project id; check "
                "tracker_config.project against `gh project list`."
            )
        fields = _gh_data([
            "project", "field-list", number, "--owner", owner, "--format", "json", "--limit", ITEM_LIMIT
        ])
        resolved: dict[str, Any] = {"project_id": str(project_id), "fields": {}}
        for field in _as_list(fields, "fields"):
            if isinstance(field, dict) and field.get("options") is not None and field.get("name"):
                resolved["fields"][str(field["name"])] = {
                    "id": str(field.get("id") or ""),
                    "options": {
                        str(opt["name"]): str(opt["id"])
                        for opt in field["options"]
                        if isinstance(opt, dict) and opt.get("name") and opt.get("id")
                    },
                }
        self._boards[key] = resolved
        return resolved

    def _find_or_add_item(self, owner: str, number: str, issue_url: str) -> str:
        """The board item for `issue_url`, added to the board only if it is not already on it."""
        for item in _raw_items(owner, number):
            if (item.get("content") or {}).get("url") == issue_url:
                return str(item.get("id") or "")
        added = _gh_json(["project", "item-add", number, "--owner", owner, "--url", issue_url, "--format", "json"])
        item_id = added.get("id")
        if not item_id:
            raise TrackerError(
                f"adding {issue_url} to project {owner}/{number} returned no item id, so no field was set."
            )
        return str(item_id)

    def _sync_attach_artifact(self, issue: str, path: Path) -> dict:
        """Upload `path` as a secret gist and link it from a comment on `issue`.

        Returns the transport's own evidence: the gist URL it printed, the re-read confirmation
        that the gist is not public, and the URL of the comment that carries the link. Any step
        that produces no output, or exits non-zero, is a failure rather than a warning.
        """
        issue = _checked_ref(issue)
        if not path.is_file():
            raise TrackerError(f"artifact not found: {path}")

        gist_url = _gh(["gist", "create", "--desc", f"shipyard artifact {issue}", str(path)])
        if not gist_url.startswith("https://"):
            raise TrackerError(
                f"gist creation returned no usable URL for {path.name}; nothing was attached to {issue}."
            )
        gist_id = gist_url.rstrip("/").rsplit("/", 1)[-1]
        if _gh_json(["api", f"gists/{gist_id}"]).get("public") is not False:
            raise TrackerError(
                f"{gist_url} is public or its visibility could not be confirmed; refusing to link it "
                f"from {issue}. Delete it: gh gist delete {gist_id}"
            )

        body = f"Shipyard artifact `{path.name}`: {gist_url}\n\nSecret gist — reachable only from this link."
        comment_url = _gh(["issue", "comment", issue, *_repo_args(), "--body", body])
        if not comment_url.startswith("https://"):
            raise TrackerError(
                f"the artifact was uploaded to {gist_url} but commenting on {issue} returned no comment "
                "URL, so the link is not discoverable from the work item."
            )
        return {
            "artifact": path.name,
            "gist_url": gist_url,
            "gist_public": False,
            "comment_url": comment_url,
        }

    def _sync_preflight(self) -> dict:
        """Confirm `gh` is installed, authenticated, and able to reach the board.

        The `project` scope is checked, not just named in the failure text: every `set_status` and
        every `Type` write goes through Projects v2, so a `repo`-only token passes an authentication
        check and then dies on the first board write — the half-finished workflow this call exists to
        prevent.

        Only a classic or OAuth token has scopes to check, though: `gh auth status` prints no
        `Token scopes:` line at all for a fine-grained PAT or an App token, which is the token type
        GitHub now recommends. An absent line therefore cannot mean "unscoped" — that would fail a
        working configuration — so capability is confirmed positively instead, by reading the board,
        and `scopes` comes back as None rather than as an empty or invented list.
        """
        version = _gh(["--version"]).splitlines()
        try:
            status = _gh(["auth", "status"])
        except TrackerError as exc:
            raise TrackerError(f"{exc} Authenticate with `gh auth login` (scopes: project, read:project).") from None
        account = re.search(r"account (\S+)", status)
        line = re.search(r"Token scopes:(.*)", status)
        scopes: list[str] | None = None
        if line:
            scopes = sorted(re.findall(r"'([^']+)'", line.group(1)))
            if "project" not in scopes:
                raise TrackerError(
                    f"the gh token is missing the 'project' scope, so every board write would fail; it has "
                    f"{scopes or 'no scopes'}. Grant it with `gh auth refresh -s project,read:project`."
                )
        else:
            _confirm_board_access()
        return {
            "tool": "gh",
            "version": version[0] if version else "unknown",
            "authenticated": True,
            "account": account.group(1) if account else None,
            "scopes": scopes,
        }


def _checked_ref(issue: str) -> str:
    """`issue` if it is a reference `gh` reads as an issue, refusing anything it could read as a flag.

    An id crosses the tool boundary as an opaque string and lands in `gh`'s argv as a positional, so
    without this an id shaped like `-Rowner/repo` is a flag: with `tracker_config.repo` unset there is
    no `--repo` ahead of it to lose the race, and the write retargets to a repo the caller named.
    The accepted shapes are the ones this adapter itself produces and resolves: a URL, or a number.
    """
    ref = issue.strip()
    if not re.fullmatch(r"https://\S+|#?\d+", ref):
        raise TrackerError(
            f"{issue!r} is not an issue reference this adapter accepts; pass the issue number, #number, "
            "or its https:// URL. Anything else is refused rather than handed to gh as an argument."
        )
    return ref


def _edit(issue: str, flag: str, value: str) -> str:
    """One `gh issue edit` write, returning the issue URL it printed as proof the write landed."""
    url = _gh(["issue", "edit", _checked_ref(issue), *_repo_args(), flag, value])
    if not url.startswith("https://"):
        raise TrackerError(
            f"gh issue edit {flag} on {issue} printed no issue URL, so the write is unconfirmed: "
            f"{_safe(url) or 'no output'}"
        )
    return url


def _view(issue: str, fields: str) -> dict[str, Any]:
    """One `gh issue view --json` read of the named fields."""
    return _gh_json(["issue", "view", _checked_ref(issue), *_repo_args(), "--json", fields])


def _url_of(issue: str) -> str:
    """`issue` as a URL, resolving a number through `gh`: board items are keyed by content URL."""
    if issue.startswith("https://"):
        return issue
    url = str(_view(issue, "url").get("url") or "")
    if not url:
        raise TrackerError(f"gh issue view {issue} reported no URL, so the board item cannot be identified.")
    return url


def _board_page(
    *, status: str | None, issue_type: str | None, parent: str | None, text: str | None, limit: int
) -> dict:
    """One page of the board items matching a status or type filter, read board-first.

    The board item list is the candidate set: it is a project-item read bounded by the board's own
    size, it carries `Status` and `Type`, and it never touches `gh issue list` or the Search API. Only
    the surviving cards are then read individually for the fields a card does not carry — the labels
    and parent `_summary` reports, plus the body a `text` filter needs — so the per-issue cost scales
    with the filtered board, not with the repository.

    Reading stops one match past the page: `is_last` needs to know only whether a further match
    exists, and each further candidate costs a `gh issue view` a caller would never see.

    `text` is therefore matched here, case-insensitively, as a substring of title or body. That is a
    deliberate divergence from `gh issue list --search`: no attempt is made to reproduce GitHub's
    server-side ranking or query syntax, in exchange for a result set that is complete for the board
    rather than silently capped at the Search API's thousandth row.
    """
    owner, number = _project_ref()
    repo = str(config.get("tracker_config.repo", default="") or "")
    candidates = [
        (url, item)
        for url, item in _item_index(owner, number).items()
        if (status is None or item["status"] == status)
        and (issue_type is None or item["type"] == issue_type)
        and (not repo or _repo_slug(url) == repo)
    ]
    fields = f"{SUMMARY_FIELDS},body" if text else SUMMARY_FIELDS
    needle = (text or "").strip().lower()
    matched: list[dict[str, Any]] = []
    for url, item in candidates:
        if len(matched) > limit:
            break
        data = _view(url, fields)
        summary = _summary(data, item)
        if parent is not None and not _same_ref(summary["parent"], parent):
            continue
        if needle and needle not in f"{data.get('title') or ''}\n{data.get('body') or ''}".lower():
            continue
        matched.append(summary)
    page = matched[:limit]
    return {"issues": page, "count": len(page), "is_last": len(matched) <= limit, "next_page_token": None}


def _repo_slug(url: str) -> str:
    """The `owner/repo` an issue URL belongs to, taken from the path so a GHES host works too.

    A board may span repositories while this search is repo-scoped, so another repo's card is skipped
    before it is read rather than after `gh` refuses it: dropping a candidate because a read failed is
    how a truncated result comes back looking complete.
    """
    parts = url.rstrip("/").split("/")
    return "/".join(parts[-4:-2]) if len(parts) >= 4 else ""


def _summary(data: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    """The fields `get_issue` and `find_issues` both report, canonicalised once for both.

    Shared rather than duplicated because the two verbs must agree key for key: a caller that
    filters a search result and then reads the issue must not meet the same status spelled twice.
    """
    url = str(data.get("url") or "")
    return {
        "id": url,
        "title": str(data.get("title") or ""),
        "status": item.get("status"),
        "type": item.get("type"),
        "parent": _ref(data.get("parent")),
        "labels": _labels(data),
        "url": url,
    }


def _comments(data: dict[str, Any]) -> list[dict[str, str]]:
    """`gh`'s comment list, reduced to the four fields the canonical shape carries."""
    return [
        {
            "id": str(comment.get("id") or ""),
            "author": str((comment.get("author") or {}).get("login") or ""),
            "created": str(comment.get("createdAt") or ""),
            "body": str(comment.get("body") or ""),
        }
        for comment in _as_list(data.get("comments"), "comments")
        if isinstance(comment, dict)
    ]


def _labels(data: dict[str, Any]) -> list[str]:
    """Label names only: the ids and colours `gh` also returns are noise to every caller."""
    labels = _as_list(data.get("labels"), "labels")
    return [str(label["name"]) for label in labels if isinstance(label, dict) and label.get("name")]


def _ref(node: object) -> str | None:
    """One related issue as a reference `gh` accepts back — its URL, or its number if that is all there is."""
    if not isinstance(node, dict):
        return None
    return str(node.get("url") or node.get("number") or "") or None


def _refs(payload: object) -> list[str]:
    """Every related issue in a `{nodes: [...]}` relation, tolerating a bare list.

    Tolerant of both wrappers because `subIssues` and `blockedBy` are recent `gh` fields whose shape
    has changed once already, and of an absent relation, which honestly means no related issues.

    A relation that is present but not a list is a failure, not an empty one: `dependencies` is what a
    caller reads to decide whether an issue is blocked, and a shape this cannot parse must not come
    back as "nothing is blocking it" — that reads identically to a genuinely unblocked issue.
    """
    if payload is None:
        return []
    nodes = payload.get("nodes", []) if isinstance(payload, dict) else payload
    if not isinstance(nodes, list):
        raise TrackerError(
            f"a related-issue relation read back as {type(nodes).__name__}, not a list of issues, so the "
            "relations on this issue are unknown; it must not be reported as having none. Check the "
            "installed gh version against the fields this adapter requests."
        )
    return [ref for ref in (_ref(node) for node in nodes) if ref]


def _same_ref(ref: str | None, other: str | None) -> bool:
    """Whether two issue references name the same issue, comparing `7`, `#7` and a URL alike.

    Needed because the caller may pass a number where `gh` reported a URL, and the verification
    re-read must not call a landed write a failure over spelling.
    """
    if not ref or not other:
        return False
    first, second = (str(x).rstrip("/").rsplit("/", 1)[-1].lstrip("#") for x in (ref, other))
    return first == second


def _project_ref() -> tuple[str, str]:
    """The configured board as `(owner, number)`. An unusable value fails before any write."""
    ref = str(config.get("tracker_config.project", default="") or "")
    owner, sep, number = ref.rpartition("/")
    if not sep or not owner or not number.isdigit():
        raise TrackerError(
            f"tracker_config.project must be <owner>/<number> (e.g. @me/3 or my-org/3); got {ref!r}. "
            "Set it in .shipyard/config.json; see docs/github-setup.md."
        )
    return owner, number


def _confirm_board_access() -> None:
    """Prove the credential reaches Projects v2 by reading the board, for a token that reports no scopes.

    Stands in for the scope check preflight cannot make on a fine-grained or App token: the failure it
    exists to catch is a `repo`-only credential, and such a credential cannot read Projects v2 at all,
    so one successful board read is positive evidence rather than an assumption. A grant that can read
    the board but not write it is indistinguishable from here without performing a write, which a
    preflight must not do; that residual case fails later, at the write, with its own message.
    """
    owner, number = _project_ref()
    try:
        _raw_items(owner, number)
    except TrackerError as exc:
        raise TrackerError(
            f"the gh token reports no scopes and project {owner}/{number} could not be read with it, so the "
            f"Projects v2 access every board write needs is unconfirmed: {exc} Grant it with `gh auth refresh "
            "-s project,read:project`, or give a fine-grained token read and write access to the board."
        ) from None


def _option_id(resolved: dict[str, Any], field_name: str, option_name: str) -> tuple[str, str] | None:
    """`(field_id, option_id)` for an option matched case-insensitively, or None when none matches."""
    field = resolved["fields"].get(field_name)
    if not field:
        return None
    target = option_name.strip().lower()
    for name, option_id in field["options"].items():
        if name.strip().lower() == target:
            return field["id"], option_id
    return None


def _item_index(owner: str, number: str) -> dict[str, dict[str, Any]]:
    """Every board item, canonicalised and keyed by the URL of the issue it holds."""
    index = {}
    for raw in _raw_items(owner, number):
        item = _normalize_item(raw)
        if item["url"]:
            index[str(item["url"])] = item
    return index


def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    """One board item, with its `Type` and `Status` option names mapped to canonical tokens."""
    content = item.get("content") or {}
    return {
        "number": content.get("number"),
        "title": content.get("title") or item.get("title"),
        "url": content.get("url"),
        "type": canonical_type(item.get("type")),
        "status": canonical_status(item.get("status")),
    }


def _verify_field(owner: str, number: str, issue_url: str, field_name: str, option_name: str) -> None:
    """Confirm the single-select write by re-reading the board, per CONTRIBUTING's write discipline.

    `gh project item-edit` reports success whether or not the value changed, and a card that did not
    move is exactly the failure a caller cannot see, so the value is read back by name.

    The read is retried because the board's item list is eventually consistent, which a live run
    caught: a card added and edited moments earlier can be entirely absent from the very next
    `item-list`, then present with the right value a second later. Failing on the first read turns a
    write that landed into a reported failure, so the retry is what makes read-back verification
    usable here at all — but it stays bounded, because a genuinely unset field must still fail.
    """
    last = ""
    for attempt in range(VERIFY_ATTEMPTS):
        if attempt:
            time.sleep(VERIFY_BACKOFF_SECONDS * attempt)
        for raw in _raw_items(owner, number):
            if (raw.get("content") or {}).get("url") != issue_url:
                continue
            last = str(raw.get(field_name.lower()) or "")
            if last.strip().lower() == option_name.strip().lower():
                return
    raise TrackerError(
        f"{field_name} on {issue_url} still reads {last or 'unset'!r} rather than {option_name!r} after "
        f"{VERIFY_ATTEMPTS} reads of project {owner}/{number}; treat the board update as failed."
    )


def _raw_items(owner: str, number: str) -> list[dict[str, Any]]:
    """Every item on the board, unmapped. The limit is `gh`'s maximum, not a page size."""
    data = _gh_data(["project", "item-list", number, "--owner", owner, "--format", "json", "--limit", ITEM_LIMIT])
    return [item for item in _as_list(data, "items") if isinstance(item, dict)]


def _as_list(data: object, key: str) -> list[Any]:
    """The list at `key`, or `data` itself when `gh` returned a bare array. Never raises."""
    if isinstance(data, dict):
        value = data.get(key, [])
        return value if isinstance(value, list) else []
    return data if isinstance(data, list) else []


def _repo_args() -> list[str]:
    """`--repo` when configured, so a write does not depend on the server's working directory."""
    repo = config.get("tracker_config.repo", default=None)
    return ["--repo", str(repo)] if repo else []


def _gh(args: list[str]) -> str:
    """Run `gh` and return its trimmed stdout. Writes nothing to this process's stdout.

    The timeout bounds a `gh` that never returns — a network stall, or a credential helper
    prompting on a stdin no one is answering — because this process has other calls to serve.
    """
    try:
        proc = subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=False, timeout=TIMEOUT_SECONDS
        )
    except FileNotFoundError:
        raise TrackerError("gh is not installed or not on PATH; install the GitHub CLI.") from None
    except subprocess.TimeoutExpired:
        raise TrackerError(
            f"gh {_shown(args)} did not finish within {TIMEOUT_SECONDS}s and was killed; it may be "
            "waiting on a credential prompt or a stalled network. Run the same command in a terminal "
            "to see what it wants."
        ) from None
    if proc.returncode != 0:
        raise TrackerError(f"gh {_shown(args)} failed: {_safe(proc.stderr)}")
    return proc.stdout.strip()


def _gh_data(args: list[str]) -> Any:
    """`_gh`, with the response parsed as JSON of whichever shape `gh` chose. Empty output is `{}`.

    Both shapes occur: `gh issue list` returns an array while `gh project view` returns an object.
    """
    out = _gh(args)
    try:
        return json.loads(out) if out else {}
    except json.JSONDecodeError:
        raise TrackerError(f"gh {_shown(args)} returned output that is not JSON.") from None


def _gh_json(args: list[str]) -> dict[str, Any]:
    """`_gh_data`, rejecting anything but a JSON object."""
    parsed = _gh_data(args)
    if not isinstance(parsed, dict):
        raise TrackerError(f"gh {_shown(args)} returned {type(parsed).__name__}, expected a JSON object.")
    return parsed


def _shown(args: list[str]) -> str:
    """The `gh` argv as a message may carry it: joined, and scrubbed exactly as command output is.

    The argv is not obviously secret-bearing, but `--body` carries whatever the caller wrote, so it
    goes through the same redaction as stderr rather than being trusted to be clean."""
    return _safe(" ".join(args))


def _safe(text: str) -> str:
    """Command output, with any credential this process holds redacted, ready to put in a message.

    Discovery honours `redaction.extra_words`, so an org-specific credential name redacts here
    exactly as it does on the attach-artifact sanitisation path.
    """
    scrubbed, _ = scrub_text(text.strip(), discover_secret_vars(extra_words=config.extra_secret_words()))
    return scrubbed[:STDERR_LIMIT]
