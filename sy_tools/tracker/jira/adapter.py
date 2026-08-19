"""Jira REST, spoken by a long-lived server process. This is the implementation, not a wrapper.

Every canonical verb of `skills/tracker/CONTRACT.md` is served from here. All of them return a dict
or raise `TrackerError`, never print and never `SystemExit`: inside the MCP server stdout carries
JSON-RPC frames, and an exit would end a process that still has other calls to serve. The verbs are
`async` over httpx2 so an upload waiting on Jira does not block an unrelated tool call.

Account identifiers come from resolved config; the credential is read from the environment only,
is put into the `Authorization` header and nowhere else, and never appears in a URL, in a
returned dict, or in an exception message.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
from pathlib import Path
import secrets
from urllib.parse import quote

import httpx2

from ... import config
from .. import TIMEOUT_SECONDS, TrackerError, canonical_status, canonical_type, native_status, native_type
from . import adf

TOKEN_ENV = "ACLI_TOKEN"
API = "/rest/api/3"

SUMMARY_FIELDS = ("summary", "status", "issuetype", "parent", "labels")
"""The fields behind every key `find-issues` reports per item, and the same keys inside `get-issue`.

One tuple rather than two lists because the two verbs are required to agree: an item from a search
must carry the same keys, canonicalised the same way, as the corresponding read.
"""

ISSUE_FIELDS = (*SUMMARY_FIELDS, "description", "subtasks", "issuelinks")
"""Exactly what a full read needs. Named explicitly instead of `*all`, which fetches every custom
field on the board — kilobytes of noise per issue that nothing above this seam looks at."""

BLOCKS = "Blocks"
"""The Jira link type behind the canonical `add-dependency` relation."""

BLOCKER_SIDE = "inwardIssue"
BLOCKED_SIDE = "outwardIssue"
"""Which end of a `Blocks` link each role occupies, in both the write and the read: the blocker is the
inward end and the issue it blocks is the outward end.

Named rather than inlined because the two are trivially swappable and a swap is silent: it inverts
every dependency the server reports while every call still succeeds, and a suite that reads direction
only through these names passes either way. Measure them, don't derive them: Jira's own prose is not
safe here — the `issueLink` POST docs call `outwardIssue` the "from" issue and the admin docs describe
the outward description as "how a work item affects other work items", which compose to blocker =
`outwardIssue`, the pre-fix (wrong) assignment this file shipped with; that composition is plausibly how
it got inverted in the first place. Instead take a link whose direction is known independently (created
by hand in the Jira UI, say) and `GET /issue/<KEY>` on both ends: per `_linked`'s docstring below, the
field a counterpart appears under (`inwardIssue`/`outwardIssue`) is that counterpart's own posted role,
so whichever end the known blocker appears under is `BLOCKER_SIDE`. Read the slot names, not link
descriptions: a description is admin-editable prose, which is what this docstring refuses to depend on."""

COMMENT_PAGE = 50
"""How many comments one read returns, newest first: a bound truncates the oldest rather than the
most recent, which is the useful half of a long ship log."""

FORBIDDEN_IN_FILENAME = ('"', "\\", "\r", "\n")
"""Characters an attachment's name may not contain, because the multipart header is built by hand:
a quote closes `filename="..."` early, a backslash can escape into the quoted string, and a CR or
LF starts a header line of the caller's choosing."""

RESULT_CEILING = 200
"""The hard cap on a search's `maxResults`, whatever a caller asks for. An unbounded search on a
busy project is a large response assembled in memory for a caller that wanted a page."""

LEAF_TYPES = ("task", "bug")
"""The canonical types whose children Jira's `subtasks` field really does report.

Membership is tested positively rather than by asking whether the type *is* an Epic, because
`canonical_type` passes a native name it does not map through unchanged: an issue at a hierarchy
level above Epic, at a custom level, or carrying no `issuetype` at all is not `"epic"` either, and
excluding only `"epic"` sent every one of them to `subtasks` — empty for all of them — so a
decomposed parent read as childless, with no error and no truncation flag to make that look wrong.

Getting this wrong in the safe direction costs one search on an issue that has no children to find.
Getting it wrong in the other direction is silent, and `children: []` is what the duplicate-work and
decomposition checks above this seam read as "nothing has been planned here yet".
"""

NOT_FOUND = 404
"""The one status that means a configured project key names nothing this account can read."""


class JiraStatusError(TrackerError):
    """A `TrackerError` for a call Jira actually answered, carrying the status it answered with.

    `_project_key` has to tell a 404 from a stall before it blames the configured project key, so the
    status rides on the exception rather than being parsed back out of its message.
    """

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class JiraAdapter:
    """The canonical tracker verbs, implemented against the Jira Cloud REST API."""

    name = "jira"
    # Characters. Atlassian's JCMA migration KB states the 32767 limit applies to both description and
    # comments on Cloud (citing JRACLOUD-59124); the jira.text.field.character.limit property behind it
    # is tunable in Data Center only (JRACLOUD-63007); JRACLOUD-68949 corroborates the
    # description-field limit. Its unit is still undocumented under ADF.
    body_limit: int = 32_767

    def __init__(self) -> None:
        self._account_id: str | None = None

    async def create_issue(self, issue_type: str, title: str, body: str = "", parent: str | None = None) -> dict:
        """Create an issue of a canonical type in the configured project; `parent` makes it a child.

        Jira enforces its own hierarchy: a type that cannot be parented to `parent`'s type comes back
        as a `TrackerError` carrying Jira's own 400 detail naming the field.
        """
        project = _project()
        native = native_type(issue_type)
        base, auth = _credentials()
        # `reporter` is required by Jira's `createmeta` but deliberately omitted: Jira then defaults it
        # to the authenticated account, where sending it lets a shared config file decide whose issue.
        fields: dict[str, object] = {
            "project": {"key": project},
            "issuetype": {"name": native},
            "summary": title,
            "description": adf.markdown_to_adf(body),
        }
        if parent:
            fields["parent"] = {"key": parent}
        created = await _send_json("POST", f"{base}{API}/issue", auth, {"fields": fields}, expect=(200, 201))
        key = _field(created, "key")
        if not key:
            raise TrackerError(f"create in {project} returned no issue key; treat the issue as not created")
        return {"id": key, "url": _browse(base, key), "type": issue_type, "title": title, "parent": parent}

    async def get_issue(self, issue: str) -> dict:
        """Read one issue and its comments, with rich text as Markdown and statuses canonicalised.

        Three calls at most: the field read scoped to `ISSUE_FIELDS`, a bounded newest-first comment
        read, and a child search on anything that is not a `LEAF_TYPES` type. `comments_truncated` and
        `children_truncated` report whether either bound cut anything off — a clipped page and a
        genuinely quiet issue read identically otherwise.
        """
        base, auth = _credentials()
        fields = await _read_fields(base, auth, issue, ISSUE_FIELDS)
        children, children_truncated = await self._children(issue, fields)
        _, thread = await request(
            "GET", f"{base}{API}/issue/{issue}/comment?maxResults={COMMENT_PAGE}&orderBy=-created", auth
        )
        comments, truncated = _comments(issue, thread)
        return {
            **_summary(base, issue, fields),
            "body": adf.adf_to_markdown(fields.get("description")),
            "children": children,
            "children_truncated": children_truncated,
            "dependencies": _linked(fields.get("issuelinks"), BLOCKER_SIDE, "issuelinks"),
            "comments": comments,
            "comments_truncated": truncated,
        }

    async def update_issue(self, issue: str, body: str) -> dict:
        """Replace `issue`'s description with `body`, converted to Jira rich text."""
        base, auth = _credentials()
        payload = {"fields": {"description": adf.markdown_to_adf(body)}}
        await _send_json("PUT", f"{base}{API}/issue/{issue}", auth, payload)
        return {"id": issue, "updated": True, "url": _browse(base, issue)}

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
        """One page of issues in the configured project matching the given filters.

        Paging is by opaque token and reports no total: exactly one page is fetched, and a caller that
        wants more asks again with the returned `next_page_token` rather than having this verb walk the
        whole board.
        """
        return await self._search(
            _project(), status=status, issue_type=issue_type, parent=parent, text=text, limit=limit,
            page_token=page_token,
        )

    async def _search(
        self,
        project: str | None = None,
        *,
        status: str | None = None,
        issue_type: str | None = None,
        parent: str | None = None,
        text: str | None = None,
        limit: int = 50,
        page_token: str | None = None,
    ) -> dict:
        """One page of issues matching the given filters: the search both readers share.

        `project` is optional because the two callers scope differently: `find-issues` is about the
        configured board, while `get-issue`'s child search scopes by `parent = <key>` alone.
        `page_token` is Jira's own `nextPageToken`.
        """
        if limit <= 0:
            raise TrackerError(f"limit must be a positive number of issues, got {limit}")
        clauses = [f"project = {_jql(project)}"] if project else []
        if status:
            clauses.append(f"status = {_jql(native_status(status))}")
        if issue_type:
            clauses.append(f"issuetype = {_jql(native_type(issue_type))}")
        if parent:
            clauses.append(f"parent = {_jql(parent)}")
        if text:
            clauses.append(f"text ~ {_jql(text)}")
        if not (project or parent):
            raise TrackerError(
                "a search scoped by neither project nor parent is not bounded to a single issue or "
                "board; Jira's search API rejects an unbounded query outright, and this adapter does "
                "not treat a status/type/text filter as bounding on its own, since it does not scope "
                "to one board or one issue's children — refusing before the request"
            )
        base, auth = _credentials()
        payload: dict[str, object] = {
            "jql": " AND ".join(clauses),
            "maxResults": min(limit, RESULT_CEILING),
            "fields": list(SUMMARY_FIELDS),
        }
        # Sent only when a caller supplies one: the endpoint reads this key's presence, not its value,
        # so a first-page request carrying an empty token is not the same request as one carrying none.
        if page_token:
            payload["nextPageToken"] = page_token
        # The classic `GET /search` endpoint is gone (410), so the JQL is POSTed to `/search/jql`.
        page = await _send_json("POST", f"{base}{API}/search/jql", auth, payload, expect=(200,))
        entries = page.get("issues") if isinstance(page, dict) else None
        if not isinstance(page, dict) or not isinstance(entries, list):
            raise TrackerError(f"search returned no issues list; got {_shape(page)}")
        items = []
        for entry in entries:
            fields = entry.get("fields") if isinstance(entry, dict) else None
            if not isinstance(fields, dict):
                raise TrackerError(f"a search result carried no fields block; got {_shape(entry)}")
            key = _str_field(entry, "key")
            if not key:
                raise TrackerError(f"a search result carried no issue key; got {_shape(entry)}")
            items.append(_summary(base, key, fields))
        return {
            "issues": items,
            "count": len(items),
            # Derived from `nextPageToken`, not the spec's own `isLast`: both were present in every live
            # response, and paging on one source of truth cannot disagree with itself.
            "is_last": not _field(page, "nextPageToken"),
            "next_page_token": _field(page, "nextPageToken"),
        }

    async def set_status(self, issue: str, status: str) -> dict:
        """Move `issue` to the column this repo uses for a canonical status, verified by reading back.

        When nothing reachable matches, this fails listing the reachable targets rather than retrying
        blind or accepting the current status: a workflow gap has to be seen, not absorbed.
        """
        target = native_status(status)
        base, auth = _credentials()
        _, listing = await request("GET", f"{base}{API}/issue/{issue}/transitions", auth)
        available = listing.get("transitions") if isinstance(listing, dict) else None
        if not isinstance(available, list) or not available:
            raise TrackerError(f"no transitions are available on {issue}; got {_shape(listing)}")
        wanted = target.strip().lower()
        # Keyed on each transition's `to.name`, never the transition's own `name`: they often coincide,
        # but they are different fields and matching the wrong one moves the issue to the wrong column.
        reachable = {_field(t.get("to"), "name") or "?": _field(t, "id") for t in available if isinstance(t, dict)}
        matched = next((tid for name, tid in reachable.items() if name.strip().lower() == wanted), None)
        if not matched:
            raise TrackerError(
                f"no transition from {issue}'s current status reaches {target!r}; "
                f"reachable targets: {', '.join(sorted(reachable))}"
            )
        await _send_json("POST", f"{base}{API}/issue/{issue}/transitions", auth, {"transition": {"id": matched}})
        moved = _field((await _read_fields(base, auth, issue, ("status",))).get("status"), "name")
        if (moved or "").strip().lower() != wanted:
            raise TrackerError(
                f"{issue} still reads {moved!r} after transition {matched} toward {target!r}; "
                "treat the move as failed"
            )
        return {"id": issue, "status": status, "native": moved}

    async def assign(self, issue: str, assignee: str = "@me") -> dict:
        """Assign `issue` to the authenticated account; any other assignee is refused, not coerced."""
        if assignee != "@me":
            raise TrackerError(
                f"only self-assignment is supported by this adapter; got {assignee!r}, expected '@me'"
            )
        base, auth = _credentials()
        account_id = await self._account(base, auth)
        await _send_json("PUT", f"{base}{API}/issue/{issue}/assignee", auth, {"accountId": account_id})
        return {"id": issue, "assignee": account_id}

    async def link_parent(self, issue: str, parent: str) -> dict:
        """Set `issue`'s parent, which on Jira is a field write rather than a link."""
        base, auth = _credentials()
        await _send_json("PUT", f"{base}{API}/issue/{issue}", auth, {"fields": {"parent": {"key": parent}}})
        return {"id": issue, "parent": parent}

    async def add_dependency(self, issue: str, blocked_by: str) -> dict:
        """Record that `blocked_by` blocks `issue`, then re-read to prove the link arrived.

        A reversed dependency reads as entirely plausible and misleads every later decomposition, so a
        link the read-back cannot find is a failure here rather than a warning. `verified: True` claims
        exactly that much: the link is on the blocker and readable. It cannot claim `BLOCKER_SIDE` and
        `BLOCKED_SIDE` name the ends Jira's way, since a write read back through the same two constants
        confirms either assignment of them.
        """
        base, auth = _credentials()
        # BLOCKER_SIDE/BLOCKED_SIDE: see their docstring for which slot is which and why it's measured.
        payload = {
            "type": {"name": BLOCKS},
            BLOCKER_SIDE: {"key": blocked_by},
            BLOCKED_SIDE: {"key": issue},
        }
        await _send_json("POST", f"{base}{API}/issueLink", auth, payload, expect=(200, 201, 204))
        links = (await _read_fields(base, auth, blocked_by, ("issuelinks",))).get("issuelinks")
        # `strict=False`: the POST has already landed, so drift in some unrelated pre-existing link must
        # not fail a write that succeeded and invite a retry that duplicates it. Field drift still raises.
        if issue not in _linked(links, BLOCKED_SIDE, "issuelinks", strict=False):
            raise TrackerError(
                f"direction not confirmed after creating the link: reading {blocked_by} shows no "
                f"readable {BLOCKS} link naming {issue} as the blocked issue. Refusing to report the "
                "dependency; check and fix by hand"
            )
        return {"id": issue, "blocked_by": blocked_by, "verified": True}

    async def add_label(self, issue: str, label: str) -> dict:
        """Add `label` to `issue`, preserving the labels already there.

        Jira has no append: the field is replaced wholesale, so the current set is read and written
        back with `label` unioned in.
        """
        base, auth = _credentials()
        current = (await _read_fields(base, auth, issue, ("labels",))).get("labels") or []
        # Aborted rather than coerced: coercing an unreadable labels field into a list deletes labels.
        if not isinstance(current, list) or not all(isinstance(x, str) for x in current):
            raise TrackerError(
                f"{issue} returned a labels field that is not a list of strings; refusing to write it "
                "back and lose the labels it does have"
            )
        intended = current if label in current else [*current, label]
        await _send_json("PUT", f"{base}{API}/issue/{issue}", auth, {"fields": {"labels": intended}})
        return {"id": issue, "labels": intended}

    async def post_comment(self, issue: str, body: str) -> dict:
        """Comment on `issue` with `body` converted from Markdown to Jira rich text."""
        base, auth = _credentials()
        # Converted here rather than client-side: the node classes a Markdown-ish client drops silently —
        # bullet lists and fenced code — are exactly the ones a ship log is made of.
        created = await _send_json(
            "POST", f"{base}{API}/issue/{issue}/comment", auth, {"body": adf.markdown_to_adf(body)}, expect=(200, 201)
        )
        comment_id = _field(created, "id")
        if not comment_id:
            raise TrackerError(f"comment on {issue} returned no id; treat the comment as not posted")
        return {"id": issue, "comment_id": comment_id, "url": f"{_browse(base, issue)}?focusedCommentId={comment_id}"}

    async def attach_artifact(self, issue: str, path: Path) -> dict:
        """Upload `path` to `issue` and return the response evidence confirming the write."""
        base, auth = _credentials()
        return await _upload(base, auth, issue, path)

    async def type_convert(self, issue: str, issue_type: str) -> dict:
        """Change `issue`'s type to a canonical type, verified by reading the type back.

        A Jira workflow can accept the field write and leave the type where it was, so an unverified
        conversion fails naming the type the issue still carries: a caller told an issue is now an Epic
        will decompose it as one.
        """
        native = native_type(issue_type)
        base, auth = _credentials()
        await _send_json("PUT", f"{base}{API}/issue/{issue}", auth, {"fields": {"issuetype": {"name": native}}})
        actual = _field((await _read_fields(base, auth, issue, ("issuetype",))).get("issuetype"), "name")
        if (actual or "").strip().lower() != native.strip().lower():
            raise TrackerError(
                f"{issue} still reads type {actual or 'unset'!r} rather than {native!r} after the write; the "
                "workflow may restrict this conversion. Treat the change as failed"
            )
        return {"id": issue, "type": issue_type, "native": actual}

    async def attachment_download(self, issue: str, filename_or_id: str, output_path: Path) -> dict:
        """Write the one attachment on `issue` matching `filename_or_id` to `output_path`."""
        base, auth = _credentials()
        found = _resolve_attachment(await _get_attachments(base, auth, issue), filename_or_id, issue)
        content_url = found.get("content")
        # A failure, not an empty file: a zero-byte artifact on disk reads exactly like one never uploaded.
        if not isinstance(content_url, str) or not content_url:
            raise TrackerError(
                f"attachment {_field(found, 'id') or '?'} on {issue} carries no content URL, so there is "
                "nothing to download; treat the read as failed rather than writing an empty file"
            )
        # Jira's own `content` URL, not a path built here: it 302s to media storage, which `binary=True`
        # is what allows this one call to follow.
        _, data = await request("GET", content_url, auth, binary=True)
        if not isinstance(data, bytes):
            raise TrackerError(f"the download of {filename_or_id!r} from {issue} returned no bytes to write")
        try:
            output_path.write_bytes(data)
        except OSError as exc:
            # Wrapped like every other failure in this verb: an unwritable destination is a failed read to
            # the caller, and a bare OSError crosses the seam as something no caller of a tracker verb handles.
            raise TrackerError(
                f"the download of {filename_or_id!r} from {issue} could not be written to {output_path}: {exc}"
            ) from None
        return {
            "issue": issue,
            "filename": _field(found, "filename"),
            "id": _field(found, "id"),
            "bytes": len(data),
            "path": str(output_path),
        }

    async def attachment_update(self, issue: str, path: Path) -> dict:
        """Replace every attachment on `issue` named `path.name` with `path`, verifying each step.

        Jira has no attachment replace: an upload under a name already on the issue adds a second
        attachment beside it, and the two are then told apart only by an id nobody above this seam holds.
        """
        base, auth = _credentials()
        # Every refusal made before the upload: a missing or unnameable source, or a namesake with no id
        # to delete it by, discovered after a delete would leave the issue with nothing at all.
        _checked_source(path)
        existing = [a for a in await _get_attachments(base, auth, issue) if a.get("filename") == path.name]
        superseded = []
        for found in existing:
            attachment_id = _field(found, "id")
            if not attachment_id:
                raise TrackerError(
                    f"an attachment named {path.name!r} on {issue} carries no id, so it cannot be deleted and "
                    "the upload would sit beside it; refusing rather than leaving two files with one name"
                )
            superseded.append(attachment_id)
        # Upload before delete is load-bearing: deleting first loses the artifact outright when the upload
        # then fails, where a failed delete only leaves two files — visible, recoverable, and reported.
        uploaded = await _upload(base, auth, issue, path)
        for attachment_id in superseded:
            await _delete_attachment(base, auth, issue, attachment_id)
        return {**uploaded, "replaced": len(superseded)}

    async def _children(self, issue: str, fields: dict) -> tuple[list[str], bool]:
        """`issue`'s children, and whether that list is one page short of all of them.

        Jira's `subtasks` field is sub-task-level only and comes back empty on every Epic whatever is
        parented beneath it, so it is trusted only for a `LEAF_TYPES` type. Everything else takes the
        same `parent = <key>` search `find-issues` serves, one page, with the bound reported. That split
        is only exhaustive because the execution model `skills/tracker/jira/ADAPTER.md` documents is flat:
        one tracking Epic with every executable Task and Bug directly under it.
        """
        if canonical_type(_field(fields.get("issuetype"), "name")) in LEAF_TYPES:
            return _keys(fields.get("subtasks"), "subtasks"), False
        # Scoped by `parent = <key>` alone: a key prefix survives a move between projects and Advanced
        # Roadmaps parents across them, so a project clause subtracts real children (Jira 200s no match).
        page = await self._search(parent=issue, limit=RESULT_CEILING)
        return [str(item["id"]) for item in page["issues"]], not page["is_last"]

    async def preflight(self) -> dict:
        """Prove the configured account, credential and project are all usable, reporting no secret value.

        Each of the three can be present and still be wrong — a credential can be revoked, a project key
        can name a board this account cannot see — so each is read rather than checked for presence. All
        three, because `skills/tracker/jira/ADAPTER.md`'s contract for this verb covers all three.
        """
        base, auth = _credentials()
        project = _project()
        account_id = await self._account(base, auth)
        key = await _project_key(base, auth, project)
        return {"ok": True, "site": base, "account_id": account_id, "project": key}

    async def _account(self, base: str, auth: str) -> str:
        """The authenticated account's id, read from `myself` once per adapter instance."""
        # Per-instance, not a module global: `tracker.adapter()` builds a fresh adapter per call, so a
        # rotated credential expires this naturally. `myself` is the cheapest authenticated read Jira has.
        if self._account_id is None:
            _, item = await request("GET", f"{base}{API}/myself", auth)
            account_id = _field(item, "accountId")
            if not account_id:
                raise TrackerError(
                    f"read of {base}{API}/myself returned no account; the credential in "
                    f"{TOKEN_ENV} may be revoked or belong to another site"
                )
            self._account_id = account_id
        return self._account_id


async def request(
    method: str,
    url: str,
    auth: str,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
    binary: bool = False,
) -> tuple[int, object]:
    """One authenticated REST call, returning `(status, parsed body or None)`.

    `transport` is the seam a test drives this mapping through without a network; production callers
    leave it None. `binary` serves the one call that fetches an attachment's bytes and changes two
    things together because that call needs both: the body comes back unparsed, and redirects are
    followed — an attachment's `content` URL answers a 302 to media storage, which is not `is_success`.
    """
    sent = {"Authorization": auth, "Accept": "application/json"}
    sent.update(headers or {})
    try:
        # Timeout on the client bounds every phase — connect, write, read, pool — not just the write; the
        # redirect is credential-safe because httpx2 drops `Authorization` once it leaves the origin.
        async with httpx2.AsyncClient(
            timeout=TIMEOUT_SECONDS, transport=transport, follow_redirects=binary
        ) as client:
            # `content=`, never form-encoded: the multipart boundary is hand-built and must reach Jira
            # byte-for-byte.
            resp = await client.request(method, url, content=data, headers=sent)
            if not resp.is_success:
                raise JiraStatusError(
                    f"HTTP {resp.status_code} from {method} {url}: {resp.text[:2000]}", resp.status_code
                )
            if binary:
                return resp.status_code, resp.content
            return resp.status_code, json.loads(resp.content) if resp.content else None
    # Ordered, not interchangeable: `TimeoutException` subclasses `RequestError`, so catching the family
    # first would rename every stall an unreachable host.
    except httpx2.TimeoutException as exc:
        raise TrackerError(f"{method} {url} timed out after {TIMEOUT_SECONDS}s") from exc
    except httpx2.RequestError as exc:
        raise TrackerError(f"could not reach {url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise TrackerError(f"{method} {url} returned a 2xx response whose body is not JSON") from exc


async def _send_json(
    method: str,
    url: str,
    auth: str,
    payload: dict,
    *,
    expect: tuple[int, ...] = (204,),
) -> object:
    """One JSON-bodied call whose status is asserted, returning the parsed body (often None).

    `expect` is per call site rather than "any 2xx": Jira answers a write with either a 204 and no body
    or a 201 and an echo, and an unexpected status means the write did not land as the caller reports.
    """
    status, body = await request(method, url, auth, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    if status not in expect:
        raise TrackerError(f"expected HTTP {'/'.join(str(x) for x in expect)} from {method} {url}, got {status}")
    return body


async def _read_fields(base: str, auth: str, issue: str, names: tuple[str, ...]) -> dict:
    """The named `fields` block of one issue, or a `TrackerError` naming the shape that came back."""
    _, item = await request("GET", f"{base}{API}/issue/{issue}?fields={','.join(names)}", auth)
    fields = item.get("fields") if isinstance(item, dict) else None
    if not isinstance(fields, dict):
        raise TrackerError(f"read of {issue} returned no fields block; got {_shape(item)}")
    return fields


async def _upload(base: str, auth: str, issue: str, path: Path) -> dict:
    """One verified multipart upload of `path` to `issue`, and the evidence it landed."""
    filename = _checked_source(path)
    boundary = "----shipyard-" + secrets.token_hex(16)
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    # One shared copy of the hand-built boundary: a second would be a second place to omit the refusal.
    payload = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        path.read_bytes(),
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    _, result = await request(
        "POST",
        f"{base}{API}/issue/{issue}/attachments",
        auth,
        payload,
        {
            "X-Atlassian-Token": "no-check",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    confirmed = _confirmation(result, filename)
    if confirmed is None:
        raise TrackerError(f"upload response did not confirm {filename!r} on {issue}; treat the attachment as failed")
    attachment_id = _field(confirmed, "id")
    if not attachment_id:
        raise TrackerError(
            f"upload of {filename!r} on {issue} came back without an attachment id; there is nothing "
            "to point at later, so treat the attachment as failed rather than reported"
        )
    return {
        "issue": issue,
        "filename": filename,
        "id": attachment_id,
        "size": confirmed.get("size"),
        "created": confirmed.get("created"),
    }


def _checked_source(path: Path) -> str:
    """`path.name`, refusing before any request the two things an upload cannot recover from."""
    # Checked, not escaped: refusing four characters is one comparison, escaping them correctly is a
    # multipart quoting implementation. CR and LF are both legal in a POSIX filename.
    if any(ch in path.name for ch in FORBIDDEN_IN_FILENAME):
        raise TrackerError(
            "attachment filename may not contain a quote, backslash, carriage return or newline: "
            "those would break the multipart header this upload builds by hand. Rename the file and retry"
        )
    if not path.is_file():
        raise TrackerError(f"attachment not found: {path}")
    return path.name


async def _get_attachments(base: str, auth: str, issue: str) -> list[dict]:
    """Every attachment currently on `issue`, or a failure naming the shape that came back."""
    found = (await _read_fields(base, auth, issue, ("attachment",))).get("attachment")
    # A failure, not an empty list: both callers read absence as an answer — "no attachment by that name"
    # for an issue that has one, or an unparseable field taken as proof a delete landed.
    if not isinstance(found, list) or not all(isinstance(a, dict) for a in found):
        raise TrackerError(
            f"read of {issue}'s attachments returned {_shape(found)}, not a list of attachments; the issue's "
            "attachments are unknown and must not be reported as none"
        )
    return found


def _resolve_attachment(attachments: list[dict], filename_or_id: str, issue: str) -> dict:
    """The one attachment `filename_or_id` names, by attachment id or by exact filename."""
    # Jira lets one issue carry several attachments with one name, so a filename is not a key and picking
    # either of two uploads of one transcript is arbitrary — hence the id in the same argument, tried first.
    by_id = [a for a in attachments if _field(a, "id") == filename_or_id]
    matches = by_id if len(by_id) == 1 else [a for a in attachments if a.get("filename") == filename_or_id]
    if len(matches) == 1:
        return matches[0]
    listing = ", ".join(
        f"id={_field(a, 'id')} filename={a.get('filename')!r} created={_field(a, 'created')}"
        for a in (matches or attachments)
    )
    raise TrackerError(
        f"{len(matches)} attachments on {issue} match {filename_or_id!r}; expected exactly one, so pass an "
        f"attachment id to name the one you mean. Candidates: {listing or 'none'}"
    )


async def _delete_attachment(base: str, auth: str, issue: str, attachment_id: str) -> None:
    """Delete one attachment and prove it is gone by re-reading the issue's own attachment field."""
    status, _ = await request("DELETE", _attachment_url(base, attachment_id), auth)
    if status != 204:
        raise TrackerError(
            f"expected HTTP 204 deleting attachment {attachment_id} from {issue}, got {status}; treat the "
            "deletion as failed"
        )
    # The 204 only says Jira accepted the call; only this read says the file is off the issue.
    if any(_field(a, "id") == attachment_id for a in await _get_attachments(base, auth, issue)):
        raise TrackerError(
            f"attachment {attachment_id} is still on {issue} after the delete reported success; treat the "
            "deletion as failed rather than reported"
        )


def _attachment_url(base: str, attachment_id: str) -> str:
    """One attachment's own endpoint: attachments are addressed by id, not through their issue."""
    return f"{base}{API}/attachment/{attachment_id}"


def _summary(base: str, key: str, fields: dict) -> dict:
    """The keys `find-issues` reports per item, which `get-issue` also returns verbatim.

    Built once so the two verbs cannot drift into naming the same issue's keys differently, or into
    canonicalising one side and leaving the other native.
    """
    labels = fields.get("labels")
    # Refused, not filtered: filtering iterated a bare string into its characters, dropped an object
    # entry, and let a number raise a bare `TypeError`. Jira omits the field on an unlabelled issue.
    if labels is not None and (not isinstance(labels, list) or not all(isinstance(x, str) for x in labels)):
        raise TrackerError(
            f"{key} returned a labels field that is not a list of strings but {_shape(labels)}; refusing "
            "to report labels this read cannot parse, since a filtered list reads as the issue's real ones"
        )
    return {
        "id": key,
        "title": str(fields.get("summary") or ""),
        "status": canonical_status(_field(fields.get("status"), "name")),
        "type": canonical_type(_field(fields.get("issuetype"), "name")),
        "parent": _field(fields.get("parent"), "key"),
        "labels": labels or [],
        "url": _browse(base, key),
    }


def _comments(issue: str, thread: object) -> tuple[list[dict], bool]:
    """One issue's comments plus whether the page left any out, in the order Jira returned them."""
    entries = thread.get("comments") if isinstance(thread, dict) else None
    if not isinstance(entries, list):
        raise TrackerError(f"comment read of {issue} returned no comments list; got {_shape(thread)}")
    items: list[dict] = []
    for index, entry in enumerate(entries):
        # Raised, not skipped: a dropped comment left the thread short while `total` and `startAt` still
        # agreed the page was complete.
        if not isinstance(entry, dict):
            raise TrackerError(
                f"comment {index} of {issue} is not a comment object but {_shape(entry)}, so the thread "
                "cannot be read whole; it must not come back one comment short of what the page reports"
            )
        items.append({
            "id": _field(entry, "id") or "",
            "author": _author(entry.get("author"), issue, index),
            "created": _field(entry, "created") or "",
            "body": adf.adf_to_markdown(entry.get("body")),
        })
    # Jira does not always send `total`, so the fallback signal is a page that came back full — reported
    # as possibly truncated rather than assumed complete.
    total = thread.get("total") if isinstance(thread, dict) else None
    start = thread.get("startAt") if isinstance(thread, dict) else None
    if isinstance(total, int) and not isinstance(total, bool):
        seen = (start if isinstance(start, int) and not isinstance(start, bool) else 0) + len(entries)
        return items, seen < total
    return items, len(entries) >= COMMENT_PAGE


def _author(author: object, issue: str, index: int) -> str:
    """One comment author's display name, refusing a shape this adapter cannot read."""
    # Jira omits the author of a deleted account, so absent stays honestly empty — as in github's `_login`.
    if author is None:
        return ""
    # Refused here rather than by tightening `_field`, whose tolerance every other caller needs for a
    # legitimately absent nested object: under it a string-shaped author read back as `""`.
    if not isinstance(author, dict):
        raise TrackerError(
            f"comment {index} of {issue} has an author that is not an author object but {_shape(author)}, "
            "so this thread cannot be read; an unreadable author must not report as an absent one"
        )
    return _field(author, "displayName") or ""


def _linked(links: object, side: str, field: str, *, strict: bool = True) -> list[str]:
    """The `Blocks`-linked issues sitting on one absolute side of a read issue's links.

    A read carries only the *counterpart* of each link, under the field naming that counterpart's
    absolute role — the same roles the write posts, not roles relative to the issue being read. So on a
    read of X, a counterpart under `inwardIssue` blocks X and one under `outwardIssue` is blocked by X.

    An absent field means no links; a field present but not a list is a failure, since `dependencies` is
    what a caller reads to decide whether an issue is blocked and a shape this cannot parse must not come
    back as "nothing is blocking it". `strict=False` keeps every field-level answer and downgrades only
    the per-entry raises to a skip, for `add_dependency`'s post-write verification.
    """
    if links is None:
        return []
    if not isinstance(links, list):
        raise TrackerError(
            f"the {field} field read back as {_shape(links)}, not a list of links, so the relations on "
            "this issue are unknown and it must not be reported as having none"
        )
    found = []
    for index, link in enumerate(links):
        if not isinstance(link, dict):
            if not strict:
                continue
            raise TrackerError(
                f"entry {index} of the {field} field is not a link object but {_shape(link)}, so the "
                "relations on this issue are unknown and it must not be reported as having none"
            )
        # Jira's REST v3 spec marks `type` required on an `IssueLink` without guaranteeing a `name` inside
        # it, so "no name to compare" is a distinct answer from "compared, and it is not Blocks".
        type_name = _str_field(link.get("type"), "name")
        if type_name is None:
            if not strict:
                continue
            raise TrackerError(
                f"entry {index} of the {field} field has an unreadable link type ({_shape(link.get('type'))}), "
                "so whether it is a Blocks link is unknown and it must not be reported as unrelated"
            )
        if type_name.lower() != BLOCKS.lower():
            continue
        # Which side is which is worth re-deriving rather than assuming, since getting it backwards is
        # silent and inverts every dependency: a link posted as "BLOCKER blocks BLOCKED" reads back on
        # BLOCKED with BLOCKER under `inwardIssue`.
        counterpart = link.get(side)
        if counterpart is None:
            continue  # this direction does not apply to this link, which is not a fault
        # Spec v3 documents `key` here as "Required if `id` isn't provided", so a counterpart carrying
        # neither is drift, and `dependencies` has no truncation channel to signal a dropped link.
        key = _str_field(counterpart, "key")
        if key:
            found.append(key)
            continue
        if _str_field(counterpart, "id"):
            continue  # spec-legal and addressable, just not by the key a caller reads
        if not strict:
            continue
        raise TrackerError(
            f"entry {index} of the {field} field has a {side} that names no issue "
            f"({_shape(counterpart)}), so a Blocks link on this issue cannot be read and it must not be "
            "reported as absent"
        )
    return found


def _keys(value: object, field: str) -> list[str]:
    """Every issue key in a list-of-issues field. An entry carrying none fails the read.

    Absent is empty — Jira omits a relation field an issue has none of — but a field, or an entry, this
    cannot parse raises: a short or unparseable child list reads as a bare, undecomposed issue, and
    `children_truncated` has no way to signal that a real child went missing from it.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise TrackerError(
            f"the {field} field read back as {_shape(value)}, not a list of issues, so the related "
            "issues on this issue are unknown and it must not be reported as having none"
        )
    keys: list[str] = []
    for index, entry in enumerate(value):
        key = _str_field(entry, "key")
        if not key:
            raise TrackerError(
                f"entry {index} of the {field} field carries no issue key, only {_shape(entry)}, so this "
                "list cannot be read whole; it must not come back one issue short of what Jira returned"
            )
        keys.append(key)
    return keys


def _field(value: object, key: str) -> str | None:
    """One string member of a nested object, or None when the object or the member is absent.

    Every relational Jira field is a nested object whose presence is optional (`parent` is missing
    rather than null on an orphan), so reaching into one is a guard, not an index.
    """
    if not isinstance(value, dict):
        return None
    member = value.get(key)
    return str(member) if member else None


def _str_field(value: object, key: str) -> str | None:
    """One member of a nested object that must really be a non-empty string, or None.

    `_field`'s coercion is right where a member is only displayed (`str()` on a Jira `id` arriving as a
    number is the id) and wrong where one is *acted on*: under it `{"key": {"a": 1}}` put the string
    `"{'a': 1}"` into `dependencies` as an issue key, and any truthy `id` shape silently dropped a real
    `Blocks` link. Jira's REST v3 spec types both as strings, so anything else reads here as absent.
    """
    if not isinstance(value, dict):
        return None
    member = value.get(key)
    return member if isinstance(member, str) and member else None


def _jql(value: str) -> str:
    """One JQL string literal, escaped so a value cannot terminate its own clause."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _browse(base: str, issue: str) -> str:
    """The human-facing URL for an issue — what a caller pastes into a PR or a comment."""
    return f"{base}/browse/{issue}"


def _shape(value: object) -> str:
    """A response's shape for a failure message: its keys or its type, never its content."""
    if isinstance(value, dict):
        return f"an object with keys {sorted(str(k) for k in value)}"
    return type(value).__name__


def _project() -> str:
    """The configured project key, which creation and search cannot be scoped without."""
    project = _identifier("tracker_config.project")
    if not project:
        raise TrackerError(
            "tracker_config.project is unset; set it in .shipyard/config.json, e.g. the board's key"
        )
    return project


async def _project_key(base: str, auth: str, project: str) -> str:
    """The configured project's own key, read back from Jira, or a failure naming the configured value.

    Nothing else here notices a wrong project key: Jira answers a JQL search naming a project that does
    not exist, or that this account cannot see, with zero issues rather than an error, so a mistyped key
    reads as an empty board and only surfaces much later, as a 400 inside a create. Reading the project
    404s loudly instead, which is what makes `skills/tracker/jira/ADAPTER.md`'s preflight contract — one
    failure naming which configured value is wrong — true of the project as well as of the credential.
    """
    try:
        _, item = await request("GET", f"{base}{API}/project/{quote(project, safe='')}", auth)
    except JiraStatusError as exc:
        # Only a 404 is rewritten into a verdict about the key: relabelling every `TrackerError` blamed the
        # configuration for a timeout, a revoked credential or a 500.
        if exc.status != NOT_FOUND:
            raise
        raise TrackerError(
            f"tracker_config.project is set to {project!r}, which this account could not read: {exc} "
            "Check the key against the projects this account can see; a wrong key is invisible to every "
            "search, which answers zero issues rather than failing"
        ) from exc
    key = _field(item, "key") or ""
    # Compared, not merely fetched: this endpoint also resolves a project by numeric id, so a response
    # arriving under a different key is not the project the configuration names.
    if key.strip() != project.strip():
        raise TrackerError(
            f"reading tracker_config.project {project!r} came back as project {key or 'nothing'!r}, so the "
            "configured value does not name that project's key; treat the configuration as wrong"
        )
    return key


def _credentials() -> tuple[str, str]:
    """Base URL and the `Authorization` header value: config identifiers plus the env-held secret.

    A site carrying userinfo is rejected, so this module's promise that no credential reaches a URL holds
    for the configured value too and not only for the env-held token.
    """
    email = _identifier("tracker_config.email")
    site = _identifier("tracker_config.site")
    token = os.environ.get(TOKEN_ENV)
    if not email:
        raise TrackerError("tracker_config.email is unset; set it in .shipyard/config.json")
    if not site:
        raise TrackerError(
            "tracker_config.site is unset; set it in .shipyard/config.json, e.g. yourorg.atlassian.net"
        )
    if not token:
        raise TrackerError(
            f"{TOKEN_ENV} is unset in the environment; it is a secret and never lives in a config file"
        )
    base = (site if site.startswith("http") else f"https://{site}").rstrip("/")
    if "@" in base.partition("://")[2].partition("/")[0]:
        raise TrackerError(
            "tracker_config.site must be a bare host such as yourorg.atlassian.net, with no "
            "user:password@ part: an embedded credential would ride in every request URL, in the "
            "browse links this returns and in any failure message quoting them"
        )
    return base, "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()


def _identifier(key: str) -> str:
    """One non-secret config identifier, as a string, empty when absent."""
    try:
        value = config.get(key, default="")
    except config.ConfigError as exc:
        raise TrackerError(f"{key} could not be resolved: {exc}") from exc
    return str(value or "")


def _confirmation(result: object, filename: str) -> dict | None:
    """The uploaded attachment Jira echoes back for `filename`, or None when it confirmed nothing."""
    if not isinstance(result, list):
        return None
    return next((x for x in result if isinstance(x, dict) and x.get("filename") == filename), None)
