"""GitHub tracker adapter, spoken to only through `sy_tools.tracker.adapter()`.

Every canonical verb of `skills/tracker/CONTRACT.md`, over the `gh` transport. Two properties come
from where this runs rather than from `gh`: nothing here writes to stdout, which carries the JSON-RPC
frames one stray line would desynchronise, and a failure raises `TrackerError` rather than exiting,
because this process has other calls to serve after a bad one.

`Type` and `Status` are Projects v2 single-select fields that `gh issue` cannot touch at all, so both
are read and written through the board, resolved once per adapter. The attachment verbs are the
asymmetry `skills/tracker/github/ADAPTER.md` documents: this tracker has no scriptable file
attachment, so an artifact becomes a secret gist that a comment on the work item links to, and the
lifecycle verbs act on that gist through the link that comment carries.

Credentials are `gh`'s own business. Nothing here reads, passes, or echoes a token, and every message
built from command output or from a configured value goes through `_safe`.

The canonical verbs are `async` because the seam above this module is; `gh` offers no async transport,
so each offloads a synchronous `_sync_*` body to a worker thread.
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
from ...secrets import DEFAULT_MIN_LENGTH, discover_secret_vars, scrub_text
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

MAX_BOARD_READS = 500
"""How many individual `gh issue view` reads one board-filtered search may perform.

`limit` bounds the reads by itself only while a board value is the only filter: `text` and `parent` are
matched against the read, so a filter that matches nothing reads every remaining candidate — one
subprocess each, bounded individually by `TIMEOUT_SECONDS` and in aggregate by nothing but this."""

ARTIFACT_COMMENT = "Shipyard artifact `{filename}`: {url}\n\nSecret gist — reachable only from this link."
ARTIFACT_LINK = re.compile(r"Shipyard artifact `([^`\n]+)`: (https://\S+)")
"""The comment `attach-artifact` writes, and the pattern the lifecycle verbs read it back with.

One pair on purpose: nothing else on this tracker links an issue to its artifact, so this comment *is*
the attachment index. Change the wording on one side only and every lifecycle verb reports that an issue
has no artifacts while its transcripts sit in gists nobody can find again."""

FORBIDDEN_IN_FILENAME = ("`", "\n")
"""Characters an artifact's filename may not carry, because that index is prose with no escaping in it.

`ARTIFACT_LINK` reads the filename back from between backticks on one line, so a name holding either
character writes an entry no read can match while the upload reports success. Both are legal in a POSIX
filename, and both are refused rather than escaped — a comment body has nowhere to escape them to."""

VERIFY_ATTEMPTS = 4
VERIFY_BACKOFF_SECONDS = 0.75
"""How hard a board write is re-read before it is called a failure.

The board's item list is eventually consistent, so the first read after a write can legitimately miss
the card entirely; the bound matters as much as the retry, since an unset field has to stay a failure
rather than becoming a hang."""


class GithubAdapter:
    """Canonical tracker verbs, mapped onto the `gh` CLI.

    Every returned `id` is the issue URL: `gh` accepts a URL wherever it accepts a number and
    `project item-add --url` accepts nothing else, while the GraphQL node id `gh` reports as `id` is
    accepted by no `gh issue` command. The resolved-board cache below lives for one tool call, since
    `tracker.adapter()` builds a fresh adapter per call — long enough to stop `create_issue` resolving
    the same board twice, short enough that no id survives a board edit between calls.
    """

    name = "github"
    # Undocumented: GitHub publishes no body limit anywhere, and this figure is attested by nothing but
    # the API's own error string on a write that goes over it.
    body_limit: int = 65_536

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
        page_token: str | None = None,
    ) -> dict:
        """Search issues, optionally by canonical status, canonical type, parent or free text.

        `gh` has no cursor, so `next_page_token` is always None and `page_token` is accepted and then
        ignored: the canonical signature stays identical on both adapters, and a cursor `gh` cannot
        honour would page a caller through a set nothing is holding still.

        With no status or type filter this is one `gh issue list` page of up to `limit` issues. A status
        or type filter names a board value, so the board becomes the candidate set: `limit` bounds the
        page rather than the fetch, `is_last` says whether a further match exists, and `text` is matched
        here against title and body instead of by GitHub's server-side search. A query too wide to read
        within `MAX_BOARD_READS` fails saying so, as does a status or type no card can carry, rather
        than answering an empty page.
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
        """Upload `path` as a secret gist and link it from a comment on `issue`."""
        return await to_thread.run_sync(self._sync_attach_artifact, issue, path)

    async def type_convert(self, issue: str, issue_type: str) -> dict:
        """Change `issue`'s board `Type` to canonical `issue_type`, verified by re-reading the card."""
        return await to_thread.run_sync(self._sync_type_convert, issue, issue_type)

    async def attachment_download(self, issue: str, filename_or_id: str, output_path: Path) -> dict:
        """Write the artifact gist `filename_or_id` names on `issue` to `output_path`."""
        return await to_thread.run_sync(self._sync_attachment_download, issue, filename_or_id, output_path)

    async def attachment_update(self, issue: str, path: Path) -> dict:
        """Replace the contents of `issue`'s artifact gist named `path.name` with `path`."""
        return await to_thread.run_sync(self._sync_attachment_update, issue, path)

    async def preflight(self) -> dict:
        """Confirm `gh` is installed, authenticated, and able to reach the configured board."""
        return await to_thread.run_sync(self._sync_preflight)

    def _sync_create_issue(self, issue_type: str, title: str, body: str, parent: str | None) -> dict:
        """Create the issue, then put it on the board with its `Type` set."""
        # Mapped before the write: an unknown canonical token must not leave an issue created untyped.
        option = native_type(issue_type)
        url = _gh(["issue", "create", *_repo_args(), "--title", title, "--body", body])
        if not url.startswith("https://"):
            raise TrackerError(f"gh issue create returned no issue URL for {_safe(title)!r}; nothing was created.")
        self._set_field(url, TYPE_FIELD, option)
        if parent:
            _edit(url, "--parent", parent)
        return {"id": url, "url": url, "type": issue_type, "title": title, "parent": parent}

    def _sync_get_issue(self, issue: str) -> dict:
        """Read `issue` from `gh`, with status and type taken from the board.

        `gh issue view --json` exposes no project single-select value, so `Status` and `Type` are read
        from the board card instead. `children_truncated` and `dependencies_truncated` say whether
        `gh`'s own per-page cap on those two relations cut anything off, the same signal the other
        adapter reports as `comments_truncated`: a clipped list reads exactly like a complete short one.
        The comment thread needs no such flag: GitHub's comment connection is a cursored page `gh`
        follows to exhaustion, so the thread comes back whole and this read reports no comment cap.
        """
        data = _view(issue, ISSUE_FIELDS)
        url = str(data.get("url") or "")
        if not url:
            raise TrackerError(f"gh issue view {issue} returned no issue; treat the read as failed.")
        owner, number = _project_ref()
        children, children_truncated = _relation(data.get("subIssues"))
        dependencies, dependencies_truncated = _relation(data.get("blockedBy"))
        comments = _comments(data)
        return {
            **_summary(data, _item_index(owner, number).get(url, {})),
            "body": str(data.get("body") or ""),
            "children": children,
            "children_truncated": children_truncated,
            "dependencies": dependencies,
            "dependencies_truncated": dependencies_truncated,
            "comments": comments,
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

        Status, type and parent are filtered here because they are board values and a sub-issue
        relation, not list flags, so a status or type filter makes the board — not the repository — the
        candidate set. Without one this is a single `gh issue list` page, and `is_last` is read from
        whether that page came back full.
        """
        if limit <= 0:
            raise TrackerError(f"limit must be a positive number of issues, got {limit}")
        # Board-first: `issue list --limit` takes the newest N and filters after, so an older match answered
        # count: 0. Widening to ITEM_LIMIT instead bounds it by the repository, which costs about a second
        # per hundred rows — a few thousand all-state issues and the call alone exceeds TIMEOUT_SECONDS —
        # and with --search routes through the Search API, whose silent 1k cap is invisible in --json.
        if status is not None or issue_type is not None:
            return self._board_page(status=status, issue_type=issue_type, parent=parent, text=text, limit=limit)
        # --state all: a done issue is still one a caller searches for, and `issue list` hides closed ones.
        args = [
            "issue", "list", *_repo_args(), "--state", "all", "--limit", str(limit), "--json", SUMMARY_FIELDS
        ]
        if text:
            args += ["--search", text]
        rows = _listed_rows(args)
        urls = [str(row.get("url") or "") for row in rows]
        if not all(urls):
            raise TrackerError(
                f"gh {_shown(args)} returned {urls.count('')} of {len(rows)} rows with no issue URL, which "
                "is the reference every verb here identifies an issue by, so this page is refused rather "
                "than answered with a row nothing could then act on. Check the installed gh version "
                "against the fields this adapter requests."
            )
        owner, number = _project_ref()
        index = _item_index(owner, number)
        matched = [
            item
            for item in (_summary(row, index.get(url, {})) for row, url in zip(rows, urls, strict=True))
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

        Any other assignee is refused rather than silently redirected to `@me`, and the returned
        `assignee` is read back rather than echoing the request — the shape both adapters report, since
        `@me` names an intent and only the resolved account evidences which identity owns the issue.
        """
        if assignee != "@me":
            raise TrackerError(
                f"only self-assignment is supported by this adapter; got {assignee!r}. Pass '@me', or "
                "assign someone else with `gh issue edit --add-assignee`."
            )
        # Resolved before the write, because `--add-assignee` is a zero-exit no-op on an already-assigned
        # issue: a non-empty assignee list proves someone owns it, not that this account does.
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
        found, _ = _refs(_view(url, "blockedBy").get("blockedBy"))
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
                f"{_safe(label)!r} is not on {issue} after the write; labels read back as {labels or 'none'}. "
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

        Every write of a board value — `Status`, and the `Type` both `create-issue` and `type-convert`
        set — goes through here, so none of them can skip the read-back verification.
        """
        owner, number = _project_ref()
        resolved, (field_id, option_id) = self._checked_option(owner, number, field_name, option_name)
        item_id = self._find_or_add_item(owner, number, issue_url)
        # The human-readable success line `item-edit` prints is deliberately not parsed: it must not turn
        # a completed write into a failure. `_verify_field` below is what evidences the write instead.
        _gh([
            "project", "item-edit",
            "--id", item_id,
            "--project-id", resolved["project_id"],
            "--field-id", field_id,
            "--single-select-option-id", option_id,
        ])
        _verify_field(owner, number, issue_url, field_name, option_name)
        return item_id

    def _checked_option(
        self, owner: str, number: str, field_name: str, option_name: str, *, context: str = ""
    ) -> tuple[dict[str, Any], tuple[str, str]]:
        """The board and the `(field_id, option_id)` for one native option name, or a failure naming the drift.

        Shared by the write path with the read filter, which meet the same fault: the board's real
        `Status` column or `Type` option name has drifted from the `columns.*` config key naming it, and
        a read filtering on the drifted value answered `count: 0, is_last: true` from a board with work
        on it — the one wrong answer a caller cannot tell from an empty queue. `context` is what the read
        path adds to say the query was refused rather than answered.
        """
        resolved = self._resolve(owner, number, refresh=False)
        ids = _option_id(resolved, field_name, option_name)
        if ids is None:
            # Re-resolved once before a miss is treated as real: an option added to the board after it
            # was resolved must work without restarting the server.
            resolved = self._resolve(owner, number, refresh=True)
            ids = _option_id(resolved, field_name, option_name)
        if ids is None:
            available = sorted((resolved["fields"].get(field_name) or {}).get("options", {}))
            raise TrackerError(
                f"project {owner}/{number} field {field_name!r} has no option matching {option_name!r} "
                f"(case-insensitive); available: {available}. Fix the board option or the columns.* config "
                f"key. See docs/github-setup.md.{context}"
            )
        return resolved, ids

    def _board_page(
        self, *, status: str | None, issue_type: str | None, parent: str | None, text: str | None, limit: int
    ) -> dict:
        """One page of the board items matching a status or type filter, read board-first.

        The board item list is the candidate set: it carries `Status` and `Type`, is bounded by the
        board's own size, and never touches `gh issue list` or the Search API. Candidates are narrowed to
        actual issues in one concrete repository — `tracker_config.repo` when set, otherwise the one `gh`
        resolves from the working directory, never the whole board — before any of them is read, and only
        the survivors are read individually for the fields a card does not carry, so the per-issue cost
        scales with the filtered board rather than with the repository.

        Reaching `MAX_BOARD_READS` with a full page returns that page as the truncated page it is,
        `is_last` false; reaching it without one fails saying what to narrow, because a page that is
        neither full nor known to be complete is the answer a caller cannot act on.
        """
        # Rejects an unrecognised canonical token before any `gh` call, as every write here does: `in_progress`
        # for `in-progress` answered count: 0, which the duplicate-work checks read as no prior work.
        wanted = {
            STATUS_FIELD: native_status(status) if status is not None else None,
            TYPE_FIELD: native_type(issue_type) if issue_type is not None else None,
        }
        owner, number = _project_ref()
        repo = _effective_repo()
        index = _item_index(owner, number)
        # After the board read rather than before it: a board this adapter cannot read completely keeps
        # its own failure instead of sending an operator to fix a column name that is already right.
        for field_name, option_name in wanted.items():
            if option_name is not None:
                self._checked_option(
                    owner, number, field_name, option_name,
                    context=" This search is refused rather than answered with an empty page: no card can "
                    "carry a board value the board does not offer.",
                )
        candidates = [
            (url, item)
            for url, item in index.items()
            if (status is None or item["status"] == status)
            and (issue_type is None or item["type"] == issue_type)
            and _in_repo(url, repo)
            and _is_issue_card(url, item)
        ]
        fields = f"{SUMMARY_FIELDS},body" if text else SUMMARY_FIELDS
        needle = (text or "").strip().lower()
        matched: list[dict[str, Any]] = []
        reads = 0
        bounded = False
        for url, item in candidates:
            # One match past the page: `is_last` needs only whether a further match exists, and every
            # further candidate costs a `gh issue view` a caller would never see.
            if len(matched) > limit:
                break
            # Bounds the case `limit` cannot: a `text` or `parent` filter that matches nothing rejects
            # every candidate only after reading it.
            if reads >= MAX_BOARD_READS:
                if len(matched) < limit:
                    raise TrackerError(
                        f"this search read the {MAX_BOARD_READS} board items one call reads individually "
                        f"without filling a page of {limit} from them, and there are more; narrow it with a "
                        "parent, a text term, or a status or type that fewer cards carry. The partial result "
                        "is refused rather than reported as complete, because it would not be."
                    )
                bounded = True
                break
            data = _view(url, fields)
            reads += 1
            if not str(data.get("url") or ""):
                raise TrackerError(f"gh issue view {url} returned no issue; treat the read as failed.")
            summary = _summary(data, item)
            if parent is not None and not _same_ref(summary["parent"], parent):
                continue
            # Substring, not `gh issue list --search`: GitHub's ranking and query syntax are given up for
            # a set complete for the board rather than capped at the Search API's thousandth row.
            if needle and needle not in f"{data.get('title') or ''}\n{data.get('body') or ''}".lower():
                continue
            matched.append(summary)
        page = matched[:limit]
        return {
            "issues": page,
            "count": len(page),
            "is_last": len(matched) <= limit and not bounded,
            "next_page_token": None,
        }

    def _resolve(self, owner: str, number: str, *, refresh: bool) -> dict[str, Any]:
        """The board's node id and its single-select fields, cached for the life of this adapter.

        Only fields carrying options are kept: everything else on the board is a text, number or date
        field this adapter never writes.
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
        # Asked for at `gh`'s maximum, not its 30-row default: a board wide enough to push `Status` past
        # the thirtieth field would otherwise resolve as a board that has no `Status` at all.
        field_args = ["project", "field-list", number, "--owner", owner, "--format", "json", "--limit", ITEM_LIMIT]
        fields = _gh_data(field_args)
        raw_fields = fields.get("fields") if isinstance(fields, dict) else None
        if not isinstance(raw_fields, list):
            raise TrackerError(
                f"gh {_shown(field_args)} returned no list of fields for project {owner}/{number}, so its "
                "Status/Type options could not be read — this must not be read as a board with no options on "
                "it, which is exactly the drift a caller cannot tell apart from a genuine `columns.*` "
                "misconfiguration. Check the installed gh version against the fields this adapter requests."
            )
        resolved: dict[str, Any] = {"project_id": str(project_id), "fields": {}}
        for field in raw_fields:
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
            if (_content_of(item) or {}).get("url") == issue_url:
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

        Returns the transport's own evidence: the gist URL it printed, the re-read confirmation that the
        gist is not public, and the URL of the comment that carries the link. Any step that produces no
        output, or exits non-zero, is a failure rather than a warning.
        """
        issue = _checked_ref(issue)
        _checked_artifact(path)

        gist_url = _gh(["gist", "create", "--desc", f"shipyard artifact {issue}", str(path)])
        if not gist_url.startswith("https://"):
            raise TrackerError(
                f"gist creation returned no usable URL for {path.name}; nothing was attached to {issue}."
            )
        gist_id = _gist_id(gist_url)
        # Privacy read back rather than assumed from the flags passed: a public gist would publish a
        # transcript irrevocably.
        if _gh_json(["api", f"gists/{gist_id}"]).get("public") is not False:
            raise TrackerError(
                f"{gist_url} is public or its visibility could not be confirmed; refusing to link it "
                f"from {issue}. Delete it: gh gist delete {gist_id}"
            )

        body = ARTIFACT_COMMENT.format(filename=path.name, url=gist_url)
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

    def _sync_type_convert(self, issue: str, issue_type: str) -> dict:
        """Set the board `Type` on an issue that already exists, reporting both vocabularies.

        The same board write `create_issue` makes, on an issue it did not just create, so it goes through
        `_set_field` and inherits that path's bounded read-back.
        """
        # Mapped before anything is written: an unknown canonical token must not leave a card
        # half-converted.
        option = native_type(issue_type)
        url = _url_of(issue)
        self._set_field(url, TYPE_FIELD, option)
        return {"id": url, "type": issue_type, "native": option}

    def _sync_attachment_download(self, issue: str, filename_or_id: str, output_path: Path) -> dict:
        """Write the artifact gist `filename_or_id` names on `issue` to `output_path`.

        Empty output is a failure rather than a zero-byte file: on disk the two are indistinguishable,
        and only one of them is an artifact. The bytes written are the artifact's text, not a byte-exact
        copy — `_gh` trims what it returns, so a trailing newline is not preserved. That is stated rather
        than worked around, because every verb here goes through `_gh` for the timeout and the credential
        scrubbing it applies.
        """
        issue = _checked_ref(issue)
        gist_id, filename, _ = _resolve_gist(issue, filename_or_id)
        # `--filename` is not optional: without it `gh gist view --raw` prepends the gist's *description*
        # and a blank line, which wrote a corrupted artifact while reporting a successful download —
        # measured against a real gist, since the mocked transport answered the same argv without it.
        content = _gh(["gist", "view", gist_id, "--raw", "--filename", filename])
        if not content:
            raise TrackerError(
                f"gist {gist_id} holding {filename!r} on {issue} read back empty, so there is nothing to "
                f"write to {output_path}; treat the download as failed rather than writing an empty file."
            )
        data = content.encode("utf-8")
        output_path.write_bytes(data)
        return {
            "issue": issue,
            "filename": filename,
            "id": gist_id,
            "bytes": len(data),
            "path": str(output_path),
        }

    def _sync_attachment_update(self, issue: str, path: Path) -> dict:
        """Replace the contents of `issue`'s artifact gist named `path.name` with `path`.

        An issue carrying no artifact of that name is a first upload, not an error: the verb's contract is
        replace-by-filename with zero existing explicitly fine, so that case does what a fresh attach does
        — the same gist, privacy re-read and linking comment — and reports `replaced: 0`, which is how a
        caller tells a supersede from an upload.
        """
        issue = _checked_ref(issue)
        intended = _checked_artifact(path)
        found = _find_gist(issue, path.name)
        if found is None:
            fresh = self._sync_attach_artifact(issue, path)
            return {
                "issue": issue,
                "filename": path.name,
                "id": _gist_id(fresh["gist_url"]),
                "gist_url": fresh["gist_url"],
                "replaced": 0,
                "comment_url": fresh["comment_url"],
            }
        gist_id, filename, gist_url = found
        # A real in-place replace, per the flag semantics `gh 2.96.0` documents: `--filename` replaces that
        # one file, so the gist id and the comment's URL survive, where `--add` would add a second file.
        _gh(["gist", "edit", gist_id, "--filename", filename, str(path)])
        # Read back because `gh gist edit` prints nothing and exits zero either way; named for the reason
        # the download is; whitespace-insensitive only, since `_gh` trims its output.
        if _gh(["gist", "view", gist_id, "--raw", "--filename", filename]).strip() != intended.strip():
            raise TrackerError(
                f"gist {gist_id} does not read back as the contents of {path.name} after the edit, so the "
                f"replacement is unconfirmed and {issue} may still link the previous artifact."
            )
        return {"issue": issue, "filename": filename, "id": gist_id, "gist_url": gist_url, "replaced": 1}

    def _sync_preflight(self) -> dict:
        """Confirm `gh` is installed, authenticated, and able to reach the configured board.

        Reading the board is the only thing that confirms reachability, the claim
        `skills/tracker/github/ADAPTER.md` makes for this verb. `scopes` comes back as None rather than an
        empty or invented list to say which check was available: only a classic or OAuth token has scopes
        at all, `gh auth status` prints no `Token scopes:` line for a fine-grained PAT or an App token,
        and an absent line must not read as "unscoped".
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
            # Kept ahead of the board read as the cheap pre-check it is: it names the exact fix for the one
            # credential fault diagnosable without touching the board.
            scopes = sorted(re.findall(r"'([^']+)'", line.group(1)))
            if "project" not in scopes:
                raise TrackerError(
                    f"the gh token is missing the 'project' scope, so every board write would fail; it has "
                    f"{scopes or 'no scopes'}. Grant it with `gh auth refresh -s project,read:project`."
                )
        _confirm_board_access(scopes)
        return {
            "tool": "gh",
            "version": version[0] if version else "unknown",
            "authenticated": True,
            "account": account.group(1) if account else None,
            "scopes": scopes,
        }


def _checked_ref(issue: str) -> str:
    """`issue` if it is a reference `gh` reads as an issue: a URL or a number, as this adapter produces.

    An id crosses the tool boundary as an opaque string and lands in `gh`'s argv as a positional, so
    without this an id shaped like `-Rowner/repo` is a flag — and with `tracker_config.repo` unset there
    is no `--repo` ahead of it to lose the race, so the write retargets to a repo the caller named.
    """
    ref = issue.strip()
    if not re.fullmatch(r"https://\S+|#?\d+", ref):
        raise TrackerError(
            f"{issue!r} is not an issue reference this adapter accepts; pass the issue number, #number, "
            "or its https:// URL. Anything else is refused rather than handed to gh as an argument."
        )
    return ref


def _checked_artifact(path: Path) -> str:
    """`path`'s decoded text, refusing before any upload the faults an artifact this tracker cannot hold presents.

    Returning the decode rather than just `path.name` means a caller that needs the text (`attachment-
    update`'s read-back comparison) never re-reads the file: a second read would open a TOCTOU window a
    caller that only needed the refusal has no reason to reintroduce. The name is checked, not escaped —
    see `FORBIDDEN_IN_FILENAME` — and every check is shared by the two uploading verbs rather than
    repeated, because on this tracker a first `attachment-update` *is* an upload: a check only one of
    them made would be a check the other could write past.
    """
    if any(character in path.name for character in FORBIDDEN_IN_FILENAME):
        raise TrackerError(
            f"an artifact filename may not contain a backtick or a newline, and {path.name!r} does: it is "
            "written into the comment that is this tracker's only record of which gist holds which "
            "artifact, and no later read of that comment could match it. Rename the file and retry."
        )
    if not path.is_file():
        raise TrackerError(f"artifact not found: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise TrackerError(
            f"{path.name} is not UTF-8 text, and this tracker's artifact store holds text only, so it "
            "cannot hold this artifact at all. Attach a text rendering of it instead."
        ) from None


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


def _gist_id(gist_url: str) -> str:
    """The gist id in a gist URL: the last path segment, which is what every `gh gist` call takes."""
    return gist_url.rstrip("/").rsplit("/", 1)[-1]


def _artifact_gists(issue: str, filename_or_id: str) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    """`(the artifacts on `issue` that `filename_or_id` selects, every artifact its comments record)`.

    The issue's own comments are the index — `attach-artifact` records the filename and the gist URL in
    one, and nothing else on this tracker ties the two together — matched by gist id first and then by
    exact filename. Both lists come from the one read, because how many matched decides the outcome and
    everything recorded is what a failure has to list. Each entry is `(filename, gist_id, gist_url)`, and
    one artifact recorded by two identical comments collapses rather than reading as an ambiguity.
    """
    links = list(
        dict.fromkeys(
            (match.group(1), _gist_id(match.group(2)), match.group(2))
            for comment in _comments(_view(issue, "comments"))
            for match in ARTIFACT_LINK.finditer(comment["body"])
        )
    )
    by_id = [link for link in links if link[1] == filename_or_id]
    matches = by_id if len(by_id) == 1 else [link for link in links if link[0] == filename_or_id]
    return matches, links


def _not_one_artifact(
    issue: str,
    filename_or_id: str,
    matches: list[tuple[str, str, str]],
    links: list[tuple[str, str, str]],
) -> TrackerError:
    """The refusal for a name that selects other than one artifact, listing what the comments do record."""
    # Scrubbed below: the listing is built from comment bodies, which carry whatever a caller wrote.
    listing = ", ".join(f"{filename} -> {gist_id}" for filename, gist_id, _ in (matches or links))
    return TrackerError(
        f"{len(matches)} artifacts on {issue} match {filename_or_id!r}; expected exactly one, so pass the gist "
        f"id to name the one you mean. The artifact comments on {issue} record: {_safe(listing) or 'none'}"
    )


def _resolve_gist(issue: str, filename_or_id: str) -> tuple[str, str, str]:
    """The one artifact gist `filename_or_id` names on `issue`, as `(gist_id, filename, gist_url)`.

    Exactly one match or a failure, zero included — the rule the other adapter applies to attachments
    sharing a filename, for the same reason: two gists under one name make picking either an arbitrary
    download or an irrevocable delete. `_find_gist` is the variant for the one verb where zero is a
    legitimate answer.
    """
    matches, links = _artifact_gists(issue, filename_or_id)
    if len(matches) != 1:
        raise _not_one_artifact(issue, filename_or_id, matches, links)
    filename, gist_id, gist_url = matches[0]
    return gist_id, filename, gist_url


def _find_gist(issue: str, filename_or_id: str) -> tuple[str, str, str] | None:
    """`_resolve_gist`, returning None instead of raising when the issue records no such artifact.

    `attachment-update` is replace-by-filename with zero existing explicitly fine — a plain first upload.
    More than one still raises: which of two namesakes to overwrite is the question nothing here can answer.
    """
    matches, links = _artifact_gists(issue, filename_or_id)
    if not matches:
        return None
    if len(matches) != 1:
        raise _not_one_artifact(issue, filename_or_id, matches, links)
    filename, gist_id, gist_url = matches[0]
    return gist_id, filename, gist_url


def _repo_slug(url: str) -> str:
    """The normalised `owner/repo` an issue URL belongs to, from its path so a GHES host works too.

    Only `gh`'s own output for a board card is parsed here — always `<host>/<owner>/<repo>/issues/<n>` — so
    the two path segments before that tail are the pair, lowercased because GitHub's names are
    case-insensitive and `_effective_repo` lowercases the side this is compared against. The repository the
    *caller* spelled is not parsed here or anywhere in this file; `gh` resolves that one. `""` means the URL
    holds no pair to compare, which `_in_repo` refuses rather than reads as "some other repository".
    """
    parts = url.rstrip("/").split("/")
    pair = [part for part in parts[-4:-2] if part] if len(parts) >= 4 else []
    return "/".join(pair).lower() if len(pair) == 2 else ""


def _in_repo(url: str, repo: str) -> bool:
    """Whether a board card's issue URL belongs to `repo`, refusing a URL holding no pair to compare.

    Every URL that reaches here is one `gh` printed for a board card, always
    `<host>/<owner>/<repo>/issues|pull/<n>`, so no pair in it is shape drift rather than a card belonging
    somewhere else — and an unreadable one compared as "some other repository" would drop out of a page
    that still reported itself complete, the same silent shortening `_item_index`, `_raw_items` and
    `_listed_rows` each refuse. This also runs before `_is_issue_card`, which such a card never reaches.
    """
    slug = _repo_slug(url)
    if not slug:
        raise TrackerError(
            f"the board card {url} carries no owner/repo for this search to compare against {repo}, so "
            "whether it belongs to this repository is unknown; it must not be dropped from a page that then "
            "reports itself complete. Check the installed gh version against the fields this adapter requests."
        )
    return slug == repo


def _effective_repo() -> str:
    """The single repository a board-filtered search is scoped to, as `gh` itself resolves it.

    `tracker_config.repo` when it is set, otherwise the repository `gh` resolves from the working
    directory — the pattern `_repo_args()` sets for every write here, so a search answers about the
    repository a write lands in rather than widening to the whole board: a page mixing another repo's cards
    into this repo's queue is read as this repo's queue. An unresolvable value is a failure rather than
    that widening, and `gh` refusing it is that check.
    """
    configured = str(config.get("tracker_config.repo", default="") or "")
    # Unparsed but not unchecked: a value opening with `-` lands in `gh`'s argv as a bare positional and
    # would be read as a flag — `_checked_ref`'s hazard, on a value arriving from configuration.
    if configured.strip().startswith("-"):
        raise TrackerError(
            f"tracker_config.repo is set to {_safe(configured)!r}, which gh would read as a flag rather "
            "than a repository reference, so it is refused before gh is called. Set it to OWNER/REPO in "
            ".shipyard/config.json; see docs/github-setup.md."
        )
    # Both sides come from `gh`: `--repo` also takes HOST/OWNER/REPO, https with or without `.git`, and an
    # scp-like SSH remote; every hand-written parser was one spelling short, each miss answering count: 0.
    args = ["repo", "view", *([configured] if configured else []), "--json", "nameWithOwner", "-q", ".nameWithOwner"]
    try:
        printed = _gh(args)
    except TrackerError as exc:
        source = (
            f"tracker_config.repo is set to {_safe(configured)!r}"
            if configured
            else "tracker_config.repo is unset, so this search is scoped to the repository gh resolves from the "
            "working directory"
        )
        raise TrackerError(
            f"{source}, and gh could not resolve it to a repository: {exc} Fix tracker_config.repo in "
            ".shipyard/config.json, or run from a checkout with a GitHub remote; see docs/github-setup.md."
        ) from None
    resolved = printed.strip().lower()
    if not re.fullmatch(r"[^\s/]+/[^\s/]+", resolved):
        raise TrackerError(
            f"gh resolved this search's repository to {_safe(printed) or 'nothing'!r}, which is not one "
            "owner/repo pair, so the search has no repository to scope to and will not answer for the whole "
            "board instead. Check tracker_config.repo in .shipyard/config.json; see docs/github-setup.md."
        )
    return resolved


def _is_issue_card(url: str, item: dict[str, Any]) -> bool:
    """Whether a board card holds an issue, so a pull request or a draft never answers an issue search.

    `gh issue view` reads a pull request URL happily enough that a PR card in the filtered column comes
    back looking like an issue, which the duplicate-work checks in `skills/plan/SKILL.md` and
    `skills/spec/SKILL.md` then read as prior work. A card whose content type is missing altogether is a
    failure rather than a guess in either direction.
    """
    kind = str(item.get("kind") or "")
    if not kind:
        raise _unclassifiable_card(url)
    return kind.strip().lower() == "issue"


def _unclassifiable_card(ref: str) -> TrackerError:
    """The failure a board card carrying no content type gets, shared by both places that can see one.

    Returned rather than raised so each caller raises it at its own site: `_item_index` meets such a card
    when its content object also carries no URL, `_is_issue_card` when it carries one, and both are the
    same unreadable shape rather than two different problems.
    """
    return TrackerError(
        f"the board reports no content type for {ref}, so whether that card holds an issue or a pull "
        "request is unknown and it must not be answered as either. Check the installed gh version "
        "against the fields this adapter requests."
    )


def _summary(data: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    """The fields `get_issue` and `find_issues` both report, canonicalised once for both.

    Shared so the two agree key for key: a caller that filters a search result and then reads the issue
    must not meet the same status spelled twice.
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
    """`gh`'s comment list, reduced to the four fields the canonical shape carries.

    A shape this cannot read fails the read rather than shortening it — an entry that is not a comment
    object here, and via `_read_list` a `comments` field that is not a list at all — the refusal jira's
    `_comments` also makes. A thread one comment short reads exactly like a complete one, both to a caller
    and to `_artifact_gists`, which reads these same comments as the only index of which gist holds which
    artifact. An absent field is honestly no comments.
    """
    thread: list[dict[str, str]] = []
    for index, comment in enumerate(_read_list(data, "comments", "this issue's thread")):
        if not isinstance(comment, dict):
            raise TrackerError(
                f"entry {index} of the comments field read back as {type(comment).__name__}, not a comment "
                "object, so this thread cannot be read whole; it must not come back one comment short of "
                "what gh returned. Check the installed gh version against the fields this adapter requests."
            )
        thread.append({
            "id": str(comment.get("id") or ""),
            "author": _login(comment.get("author"), index),
            "created": str(comment.get("createdAt") or ""),
            "body": str(comment.get("body") or ""),
        })
    return thread


def _labels(data: dict[str, Any]) -> list[str]:
    """Label names only: the ids and colours `gh` also returns are noise to every caller.

    Both the whole field (via `_read_list`) and every entry in it must read as the issue's real labels or
    the read fails: `labels` is what a caller reads to decide whether an issue is already decomposed or
    already shipped, and a filtered or empty list reads exactly like an issue that genuinely has none. A
    name that is not a string is refused rather than coerced, since `str(name)` turned a `{"name": 3}` into
    the label `"3"`. Jira refuses each of these, and one protocol whose caller cannot see which tracker
    replied must not have two behaviours. An absent field is honestly no labels.
    """
    names: list[str] = []
    for index, label in enumerate(_read_list(data, "labels", "the labels on this issue")):
        name = label.get("name") if isinstance(label, dict) else None
        if not isinstance(name, str) or not name:
            raise TrackerError(
                f"entry {index} of the labels field read back as {_shape(label)} with no readable string "
                "name, so the labels on this issue cannot be reported whole and a filtered list would read "
                "as its real ones. Check the installed gh version against the fields this adapter requests."
            )
        names.append(name)
    return names


def _read_list(data: dict[str, Any], key: str, subject: str) -> list[Any]:
    """One `gh` field that must be a list of objects, refusing every other shape rather than emptying it.

    `_as_list`'s tolerance is load-bearing for the optional relations it also parses, so the strictness
    lives here instead, shared by `labels` and `comments`. Only the `{key: [...]}` nesting `_as_list` exists
    to unwrap is accepted, and it is checked rather than assumed: admitting *any* dict and handing it to
    `_as_list` looks equivalent and is not — `_as_list` returns `[]` for a dict it cannot address, and
    `{"nodes": [...]}`, the wrapper this file's own `_refs` treats as `gh`'s native relation list, is the
    most plausible drift of all.
    """
    field = data.get(key)
    if field is None:
        return []
    if isinstance(field, list):
        return field
    nested = field.get(key) if isinstance(field, dict) else None
    if isinstance(nested, list):
        return nested
    raise TrackerError(
        f"the {key} field read back as {_shape(field)}, not a list of {key}, so {subject} is unknown and it "
        "must not be reported as having none. Check the installed gh version against the fields this "
        "adapter requests."
    )


def _shape(value: object) -> str:
    """A payload's shape for a failure message: its keys or its type, never its content.

    Keys for an object, because the drift that matters is a list arriving under a wrapper this adapter does
    not know — `{"nodes": [...]}` — which `dict` alone names nothing actionable about. The twin of jira's
    `_shape`, so a refusal reads the same whichever adapter made it.
    """
    if isinstance(value, dict):
        return f"an object with keys {sorted(str(k) for k in value)}"
    return type(value).__name__


def _login(author: object, index: int) -> str:
    """One comment author's login, refusing a shape this adapter cannot read.

    A non-dict `author` — a plain login string, say — raised a bare `AttributeError` past every caller's
    `except TrackerError`, which is not the failure this module promises. An absent author stays honestly
    empty: `gh` omits it for a deleted account, which is not a drift.
    """
    if author is None:
        return ""
    if not isinstance(author, dict):
        raise TrackerError(
            f"entry {index} of the comments field has an author that read back as "
            f"{type(author).__name__}, not an author object, so this thread cannot be read. Check the "
            "installed gh version against the fields this adapter requests."
        )
    return str(author.get("login") or "")


def _ref(node: object) -> str | None:
    """One related issue as a reference `gh` accepts back — its URL, or its number if that is all there is."""
    # Tolerant rather than raising: the other caller reads an optional `parent`, where no node means unparented.
    if not isinstance(node, dict):
        return None
    return str(node.get("url") or node.get("number") or "") or None


def _refs(payload: object) -> tuple[list[str], int]:
    """Every related issue in a `{nodes: [...]}` relation, plus how many entries it could not name.

    Both wrappers are tolerated, because `subIssues` and `blockedBy` are recent `gh` fields whose shape has
    changed once already, and an absent relation honestly means no related issues. Every other unreadable
    shape is a failure, a dict carrying no `nodes` key included: `dependencies` is what a caller reads to
    decide whether an issue is blocked, and a shape this cannot parse must not come back as "nothing is
    blocking it". `nodes` is therefore read with no default, since `.get("nodes", [])` admits any object and
    answers "no related issues" for the drift most worth catching.

    An entry that names no issue is the one exception, and only where the shortfall can be signalled: it is
    skipped and counted when the relation carries a `totalCount` for `_relation` to report it from, and
    raises when there is none. Raising unconditionally would fail an entire `get_issue` — relations the
    caller never asked about included — over one unaddressable node, the blast radius `_item_index` already
    refuses; the invariant is narrower than "raise": no dropped node may be silently claimed complete.

    The count is returned rather than inferred from the lengths, so a `totalCount` that drifts down to agree
    with the shortened list is still reported as a drop.
    """
    if payload is None:
        return [], 0
    if isinstance(payload, dict):
        if "nodes" not in payload:
            raise TrackerError(
                f"a related-issue relation read back as {_shape(payload)}, with no nodes list to read the "
                "related issues out of, so the relations on this issue are unknown; it must not be reported "
                "as having none. Check the installed gh version against the fields this adapter requests."
            )
        nodes = payload.get("nodes")  # only after the membership check above: absent is a refusal, not []
        countable = payload.get("totalCount") is not None
    else:
        nodes, countable = payload, False
    if not isinstance(nodes, list):
        raise TrackerError(
            f"a related-issue relation read back as {_shape(nodes)}, not a list of issues, so the "
            "relations on this issue are unknown; it must not be reported as having none. Check the "
            "installed gh version against the fields this adapter requests."
        )
    refs: list[str] = []
    dropped = 0
    for index, node in enumerate(nodes):
        ref = _ref(node)
        if ref is None:
            if not countable:
                raise TrackerError(
                    f"entry {index} of a related-issue relation read back as {_shape(node)}, with no url or "
                    "number to name the issue by, and the relation carries no totalCount to report the "
                    "shortfall from, so it cannot be read whole and must not come back one issue short of "
                    "what gh returned. Check the installed gh version against the fields this adapter "
                    "requests."
                )
            dropped += 1
            continue
        refs.append(ref)
    return refs, dropped


def _relation(payload: object) -> tuple[list[str], bool]:
    """A relation's related issues, plus whether `gh` returned fewer of them than the relation holds.

    `gh` asks for one page of each of these relations and has no loop behind it — 100 sub-issues, 50
    blocked-by — but returns `totalCount` beside the nodes, so the two disagreeing is the one signal that
    the list is short: an issue with sixty blockers came back as exactly fifty, indistinguishable from one
    that really has fifty, which is what the other adapter reports as `comments_truncated`.

    An absent `totalCount` reports complete, since `gh` prints one for every relation object and the
    bare-list shape `_refs` also tolerates carries none. Every other unreadable count reports truncated, a
    `totalCount` of `"60"` included, which a clean `int` comparison would silently skip. A node `_refs`
    could not name counts as truncation too — that is what lets it skip one rather than fail the whole
    `get_issue` read — and comes from its own count rather than from `len(found)`, because a shortfall the
    count cannot see is exactly the silent completeness this pair exists to prevent.
    """
    found, dropped = _refs(payload)
    total = payload.get("totalCount") if isinstance(payload, dict) else None
    # A drop cannot reach this return: `_refs` raises rather than counting one with no count to report it.
    if total is None:
        return found, False
    return found, bool(dropped) or not isinstance(total, int) or isinstance(total, bool) or total > len(found)


def _same_ref(ref: str | None, other: str | None) -> bool:
    """Whether two issue references name the same issue, comparing `7`, `#7` and a URL alike."""
    if not ref or not other:
        return False
    # A caller may pass a number where `gh` reported a URL; a re-read must not fail a landed write over
    # spelling.
    first, second = (str(x).rstrip("/").rsplit("/", 1)[-1].lstrip("#") for x in (ref, other))
    return first == second


def _project_ref() -> tuple[str, str]:
    """The configured board as `(owner, number)`. An unusable value fails before any write.

    The owner half is checked to be shaped like one rather than merely non-empty, and that is what keeps it
    printable: it reaches the failure message of every board read, write and verification in this file, and
    a value shaped like a URL can carry userinfo, so `https://x-access-token:<token>@host/orgs/o/projects/3`
    would otherwise hand a credential to half a dozen messages with no way to recognise one. `gh --owner`
    takes a login or `@me`, and GitHub logins are alphanumerics and hyphens.
    """
    ref = str(config.get("tracker_config.project", default="") or "")
    owner, sep, number = ref.rpartition("/")
    if not sep or not number.isdigit() or not re.fullmatch(r"@me|[A-Za-z0-9][A-Za-z0-9-]*", owner):
        raise TrackerError(
            f"tracker_config.project must be <owner>/<number> (e.g. @me/3 or my-org/3); got {_safe(ref)!r}. "
            "Set it in .shipyard/config.json; see docs/github-setup.md."
        )
    return owner, number


def _confirm_board_access(scopes: list[str] | None = None) -> None:
    """Prove the configured board is reachable with this credential, by reading it.

    Run for every token, whatever its scopes say: a `repo`-only credential cannot read Projects v2 at all,
    so one successful read is positive evidence rather than an assumption, and a scope is a property of the
    credential, not of the board — a token that does report `project` still fails every board write if the
    board has since been deleted, renamed, or made invisible to it. A grant that can read the board but not
    write it is indistinguishable from here without performing a write, which a preflight must not do; that
    residual case fails later, at the write, with its own message.
    """
    owner, number = _project_ref()
    try:
        _raw_items(owner, number)
    # Only `gh` refusing the read can be a credential problem, and `scopes` decides how it is named:
    # `gh auth refresh` sends a token that already holds `project` to re-grant a scope it has.
    except _GhFailure as exc:
        fix = (
            f"The token already reports the 'project' scope ({scopes}), so check tracker_config.project "
            "against `gh project list`: a board that has been deleted, renamed or made invisible to this "
            "credential fails every board write the same way."
            if scopes is not None
            else "Grant it with `gh auth refresh -s project,read:project`, or give a fine-grained token "
            "read and write access to the board."
        )
        raise TrackerError(
            f"project {owner}/{number} could not be read with the gh token, so the Projects v2 access every "
            f"board write needs is unconfirmed: {exc} {fix}"
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
    """Every board item, canonicalised and keyed by the URL of the issue it holds.

    A card with no URL is left out rather than indexed: a `DraftIssue` legitimately has none, and nothing
    here can address a card that is not an issue or a pull request anyway. So is a card with no `content`
    object at all — Projects v2 reports an item the credential may not view as `REDACTED`, which `gh`
    renders as `content: null`, a documented board state rather than a malformed response.

    A `content` object that is present but carries neither a URL nor a content type is neither case: it is a
    shape this cannot read, and it fails here rather than going missing from a page that still reports
    itself complete.
    """
    index = {}
    for raw in _raw_items(owner, number):
        item = _normalize_item(raw)
        if not item["url"]:
            # Told apart on the raw item, before `_normalize_item` collapses `null` into `{}`: a check that
            # cannot tell them apart fails every read of the whole board over one card nobody can see.
            if not item["kind"] and _content_of(raw) is not None:
                raise _unclassifiable_card(f"board item {raw.get('id') or 'with no id'}")
            continue
        index[str(item["url"])] = item
    return index


def _content_of(raw: dict[str, Any]) -> dict[str, Any] | None:
    """A board item's `content` object, or None when the item carries nothing readable as one.

    `null`, absent and not-an-object alike, shared by the three places that read a card's content: `gh`
    renders a card the credential may not view as `content: null`, and any other non-object would otherwise
    cross the tool boundary as an `AttributeError` rather than as a `TrackerError`. `_item_index` is the only
    caller that must tell "no content object" from "an empty one", and it has the raw item to do it with.
    """
    content = raw.get("content")
    return content if isinstance(content, dict) else None


def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    """One board item, with its `Type` and `Status` option names mapped to canonical tokens.

    `kind` is the card's own content type — `Issue`, `PullRequest` or `DraftIssue` — carried through
    unmapped, because `find_issues` owes its caller issues and a board holds pull request and draft cards in
    the same columns. It is deliberately not folded into `type`, which is the board's own single-select and
    the canonical `epic`/`task`/`bug` vocabulary.
    """
    # Collapsing `null` into `{}` is what loses the distinction `_item_index` draws on the raw item.
    content = _content_of(item) or {}
    return {
        "number": content.get("number"),
        "title": content.get("title") or item.get("title"),
        "url": content.get("url"),
        "kind": content.get("type"),
        "type": canonical_type(item.get("type")),
        "status": canonical_status(item.get("status")),
    }


def _verify_field(owner: str, number: str, issue_url: str, field_name: str, option_name: str) -> None:
    """Confirm the single-select write by re-reading the board, as every write verb here evidences itself.

    `gh project item-edit` reports success whether or not the value changed, and a card that did not move is
    exactly the failure a caller cannot see, so the value is read back by name. Retried, because the board's
    item list is eventually consistent — a live run saw a card added and edited moments earlier absent from
    the very next `item-list` and present with the right value a second later — and bounded, because a
    genuinely unset field must still fail.
    """
    last = ""
    for attempt in range(VERIFY_ATTEMPTS):
        if attempt:
            time.sleep(VERIFY_BACKOFF_SECONDS * attempt)
        # strict=False: this loop already reads an incomplete board as "not yet", and a transient
        # `totalCount` disagreement raised on the first attempt would abort the retry it exists for.
        for raw in _raw_items(owner, number, strict=False):
            if (_content_of(raw) or {}).get("url") != issue_url:
                continue
            last = str(raw.get(field_name.lower()) or "")
            if last.strip().lower() == option_name.strip().lower():
                return
    raise TrackerError(
        f"{field_name} on {issue_url} still reads {last or 'unset'!r} rather than {option_name!r} after "
        f"{VERIFY_ATTEMPTS} reads of project {owner}/{number}; treat the board update as failed."
    )


def _raw_items(owner: str, number: str, *, strict: bool = True) -> list[dict[str, Any]]:
    """Every item on the board, unmapped, in one read bounded by this adapter's own `ITEM_LIMIT`.

    `ITEM_LIMIT` is not a `gh` maximum — `gh` documents none — so nothing but this read's own bound
    guarantees the list is the whole board, which is what every caller treats it as: `_board_page` reads
    `is_last` from it, the preflight reads reachability, the write-back verification reads a card's absence
    as the write not having landed. So a board larger than one read fails here, and so does a payload with no
    `items` list or a `totalCount` that is absent or not an `int` — `gh` prints one for every board, an empty
    one included, and a count of `"65"` silently skipped a check made only for a clean `int`. `items: []` is
    accepted: a board really can hold nothing.

    `strict=False` drops those completeness checks for `_verify_field` alone, which retries with backoff and
    reads a card it cannot find as "not yet" rather than as a failed write — a `totalCount` disagreeing with
    the items returned is what a mid-pagination read of a busy board transiently looks like. An unverifiable
    write stays a bounded failure there, so the search paths keep the checks: a page they call complete has
    to be.
    """
    args = ["project", "item-list", number, "--owner", owner, "--format", "json", "--limit", ITEM_LIMIT]
    data = _gh_data(args)
    raw = data.get("items") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        if not strict:
            return []
        raise TrackerError(
            f"gh {_shown(args)} returned no list of items for project {owner}/{number}, so the board could "
            "not be read and must not be reported as one with nothing on it. Check the installed gh version "
            "against the fields this adapter requests."
        )
    items = [item for item in raw if isinstance(item, dict)]
    if strict and len(items) != len(raw):
        raise TrackerError(
            f"gh {_shown(args)} returned {len(raw) - len(items)} of project {owner}/{number}'s "
            f"{len(raw)} entries as something other than an item object, so those cards cannot be read "
            "and dropping them would shorten every answer drawn from this board without saying so. "
            "Check the installed gh version against the fields this adapter requests."
        )
    total = data.get("totalCount") if isinstance(data, dict) else None
    if strict:
        if not isinstance(total, int) or isinstance(total, bool):
            raise TrackerError(
                f"gh {_shown(args)} reported project {owner}/{number}'s item count as "
                f"{type(total).__name__}, not a number, so whether this read holds the whole board cannot "
                "be checked and no answer drawn from it could honestly be called complete. Check the "
                "installed gh version against the fields this adapter requests."
            )
        if total > len(items):
            raise TrackerError(
                f"project {owner}/{number} holds {total} items but one read returned {len(items)} of them, "
                f"the most this adapter's ITEM_LIMIT of {ITEM_LIMIT} asks for, so the board cannot be read "
                "in one call and no answer derived from this read would be complete. Split the board, or "
                "archive its finished cards."
            )
    return items


def _listed_rows(args: list[str]) -> list[dict[str, Any]]:
    """`gh issue list --json`'s rows, refusing a payload it cannot read rather than calling it empty.

    The check `_raw_items` makes on the board read, made here on the sibling path a `find_issues` with no
    board filter takes: `_as_list`'s tolerance read an unparseable response as "no issues match" and
    `is_last` then reported that page complete. `gh` prints a bare array; an object carrying an `issues` list
    is tolerated in case it ever wraps one, and `[]` remains a real empty page.
    """
    payload = _gh_data(args)
    rows = payload.get("issues") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise TrackerError(
            f"gh {_shown(args)} returned no list of issues, so this search could not be read and must not be "
            "reported as a repository with nothing matching in it. Check the installed gh version against the "
            "fields this adapter requests."
        )
    objects = [row for row in rows if isinstance(row, dict)]
    if len(objects) != len(rows):
        raise TrackerError(
            f"gh {_shown(args)} returned {len(rows) - len(objects)} of {len(rows)} rows as something other than "
            "an issue object, so those issues cannot be read and dropping them would shorten this page without "
            "saying so. Check the installed gh version against the fields this adapter requests."
        )
    return objects


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


class _GhFailure(TrackerError):
    """A `gh` invocation that exited non-zero: the tool ran and refused the operation.

    A `TrackerError` either way for every caller, and subclassed only so `_confirm_board_access` can tell
    `gh` refusing a call — the shape a credential problem takes — from this adapter refusing `gh`'s answer,
    which it must not relabel as a missing scope: a board too large to read in one call, or a payload of the
    wrong shape, is not a token problem.
    """


def _gh(args: list[str]) -> str:
    """Run `gh` in the consuming repository and return its trimmed stdout. Writes nothing to stdout.

    The working directory is the repository the configuration resolved against, never this process's own:
    `_repo_args()` is empty whenever `tracker_config.repo` is unset — a documented-optional field — and an
    unqualified `gh` then resolves its target repository from wherever it runs, which for a `pixi run` launch
    is the *plugin's* checkout, so the write would land in Shipyard's repository and report success for it.
    """
    root = config.resolved_root()
    # Checked before `gh` is invoked: `subprocess.run` reports a missing cwd as `FileNotFoundError`, the same
    # class a missing binary raises, and a non-directory one as an `OSError` nothing here contracts for.
    if not root.is_dir():
        raise TrackerError(
            f"the repository the configuration resolved against, {root}, is not an existing directory, "
            "so there is nowhere to run gh. This is a configuration fault, not a gh or credential one: "
            "check CLAUDE_PROJECT_DIR and the .shipyard/ layers it points at."
        )
    try:
        # The timeout bounds a `gh` that never returns and with it the worker thread the verb offloaded;
        # DEVNULL because a child inherits the stdin carrying this server's JSON-RPC frames and would eat one.
        proc = subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=False, timeout=TIMEOUT_SECONDS,
            cwd=root, stdin=subprocess.DEVNULL,
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
        raise _GhFailure(f"gh {_shown(args)} failed: {_safe(proc.stderr)}")
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
    """The `gh` argv as a message may carry it: each element stripped, joined, then scrubbed as output is.

    Not obviously secret-bearing, but `--body` carries whatever the caller wrote and `_repo_args()` carries
    `tracker_config.repo` into every verb's argv, so both take the same redaction as stderr.
    """
    # Per element and *before* the join, though `_safe` strips too: an element is the unit the caller
    # controls, so an over-stripping fallback for one malformed authority cannot reach its neighbour.
    return _safe(" ".join(_stripped_of_credentials(arg) for arg in args))


def _stripped_of_credentials(value: str) -> str:
    """`value` with the userinfo of every credentialed authority in it dropped, credential fragments included.

    `gh --repo` accepts an https remote, an https remote can carry userinfo, and `gh`'s own stderr echoes such
    a value back for several failure shapes — so a misconfigured
    `https://x-access-token:<token>@github.com/owner/repo.git` runs through here whether it came from
    configuration or from command output. What is left — `https://host/owner/repo`, or `host:owner/repo` for
    an SSH remote — is still the value the operator has to go and fix.

    One whitespace-separated token at a time, whitespace put back, because a credentialed reference is rarely
    the whole value: real `gh` (2.96.0) quotes one *inside a sentence* and a `--body` carries whatever the
    caller wrote around one, so matching the whole value missed every prose case. A token cannot contain
    whitespace, so several credentialed references in one line each strip independently. Punctuation around
    one is not preserved — dropping `gh`'s opening quote with the userinfo is a cosmetic cost paid to remove
    the secret.

    Adversarially malformed userinfo is not guaranteed to be stripped in full; real-world credential shapes
    are. A userinfo holding a raw, unencoded `/` is not RFC 3986 (it would be `%2F`), and no genuine remote or
    token an operator configures carries one, so `/` is read as the path boundary it is in every compliant
    value rather than guessed at.
    """
    return "".join(piece if piece.isspace() else _stripped_token(piece) for piece in re.split(r"(\s+)", value))


def _stripped_token(token: str) -> str:
    """One whitespace-free piece of a value, with the userinfo of the credentialed authority in it dropped.

    A token opening an authority with `//` is bounded before the credential is looked for, which a `re.sub`
    could not do: `_stripped_authority` cuts the authority component out first, and only the last `@` *inside*
    it separates userinfo from host, so an extra `@` in the userinfo carries no fragment of itself through.

    Any other token is matched against three schemeless remote spellings: `user:pass@host:path` and
    `user:pass@host/path`, greedy to the last `@` for that same reason; and `user:pass@host` with no path or
    port at all — the bare RFC 3986 authority `git credential fill`/`.netrc` hand back and a base-host paste
    against `--repo`'s `HOST/OWNER/REPO` grammar produces — which needs a colon inside the userinfo to tell it
    from a bare `user@host` address. The userinfo must be non-empty, so `@me` and `@me/abc` are left exactly as
    configured, and a host followed by a bare `:` is not the scp form, so an address in a comment body
    (`someone@example.com: ...`) is not read as a credential. Anything else — `OWNER/REPO`, a flag, an email
    address — is returned unchanged.

    An address immediately followed by a path (`someone@example.com/team`) *is* the schemeless spelling and
    loses its local part: the two are not distinguishable by shape, and a mangled address in a comment body
    costs less than a token in one.
    """
    if "//" in token:
        head, *segments = token.split("//")
        return head + "".join(f"//{_stripped_authority(segment)}" for segment in segments)
    return re.sub(r"\S+@(?=[^\s@:/]+(?:/|:\S))|\S*:\S*@(?=[^\s@:/]+$)", "", token)


def _stripped_authority(segment: str) -> str:
    """One `//`-opened piece of a value, with the userinfo of the authority it opens with dropped.

    The authority is everything up to the first `/`, `?` or `#`, and the host is what follows its last `@`. A
    segment whose host is then not a host at all falls back to dropping everything before the segment's last
    `@` — over-stripping a malformed value rather than printing what may be a password with a slash in it.
    """
    boundary = min((cut for cut in (segment.find(char) for char in "/?#") if cut != -1), default=len(segment))
    authority, tail = segment[:boundary], segment[boundary:]
    host = authority[authority.rfind("@") + 1 :]
    if re.fullmatch(r"[^\s/?#@:]+(?::\d*)?", host):
        return host + tail
    return segment[segment.rfind("@") + 1 :] if "@" in segment else segment


def _safe(text: str) -> str:
    """`text` as a message may carry it: credentials this process holds redacted, URL userinfo dropped.

    Three passes, and none can do the others' job. Scrubbing catches the credentials this process holds in its
    own environment, honouring `redaction.extra_words` so an org-specific credential name redacts here exactly
    as on the attach-artifact sanitisation path. Stripping catches a credential this process cannot recognise
    by value, by its shape in free text. Neither reaches a bare `<token>@host` with no path, port or colon in
    it, which is shape-identical to a real email address: no pattern can tell those two apart in *free* text
    without also mangling every genuine address a `gh` error or a comment body carries.

    The third pass sidesteps that ambiguity by not pattern-matching at all. `tracker_config.repo` and
    `.project` are held directly, not inferred from arbitrary output, so any `@` in either — other than the
    literal `@me`/`@me/...` self-reference — is unambiguously a credential boundary *in this narrow, known
    context*, the way scrubbing already treats an environment variable's value as sensitive for what it is
    rather than what it looks like. Its exact prefix is registered for the same literal-value replacement, so
    the schemeless and colon-less shapes the strip cannot safely touch are caught by matching the value itself;
    a scheme-bearing echo is already the strip's, scheme-case-agnostically. Below `DEFAULT_MIN_LENGTH` no
    prefix is registered at all: a degenerate one- or two-character value would otherwise redact every short,
    unrelated `@`-bearing substring a message happens to carry.

    Every message built from `gh` output, from `_shown`'s argv rendering, or from a configured
    `tracker_config.repo`/`.project` value goes through here, the strip included rather than left to each site:
    `gh`'s own stderr echoes a credentialed repository value back for a GraphQL resolution error, an HTTP error
    naming the URL, and a malformed-URL argument error, so the site-by-site version of this leaked from
    whichever verb was fixed second. So do the two messages that echo caller-supplied free text — a title and
    a label — since a title assembled from command output can carry a token exactly as a body can, whatever a
    caller usually pastes; the server scrubs those two fields on the way in, and this is the second half. Every
    other interpolation here is a `gh`-returned or configured identifier, which is never a token's home.
    """
    secrets = dict(discover_secret_vars(extra_words=config.extra_secret_words()))
    configured_paths = (
        ("TRACKER_CONFIG_REPO", "tracker_config.repo"),
        ("TRACKER_CONFIG_PROJECT", "tracker_config.project"),
    )
    for name, path in configured_paths:
        prefix = _configured_secret_prefix(path)
        if prefix and len(prefix) >= DEFAULT_MIN_LENGTH:
            secrets[name] = prefix
    scrubbed, _ = scrub_text(_stripped_of_credentials(text.strip()), secrets)
    return scrubbed[:STDERR_LIMIT]


def _configured_secret_prefix(path: str) -> str | None:
    """The credential-bearing prefix of a configured repository/project reference, or `None` without one.

    Read directly from config rather than matched against arbitrary text, which is what lets this catch the one
    shape no pattern safely can: a bare `<token>@host` is indistinguishable from an email address in free text,
    but these two values are never legitimately an email address, so any `@` in one is a credential boundary —
    except the literal `@me` or `@me/...` self-reference, `tracker_config.project`'s one legitimate use of `@`.

    The carve-out does not generalise beyond that one literal form: a typo like `myorg@3` is treated as a
    credential too and reported redacted rather than verbatim, hiding the very value the operator needs to see
    to fix it. That is this file's trade everywhere in this direction — over-redacting an ambiguous value costs
    a confusing message, not a leak.
    """
    value = str(config.get(path, default="") or "")
    if not value or value == "@me" or value.startswith("@me/"):
        return None
    at = value.rfind("@")
    return value[: at + 1] if at != -1 else None
