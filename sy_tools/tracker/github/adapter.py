"""GitHub tracker adapter, spoken to only through `sy_tools.tracker.adapter()`.

This is the implementation of every canonical verb of `skills/tracker/CONTRACT.md` for this tracker,
and the `gh` transport it speaks them over. Two properties come from where it runs rather than from
`gh`: it never writes to stdout (that stream carries JSON-RPC frames, so one stray line
desynchronises the client), and a failure raises `TrackerError` instead of exiting, because this
process has other calls to serve after a bad one.

Issue `Type` and `Status` are Projects v2 single-select fields, which `gh issue` cannot touch at all,
so the board is resolved here: resolve, look the option up case-insensitively, and on a miss
re-resolve once before failing, so a column added minutes ago works without a restart. The resolved
board is cached in a per-instance dict, and the `item-edit` response is deliberately not parsed —
a human-readable success line must not turn a completed write into a failure.

The attachment verbs are the deliberate asymmetry `skills/tracker/github/ADAPTER.md` documents: this
tracker has no scriptable file attachment, so an artifact becomes a secret gist that a comment on the
work item links to, and the lifecycle verbs act on that gist through the link the comment carries.
Privacy is verified by reading the created gist back, not assumed from the flags passed: a public
gist would publish a transcript irrevocably.

Credentials are `gh`'s own business. Nothing here reads, passes, or echoes a token, and every message
built from command output or from a configured value goes through `_safe`, which both scrubs the
credentials this process holds and drops the userinfo a configured remote URL can carry — a credential
this process does not hold and so cannot recognise by value. Both halves live in that one function
rather than at each site, because the site-by-site version leaked twice: first from the argv every
verb renders, then from the stderr `gh` echoes a credentialed repository value back in.

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

`limit` bounds the reads by itself while a board value is the only filter, but `text` and `parent` are
matched against the read, so a filter that matches nothing reads every remaining candidate: one
subprocess each, bounded individually by `TIMEOUT_SECONDS` and in aggregate by nothing. Past this many
the search fails and says how to narrow itself, because a query that spends minutes and then reports
nothing is indistinguishable to its caller from a board that has nothing on it."""

ARTIFACT_COMMENT = "Shipyard artifact `{filename}`: {url}\n\nSecret gist — reachable only from this link."
ARTIFACT_LINK = re.compile(r"Shipyard artifact `([^`\n]+)`: (https://\S+)")
"""The comment `attach-artifact` writes, and the pattern the lifecycle verbs read it back with.

The template and the pattern are one pair on purpose. This tracker stores nothing that links an issue to
its artifact except that comment, so the comment *is* the attachment index: `attachment-download` and
`-update` each find their gist by matching it. Written at one site and matched at another, the two drift
the first time the wording changes, and the symptom is every lifecycle verb reporting that an issue has
no artifacts while its transcripts sit in gists nobody can find again."""

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
        board becomes the candidate set: every matching issue card in the scoped repository is
        enumerated, `limit` bounds the page rather than the fetch, `is_last` says whether a further
        match exists, and `text` is matched here against title and body instead of by GitHub's
        server-side search. A query too wide to read within `MAX_BOARD_READS` fails saying so; so does a
        status or type this cannot match a card by — an unrecognised canonical token, or one whose
        configured board name the board itself does not offer — rather than answering an empty page.
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

    async def type_convert(self, issue: str, issue_type: str) -> dict:
        """Change `issue`'s board `Type` to canonical `issue_type`, verified by re-reading the card."""
        return await to_thread.run_sync(self._sync_type_convert, issue, issue_type)

    async def attachment_download(self, issue: str, filename_or_id: str, output_path: Path) -> dict:
        """Write the artifact gist `filename_or_id` names on `issue` to `output_path`, off the event loop."""
        return await to_thread.run_sync(self._sync_attachment_download, issue, filename_or_id, output_path)

    async def attachment_update(self, issue: str, path: Path) -> dict:
        """Replace the contents of `issue`'s artifact gist named `path.name` with `path`, off the event loop."""
        return await to_thread.run_sync(self._sync_attachment_update, issue, path)

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
        separately: `Status` and `Type` live on the card, not on the issue.
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
        is GitHub's, and `is_last` is read from whether that page came back full — from every row `gh`
        returned, because a response this cannot read fails in `_listed_rows` rather than shortening the
        page that `is_last` is then computed from. A row carrying no URL fails here for the same reason it
        fails a `get_issue` and a board-path read: the URL is the `id` every other verb takes, so such a
        row is an unaddressable entry that `_checked_ref` would reject on the caller's next call, not an
        issue that was found.

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

        Three properties of that board path are the caller's to rely on. Only issues come back, never a
        pull request or draft card sharing the column. The page is scoped to one concrete repository —
        `tracker_config.repo`, or the repository `gh` resolves from the working directory, in either case
        resolved by `gh` itself so any spelling `--repo` accepts names the same one — and never to the
        board at large. And the per-candidate reads are bounded by `MAX_BOARD_READS`, past which the call
        fails with what to narrow instead of returning a page it cannot honestly call complete.
        """
        if limit <= 0:
            raise TrackerError(f"limit must be a positive number of issues, got {limit}")
        if status is not None or issue_type is not None:
            return self._board_page(status=status, issue_type=issue_type, parent=parent, text=text, limit=limit)
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

        The option lookup is `_checked_option` rather than inline, because the board-filtered read has to
        make the very same check against the very same board fields before it matches a card by one of
        them. Every write of a board value — `Status`, and the `Type` both `create-issue` and
        `type-convert` set — goes through here, so the read-back verification cannot be skipped by one.
        """
        owner, number = _project_ref()
        resolved, (field_id, option_id) = self._checked_option(owner, number, field_name, option_name)
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

    def _checked_option(
        self, owner: str, number: str, field_name: str, option_name: str, *, context: str = ""
    ) -> tuple[dict[str, Any], tuple[str, str]]:
        """The board and the `(field_id, option_id)` for one native option name, or a failure naming the drift.

        Shared by the write path with the read filter because the two meet the same fault: the board's real
        `Status` column or `Type` option name has drifted from the `columns.*` config key naming it —
        renamed, retyped, or never created. Only the write path used to notice. `_board_page` compared
        canonical tokens against values `canonical_status` had passed through unmapped, so the same drift
        filtered every card out and `find_issues` answered `count: 0, is_last: true` from a board with work
        on it, which is the one wrong answer a caller cannot tell from an empty queue. `context` is what the
        read path adds to say the query was refused rather than answered.

        The refresh retry is the load-bearing part for both: an option added to the board after this board
        was resolved must work without restarting the server, so a lookup miss re-resolves once before it
        is treated as a real miss.
        """
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
                f"key. See docs/github-setup.md.{context}"
            )
        return resolved, ids

    def _board_page(
        self, *, status: str | None, issue_type: str | None, parent: str | None, text: str | None, limit: int
    ) -> dict:
        """One page of the board items matching a status or type filter, read board-first.

        The board item list is the candidate set: it is a project-item read bounded by the board's own
        size, it carries `Status` and `Type`, and it never touches `gh issue list` or the Search API. Only
        the surviving cards are then read individually for the fields a card does not carry — the labels
        and parent `_summary` reports, plus the body a `text` filter needs — so the per-issue cost scales
        with the filtered board, not with the repository.

        Candidates are narrowed to actual issues in one concrete repository before any of them is read. A
        board holds pull request and draft cards too, and `gh issue view` reads a pull request URL without
        complaint, so a PR sitting in the filtered column would otherwise be returned as an issue; and the
        repository is always exactly the one every other verb acts on — `tracker_config.repo` when it is
        set, otherwise the one `gh` resolves from the working directory — never the whole board.

        Reading stops one match past the page: `is_last` needs to know only whether a further match
        exists, and each further candidate costs a `gh issue view` a caller would never see. `MAX_BOARD_READS`
        bounds those reads for the case `limit` cannot: a `text` or `parent` filter that matches nothing
        rejects every candidate after reading it, and past the bound this stops rather than spending a minute
        per few hundred cards. Reaching the bound with a full page returns that page as the truncated page it
        is, `is_last` false; reaching it without one fails, saying what to narrow, because a page that is
        neither full nor known to be complete is exactly the answer a caller cannot act on.

        `text` is therefore matched here, case-insensitively, as a substring of title or body. That is a
        deliberate divergence from `gh issue list --search`: no attempt is made to reproduce GitHub's
        server-side ranking or query syntax, in exchange for a result set that is complete for the board
        rather than silently capped at the Search API's thousandth row.

        The filter is checked in two places, because matching is what makes both checks necessary — an
        unmatchable filter simply matches no card, and every miss came back as a complete empty page.
        `native_status`/`native_type` reject an unrecognised *canonical* token before any `gh` call, the way
        every write here does: `in_progress` for `in-progress` used to answer `count: 0, is_last: true`, which
        the duplicate-work checks in `skills/plan/SKILL.md` and `skills/spec/SKILL.md` read as "no prior work".
        `_checked_option` then rejects a native name the board does not actually offer, against the board's own
        field options — the same read and the same failure the write path makes. That is the drift the
        comparison below cannot see: it is canonical-to-canonical, over values `_normalize_item` produced, and
        `canonical_status`/`canonical_type` pass a board value no configured column names through unchanged, so
        a renamed column matches nothing and the empty page reports itself complete.

        That check comes after the board read rather than before it, so a board this adapter cannot read
        completely keeps its own failure: telling an operator to fix a column name over a board too large for
        one read sends them to correct configuration that is already right.
        """
        wanted = {
            STATUS_FIELD: native_status(status) if status is not None else None,
            TYPE_FIELD: native_type(issue_type) if issue_type is not None else None,
        }
        owner, number = _project_ref()
        repo = _effective_repo()
        index = _item_index(owner, number)
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
            if len(matched) > limit:
                break
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
        gist_id = _gist_id(gist_url)
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
        `_set_field` and inherits that path's bounded read-back: `gh project item-edit` reports success
        whether or not the value changed. The type is mapped before anything is written, so an unknown
        canonical token cannot leave a card half-converted.

        The issue is resolved to a URL first because board items are keyed by their content URL: an issue
        number identifies nothing on the board.
        """
        option = native_type(issue_type)
        url = _url_of(issue)
        self._set_field(url, TYPE_FIELD, option)
        return {"id": url, "type": issue_type, "native": option}

    def _sync_attachment_download(self, issue: str, filename_or_id: str, output_path: Path) -> dict:
        """Write the artifact gist `filename_or_id` names on `issue` to `output_path`.

        `--filename` is not optional here, and leaving it off is not a smaller version of this call:
        `gh gist view --raw` with no filename prepends the gist's *description* and a blank line to the
        output, so it wrote a corrupted artifact — the description text, then the transcript — while
        reporting a successful download. Naming the file returns exactly that file's bytes and nothing
        else, on a multi-file gist as well as a single-file one. Measured against a real gist, because
        this is the shape an offline fake reproduces only if it already knows about it: the mocked
        transport answered the description-less content for the same argv, so the tests agreed with the
        bug until a live run disagreed with both.

        Empty output is a failure rather than a zero-byte file: on disk the two are indistinguishable,
        and only one of them is an artifact.

        The bytes written are the artifact's text, not a byte-exact copy: `_gh` trims the output it
        returns, so a trailing newline is not preserved. That is stated rather than worked around because
        every other verb in this file goes through `_gh` for the timeout and the credential scrubbing it
        applies, and a transcript is read for its content.
        """
        issue = _checked_ref(issue)
        gist_id, filename, _ = _resolve_gist(issue, filename_or_id)
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

        `gh gist edit <id> --filename <name> <source>` replaces one file in a gist from a local file,
        which is a real in-place replace: the gist id and therefore the URL in the comment survive, so the
        link on the work item keeps pointing at the current artifact and no second comment is posted. That
        is the flag semantics `gh 2.96.0` documents for itself, not an assumption — `--add` adds a *new*
        file, and adding one under a name the gist already carries is not what replace means here.

        Verified by reading the gist back and comparing what it now holds against the file that was sent,
        because `gh gist edit` prints nothing and exits zero either way. The read-back names the file for
        the same reason the download does: without `--filename`, `gh gist view --raw` prepends the gist's
        description, so the comparison could never match and a replacement that had landed was reported as
        unconfirmed. The comparison ignores surrounding whitespace only, since `_gh` trims its output.

        An issue carrying no artifact of that name is a first upload, not an error: the verb's contract is
        replace-by-filename with zero existing explicitly fine, and routing that through the resolver's
        exactly-one rule made this adapter refuse every first `attachment-update` where the other adapter
        performs one. So it does what a fresh attach does — the same gist, privacy re-read and linking
        comment — and reports `replaced: 0`, which is how a caller tells a supersede from an upload.
        """
        issue = _checked_ref(issue)
        if not path.is_file():
            raise TrackerError(f"artifact not found: {path}")
        try:
            intended = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise TrackerError(
                f"{path.name} is not UTF-8 text, and a gist holds text: this tracker has no binary "
                "attachment, so there is nothing to replace it with. Attach the artifact as text."
            ) from None
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
        _gh(["gist", "edit", gist_id, "--filename", filename, str(path)])
        if _gh(["gist", "view", gist_id, "--raw", "--filename", filename]).strip() != intended.strip():
            raise TrackerError(
                f"gist {gist_id} does not read back as the contents of {path.name} after the edit, so the "
                f"replacement is unconfirmed and {issue} may still link the previous artifact."
            )
        return {"issue": issue, "filename": filename, "id": gist_id, "gist_url": gist_url, "replaced": 1}

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


def _gist_id(gist_url: str) -> str:
    """The gist id in a gist URL: the last path segment, which is what every `gh gist` call takes."""
    return gist_url.rstrip("/").rsplit("/", 1)[-1]


def _artifact_gists(issue: str, filename_or_id: str) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    """`(the artifacts on `issue` that `filename_or_id` selects, every artifact its comments record)`.

    The issue's own comments are the index: `attach-artifact` records the artifact's filename and the gist
    URL in one, and nothing else on this tracker ties the two together. So the comments are read and
    matched, by gist id first and then by exact filename.

    Both lists come back from the one read, because how many matched decides the outcome and everything
    recorded is what a failure has to list. Each entry is `(filename, gist_id, gist_url)`. Two comments
    naming the same filename *and* the same gist are one artifact recorded twice, so they collapse rather
    than reading as an ambiguity a caller cannot resolve.
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
    """The refusal for a name that selects other than one artifact, listing what the comments do record.

    The listing goes through `_safe`: it is built from comment bodies, which carry whatever a caller wrote.
    """
    listing = ", ".join(f"{filename} -> {gist_id}" for filename, gist_id, _ in (matches or links))
    return TrackerError(
        f"{len(matches)} artifacts on {issue} match {filename_or_id!r}; expected exactly one, so pass the gist "
        f"id to name the one you mean. The artifact comments on {issue} record: {_safe(listing) or 'none'}"
    )


def _resolve_gist(issue: str, filename_or_id: str) -> tuple[str, str, str]:
    """The one artifact gist `filename_or_id` names on `issue`, as `(gist_id, filename, gist_url)`.

    Exactly one match, or a failure — the same rule the other adapter applies to attachments sharing a
    filename, and for the same reason: attaching one transcript twice leaves two gists under one name,
    and picking either would download or irrevocably delete an arbitrary one of them. Zero is a failure
    too, for the verbs that have nothing to act on without a match; `_find_gist` is the one for the verb
    where zero is a legitimate answer.
    """
    matches, links = _artifact_gists(issue, filename_or_id)
    if len(matches) != 1:
        raise _not_one_artifact(issue, filename_or_id, matches, links)
    filename, gist_id, gist_url = matches[0]
    return gist_id, filename, gist_url


def _find_gist(issue: str, filename_or_id: str) -> tuple[str, str, str] | None:
    """`_resolve_gist`, returning None instead of raising when the issue records no such artifact.

    `attachment-update` is replace-by-filename with zero existing explicitly fine — a plain first upload —
    so a no-match there is a decision for the caller, not a failure. More than one still raises: which of
    two namesakes to overwrite is exactly the question nothing here can answer.
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

    A board may span repositories while this search is repo-scoped, so another repo's card is skipped
    before it is read rather than after `gh` refuses it: dropping a candidate because a read failed is
    how a truncated result comes back looking complete.

    Only `gh`'s own output for a board card is parsed here — always `<host>/<owner>/<repo>/issues/<n>` —
    so the two path segments before the `issues/<n>` tail are the pair, lowercased because GitHub's names
    are case-insensitive and `_effective_repo` lowercases the side this is compared against. The
    repository the *caller* spelled is not parsed here or anywhere in this file; `gh` resolves that one.

    `""` means the URL holds no pair to compare, which `_in_repo` refuses rather than reads as "some other
    repository".
    """
    parts = url.rstrip("/").split("/")
    pair = [part for part in parts[-4:-2] if part] if len(parts) >= 4 else []
    return "/".join(pair).lower() if len(pair) == 2 else ""


def _in_repo(url: str, repo: str) -> bool:
    """Whether a board card's issue URL belongs to `repo`, refusing a URL holding no pair to compare.

    Found by review, sweeping for the pattern this path keeps growing: a card whose URL `_repo_slug` could
    not read came back as `""`, which equals no repository, so the card was dropped from the candidate set
    exactly as another repository's card is — and the page still reported itself complete. That is the same
    silent shortening `_item_index`, `_raw_items` and `_listed_rows` each refuse one level up, and it also
    took the `_is_issue_card` check out of reach for such a card, since this comparison runs first.

    Every URL that reaches here is one `gh` printed for a board card, always
    `<host>/<owner>/<repo>/issues|pull/<n>`, so a URL with no pair in it is shape drift rather than a card
    belonging somewhere else.
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

    `_repo_args()` sets the pattern every write here follows: `tracker_config.repo` when it is set, and
    otherwise whatever repository `gh` resolves from the working directory. A search has to answer about
    the same one, so an unset value resolves that repository rather than widening the search to the whole
    board: a page mixing another repo's cards into this repo's queue is read as this repo's queue.

    Both branches ask `gh` rather than parsing the value here. `gh --repo` accepts `OWNER/REPO`,
    `HOST/OWNER/REPO`, an https URL with or without a `.git` suffix, and an scp-like SSH remote, and every
    hand-written parser of that grammar was one spelling short of it — each miss answering `count: 0` from
    a board full of the configured repo's cards, the one wrong answer a caller cannot tell from an empty
    queue. `gh repo view` takes exactly the references `--repo` takes and prints the canonical pair, so
    the two sides of the comparison agree by construction instead of by this file keeping up with `gh`.

    An unresolvable value is a failure rather than a fallback to board-wide, and `gh` refusing it is that
    check: a value which is not a repository reference at all — an issue URL, say — is refused there
    instead of being normalised into something that matches no card while the page reports itself
    complete. `gh`'s answer is then checked to be one `owner/repo` pair for the same reason: a repository
    this cannot compare a card against is as invisible to a caller as a board with nothing on it.

    The value reaches `gh` unparsed but not unchecked: one starting with `-` is refused here, because it
    lands in `gh`'s argv as a bare positional and `gh` would read it as a flag — the hazard `_checked_ref`
    exists for on the issue-reference side, on a value that arrives from configuration instead of a caller.

    It also reaches no message unstripped. An https remote can carry userinfo, so the failures that name
    the value go through `_safe`, which drops it, and the `gh` failure this one wraps is safe for the same
    reason one round further in: `_shown` strips every argv element and `_safe` strips `gh`'s own stderr,
    so neither the value `_repo_args()` hands to every other verb nor the text `gh` echoes it back in can
    print its token.
    """
    configured = str(config.get("tracker_config.repo", default="") or "")
    if configured.strip().startswith("-"):
        raise TrackerError(
            f"tracker_config.repo is set to {_safe(configured)!r}, which gh would read as a flag rather "
            "than a repository reference, so it is refused before gh is called. Set it to OWNER/REPO in "
            ".shipyard/config.json; see docs/github-setup.md."
        )
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

    `find_issues` owes its caller issues, and `gh issue view` reads a pull request URL happily enough
    that a PR card in the filtered column comes back looking like one — which the duplicate-work checks
    in `skills/plan/SKILL.md` and `skills/spec/SKILL.md` then read as prior work on the issue.

    A card whose content type is missing altogether is a failure rather than a guess in either direction:
    calling it an issue reports a PR as one, and calling it not an issue drops a real issue from a page
    that still reports itself complete.
    """
    kind = str(item.get("kind") or "")
    if not kind:
        raise _unclassifiable_card(url)
    return kind.strip().lower() == "issue"


def _unclassifiable_card(ref: str) -> TrackerError:
    """The failure a board card carrying no content type gets, shared by both places that can see one.

    Returned rather than raised so each caller raises it at its own site, and shared so the two cannot
    drift: `_item_index` meets such a card when its content object also carries no URL, `_is_issue_card`
    when it carries one, and both are the same unreadable shape rather than two different problems.
    """
    return TrackerError(
        f"the board reports no content type for {ref}, so whether that card holds an issue or a pull "
        "request is unknown and it must not be answered as either. Check the installed gh version "
        "against the fields this adapter requests."
    )


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
    """The configured board as `(owner, number)`. An unusable value fails before any write.

    The owner half is checked to be shaped like one, not merely non-empty: `gh --owner` takes a login or
    `@me`, and GitHub logins are alphanumerics and hyphens, so anything else is a misconfiguration that
    `gh` would refuse anyway. Checking it here is what keeps the owner printable — it reaches the failure
    message of every board read, write and verification in this file, and a value shaped like a URL can
    carry userinfo, so `https://x-access-token:<token>@host/orgs/o/projects/3` would otherwise hand a
    credential to half a dozen messages that have no way to recognise one. The single message that does
    print the raw configured value goes through `_safe`, which strips it.
    """
    ref = str(config.get("tracker_config.project", default="") or "")
    owner, sep, number = ref.rpartition("/")
    if not sep or not number.isdigit() or not re.fullmatch(r"@me|[A-Za-z0-9][A-Za-z0-9-]*", owner):
        raise TrackerError(
            f"tracker_config.project must be <owner>/<number> (e.g. @me/3 or my-org/3); got {_safe(ref)!r}. "
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

    Only `gh` refusing the read is relabelled as the scope problem, because only that shape can be one.
    Everything else `_raw_items` can raise — a board larger than one read, a payload it will not parse as
    a board, a `gh` that is missing or hung — keeps its own message: telling an operator to grant the
    `project` scope over a board too large to read sends them to fix a credential that is already correct.
    """
    owner, number = _project_ref()
    try:
        _raw_items(owner, number)
    except _GhFailure as exc:
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
    """Every board item, canonicalised and keyed by the URL of the issue it holds.

    A card with no URL is left out rather than indexed: a `DraftIssue` legitimately has none, and nothing
    here can address a card that is not an issue or a pull request anyway.

    A card carrying a `content` object with neither a URL nor a content type inside it is not that case —
    it is a shape this cannot read — and it fails here instead of being dropped as though it were a draft.
    Dropping it silently is how a card goes missing from a page that still reports itself complete, and it
    also took the same failure `_is_issue_card` raises out of reach: every url-less card was gone before
    that check ever saw one.

    A card with no `content` object at all is the third case, and it is neither of those: Projects v2
    reports an item the credential may not view as `REDACTED`, which `gh` renders as `content: null`, so
    such a card is a documented board state and not a malformed response. It is skipped like a draft —
    silently, because it carries no URL for any caller to have acted on and no verb here could address it
    if it did — and the raise is reserved for a `content` object that is present but says nothing. The two
    are told apart on the raw item, before `_normalize_item` collapses `null` into `{}`: a check that could
    not tell them apart failed every read of the whole board over one card nobody can see.
    """
    index = {}
    for raw in _raw_items(owner, number):
        item = _normalize_item(raw)
        if not item["url"]:
            if not item["kind"] and _content_of(raw) is not None:
                raise _unclassifiable_card(f"board item {raw.get('id') or 'with no id'}")
            continue
        index[str(item["url"])] = item
    return index


def _content_of(raw: dict[str, Any]) -> dict[str, Any] | None:
    """A board item's `content` object, or None when the item carries nothing readable as one.

    The three places that read a card's content — the board index, the board-item lookup a write does,
    and the write's verifying re-read — all have to survive a `content` that is not an object: `gh`
    renders a card the credential may not view as `content: null`, and any future non-object would
    otherwise cross the tool boundary as an `AttributeError` rather than as a `TrackerError`. Shared
    rather than repeated, because two of the three sites were fixed a round apart from the first.

    None is `null`, absent, and not-an-object alike; `_item_index` is the only caller that needs to tell
    "no content object" from "an empty one", and it has the raw item to do it with.
    """
    content = raw.get("content")
    return content if isinstance(content, dict) else None


def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    """One board item, with its `Type` and `Status` option names mapped to canonical tokens.

    `kind` is the card's own content type — `Issue`, `PullRequest` or `DraftIssue` — carried through
    unmapped, because `find_issues` owes its caller issues and a board holds pull request and draft cards
    in the same columns. It is deliberately not folded into `type`, which is the board's own single-select
    and the canonical `epic`/`task`/`bug` vocabulary.

    A `content` that is anything but an object reads as no content, rather than being asked for keys it
    has no `get` for: `null` is what `gh` renders a card the credential may not view as, and any other
    non-object would otherwise leave an `AttributeError` — not a `TrackerError` — crossing the tool
    boundary. `_item_index` is where the distinction between "no content object" and "an empty one" is
    drawn, on the raw item, because this collapse is exactly what loses it.
    """
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
    """Confirm the single-select write by re-reading the board, per CONTRIBUTING's write discipline.

    `gh project item-edit` reports success whether or not the value changed, and a card that did not
    move is exactly the failure a caller cannot see, so the value is read back by name.

    The read is retried because the board's item list is eventually consistent, which a live run
    caught: a card added and edited moments earlier can be entirely absent from the very next
    `item-list`, then present with the right value a second later. Failing on the first read turns a
    write that landed into a reported failure, so the retry is what makes read-back verification
    usable here at all — but it stays bounded, because a genuinely unset field must still fail.

    The read is therefore asked for without `_raw_items`' completeness checks: this loop already treats an
    incomplete read as "not yet, read again", and a transient `totalCount` disagreement raised on the first
    attempt would abort the very retry that exists to absorb it — reporting a landed write as failed, which
    is the failure this whole function is built to avoid.
    """
    last = ""
    for attempt in range(VERIFY_ATTEMPTS):
        if attempt:
            time.sleep(VERIFY_BACKOFF_SECONDS * attempt)
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
    guarantees the list is the whole board, and every caller treats it as the whole board: `_board_page`
    reads `is_last` from it, the preflight reads reachability from it, and the write-back verification
    reads a card's absence from it as the write not having landed. `gh` returns `totalCount` for the board
    alongside the truncated `items`, so a board larger than one read fails here rather than answering
    completely from its first `ITEM_LIMIT` cards.

    That count has to be readable as a number for the check to mean anything, so one that is absent or of
    any other type fails too. `gh project item-list --format json` prints `totalCount` for every board,
    including an empty one, so an unreadable count is shape drift rather than a `gh` version that omits it —
    and making the completeness check only for a clean `int` is the same false completeness the check
    exists to prevent, reintroduced one type away: a `totalCount` of `"65"` skipped it silently.

    A payload carrying no `items` list is that same failure and not an empty board. `_as_list` is
    deliberately tolerant, because the relations it also parses are legitimately absent; a board this
    could not read must not come back looking like a board with nothing on it, which reads identically to
    a genuinely empty one. `items: []` is accepted: a board really can hold nothing.

    `strict=False` drops those completeness checks for `_verify_field` alone, which already tolerates an
    incomplete read: the board's item list is eventually consistent, so that caller retries with backoff
    and treats a card it cannot find as "not yet", never as "the write failed". A `totalCount` that
    disagrees with the items returned is exactly what a mid-pagination read of a busy board can transiently
    look like, and raising on it inside that retry loop aborted the retry on its first attempt and reported
    a write that had landed as a failure. The read still cannot make that caller optimistic — an
    unverifiable write stays a bounded failure with its own message — so the tolerance costs nothing the
    search paths need, and they keep the checks, because a page they call complete has to be.
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
    board filter takes. `_as_list` is tolerant by design and stays so — the optional relations it also
    parses are legitimately absent — but tolerance here read an unparseable response as "no issues match",
    and `is_last` then reported that page complete: the same false completeness the board path has now been
    fixed for four times, on the one path it was never applied to. `gh` prints a bare array; an object
    carrying an `issues` list is tolerated in case it ever wraps it, and `[]` remains a real empty page.
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

    A `TrackerError` either way for every caller, and subclassed only so one of them can tell `gh`
    refusing a call — the shape a credential problem takes — from this adapter refusing `gh`'s answer.
    `_confirm_board_access` relabels the first as a missing scope and must not relabel the second: a
    board too large to read in one call, or a payload of the wrong shape, is not a token problem, and
    naming it as one sends whoever reads the preflight to `gh auth refresh` over an unrelated fault.
    """


def _gh(args: list[str]) -> str:
    """Run `gh` in the consuming repository and return its trimmed stdout. Writes nothing to stdout.

    The working directory is the repository the configuration resolved against, never this process's
    own. `_repo_args()` is empty whenever `tracker_config.repo` is unset — a documented-optional
    field — and an unqualified `gh` call then resolves its target repository from wherever it runs.
    That is not where the server runs: a `pixi run <declared-task>` launch puts the process in the
    *plugin's* checkout, which has an `origin` of its own, so an unqualified write would land in
    Shipyard's repository rather than the consumer's and report success for it. Pinning `cwd` to the
    resolved root makes the two agree by construction, config-configured repo or not.

    That `cwd` is checked before `gh` is invoked rather than left to `subprocess.run`, which reports a
    `cwd` that does not exist as `FileNotFoundError` — the same class a missing `gh` binary raises, so
    a resolved root pointing at nothing would be reported as "install the GitHub CLI" and send whoever
    read it after the wrong fix — and a `cwd` that exists but is not a directory as `NotADirectoryError`,
    an `OSError` this adapter never contracted to let escape. Checked here, `FileNotFoundError` below
    can only mean the binary.

    The timeout bounds a `gh` that never returns — a network stall, or a credential helper
    prompting on a stdin no one is answering — because this process has other calls to serve.
    """
    root = config.resolved_root()
    if not root.is_dir():
        raise TrackerError(
            f"the repository the configuration resolved against, {root}, is not an existing directory, "
            "so there is nowhere to run gh. This is a configuration fault, not a gh or credential one: "
            "check CLAUDE_PROJECT_DIR and the .shipyard/ layers it points at."
        )
    try:
        proc = subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=False, timeout=TIMEOUT_SECONDS,
            cwd=root,
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

    The argv is not obviously secret-bearing, but `--body` carries whatever the caller wrote and
    `_repo_args()` carries `tracker_config.repo` — into every verb's argv, not just the one that
    resolves the repository — so both go through the same redaction as stderr rather than being
    trusted to be clean.

    The strip is per element and *before* the join, even though `_safe` strips too: an element is the unit
    the caller controls, so an over-stripping fallback for a malformed authority in one of them cannot
    reach an element the join happened to put next to it.
    """
    return _safe(" ".join(_stripped_of_credentials(arg) for arg in args))


def _stripped_of_credentials(value: str) -> str:
    """`value` with the userinfo of every credentialed authority in it dropped, credential fragments included.

    `gh --repo` accepts an https remote, and an https remote can carry userinfo, so a misconfigured
    `https://x-access-token:<token>@github.com/owner/repo.git` would otherwise be printed in cleartext by
    the failure that names it — and `gh`'s own stderr echoes such a value back for several failure shapes,
    which is why this runs over command output too. What is left — `https://host/owner/repo`, or
    `host:owner/repo` for an SSH remote — is still the value the operator has to go and fix.

    The value is stripped one whitespace-separated token at a time, and the whitespace is put back: a
    credentialed reference is rarely the whole value. Real `gh` (2.96.0) quotes one *inside a sentence* —
    ``Could not resolve to a Repository with the name 'x-access-token:<token>@github.com:owner/repo.git'.``
    — and a `--body` carries whatever the caller wrote around one. Matching the whole value, which is what
    the schemeless spellings used to require, therefore missed every prose case; a token cannot contain
    whitespace, so bounding a match inside one needs no lookahead past the token and several credentialed
    references in one line are each stripped independently.

    Leading and trailing punctuation around a token's credentialed reference — `gh`'s quotes and its
    trailing period — is not preserved: the userinfo prefix is what is dropped, and dropping an opening
    quote with it is a cosmetic cost paid to remove the secret.

    Adversarially malformed userinfo is not guaranteed to be stripped in full; real-world credential
    shapes are. A userinfo holding a raw, unencoded `/` is not RFC 3986 (it would be `%2F`), and no
    genuine remote or token an operator configures carries one, so `/` is read as the path boundary it is
    in every compliant value rather than guessed at.
    """
    return "".join(piece if piece.isspace() else _stripped_token(piece) for piece in re.split(r"(\s+)", value))


def _stripped_token(token: str) -> str:
    """One whitespace-free piece of a value, with the userinfo of the credentialed authority in it dropped.

    A token opening an authority with `//` is bounded before the credential is looked for, which is what a
    `re.sub` could not do: `_stripped_authority` cuts the authority component out first and only the last
    `@` *inside* it separates userinfo from host, so an extra `@` in the userinfo carries no fragment of
    itself through.

    Any other token is matched against three schemeless remote spellings: `user:pass@host:path` and
    `user:pass@host/path`, matched greedily to the last `@` so an extra `@` in the userinfo loses none of
    itself; and `user:pass@host` with no path or port at all — the bare RFC 3986 authority, which
    `git credential fill`/`.netrc` hand back directly and which a base-host paste against `--repo`'s
    `HOST/OWNER/REPO` grammar produces — which requires a colon inside the userinfo to tell it apart from
    a bare `user@host` address with nothing after it. The userinfo must be non-empty, so `@me` and
    `@me/abc` are left exactly as configured rather than reported back to the operator as `me/abc`; and a
    host followed by a bare `:` is not the scp form, so an address in a comment body
    (`someone@example.com: ...`) is not read as a credential. A token shaped like none of these —
    `OWNER/REPO`, a flag, an email address — is returned unchanged.

    An address immediately followed by a path (`someone@example.com/team`) *is* the schemeless remote
    spelling and loses its local part. The two are not distinguishable by shape, and a comment body with a
    mangled address in it costs less than a body with a token in it.
    """
    if "//" in token:
        head, *segments = token.split("//")
        return head + "".join(f"//{_stripped_authority(segment)}" for segment in segments)
    return re.sub(r"\S+@(?=[^\s@:/]+(?:/|:\S))|\S*:\S*@(?=[^\s@:/]+$)", "", token)


def _stripped_authority(segment: str) -> str:
    """One `//`-opened piece of a value, with the userinfo of the authority it opens with dropped.

    The authority is everything up to the first `/`, `?` or `#`, and the host is what follows its last
    `@`. A segment whose host is then not a host at all is not something this can reason about as a URL,
    so it falls back to dropping everything before the segment's last `@` — over-stripping a malformed
    value rather than printing what may be a password with a slash in it.
    """
    boundary = min((cut for cut in (segment.find(char) for char in "/?#") if cut != -1), default=len(segment))
    authority, tail = segment[:boundary], segment[boundary:]
    host = authority[authority.rfind("@") + 1 :]
    if re.fullmatch(r"[^\s/?#@:]+(?::\d*)?", host):
        return host + tail
    return segment[segment.rfind("@") + 1 :] if "@" in segment else segment


def _safe(text: str) -> str:
    """`text` as a message may carry it: credentials this process holds redacted, URL userinfo dropped.

    Three passes, and none can do the others' job. Scrubbing catches the credentials this process holds
    in its own environment; discovery honours `redaction.extra_words`, so an org-specific credential name
    redacts here exactly as it does on the attach-artifact sanitisation path. Stripping catches a
    credential this process cannot recognise by value, by its shape in free text — a token misconfigured
    into a remote URL. But a bare `<token>@host` with no path, port, or colon in it is shape-identical to
    a real email address, and no pattern can tell the two apart in *free* text without also mangling every
    genuine address a `gh` error or a comment body carries; found by review, round 10.

    The third pass sidesteps that ambiguity by not pattern-matching at all: `tracker_config.repo` and
    `.project` are held directly, not inferred from arbitrary output, so any `@` in either — other than
    the literal `@me`/`@me/...` self-reference, which is the one legitimate use of `@` a project value
    has — is unambiguously a credential boundary *in this narrow, known context*, the same way scrubbing
    already treats an environment variable's value as unambiguously sensitive because of what it is, not
    what it looks like. The exact prefix is registered for the same literal-value replacement scrubbing
    already does, so the schemeless and colon-less shapes the strip cannot safely touch — with a colon,
    without one, with or without a path — are caught here by matching the value itself rather than
    guessing at its shape; a scheme-bearing echo is already the strip's, scheme-case-agnostically. Below
    `DEFAULT_MIN_LENGTH` a prefix is not registered at all: a degenerate one- or two-character value would
    otherwise redact every short, unrelated `@`-bearing substring a message happens to carry.

    Every message built from `gh` output, from `_shown`'s argv rendering, or from a configured
    `tracker_config.repo`/`.project` value — the values that can actually carry a credential — goes
    through here, and the strip is part of it rather than something each site remembers to do: `gh`'s own
    stderr echoes a credentialed repository value back for a GraphQL resolution error, an HTTP error
    naming the URL, and a malformed-URL argument error, so the site-by-site version of this leaked from
    whichever verb was fixed second. A handful of messages interpolate caller-authored or `gh`-returned
    content that is never credential-shaped by construction — an issue title, an assignee login, a label,
    a configured `columns.*` display name, a board field's own option titles — and are not routed through
    here; those values are never a token's home, only a repository or project reference is.
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

    Read directly from config rather than matched against arbitrary text, which is what lets this catch
    the one shape no pattern safely can: a bare `<token>@host` is indistinguishable from an email address
    in free text, but `tracker_config.repo`/`.project` is never legitimately an email address, so any `@`
    in it is unambiguously a credential boundary here — except the literal `@me` or `@me/...` self-
    reference, `tracker_config.project`'s one legitimate use of `@`, which this leaves alone rather than
    reporting it back to the operator as a value they never configured.

    The carve-out does not generalise beyond that one literal form: a typo like `myorg@3` is treated as a
    credential too, and reported redacted rather than verbatim — hiding the very value the operator would
    need to see to fix it. That is the same trade this file already makes everywhere else in this
    direction: erring toward over-redaction of an ambiguous value costs a confusing message, not a leak.
    """
    value = str(config.get(path, default="") or "")
    if not value or value == "@me" or value.startswith("@me/"):
        return None
    at = value.rfind("@")
    return value[: at + 1] if at != -1 else None
