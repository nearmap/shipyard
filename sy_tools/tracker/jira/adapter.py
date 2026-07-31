"""Jira REST, spoken by a long-lived server process.

Ported from `skills/tracker/jira/jira_rest.py` rather than imported from it: that script is a
CLI, so it signals with `print()` and `raise SystemExit`. Inside the MCP server stdout carries
JSON-RPC frames — a stray print desynchronises the client — and a `SystemExit` would end a
process that still has other calls to serve. Everything here returns a dict or raises
`TrackerError`, and nothing in this module writes to stdout. The shipped script stays untouched
so the CLI deployment keeps working unchanged.

The transport is async httpx2, and the canonical verbs are `async` because the server serves calls
concurrently: an upload waiting on Jira must not block an unrelated tool call. Only the transport
awaits — the multipart body is assembled synchronously, byte-for-byte, because Jira validates the
hand-built boundary.

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

BLOCKER_SIDE = "outwardIssue"
BLOCKED_SIDE = "inwardIssue"
"""Which end of a `Blocks` link each role occupies, in both the write and the read.

Named rather than inlined because the two are trivially swappable and a swap is silent: it inverts
every dependency the server reports while every call still succeeds. See `_linked` for the measured
read semantics that fix these two values."""

COMMENT_PAGE = 50
"""How many comments one read returns, newest first: a bound truncates the oldest rather than the
most recent, which is the useful half of a long ship log."""

FORBIDDEN_IN_FILENAME = ('"', "\r", "\n")
"""Characters an attachment's name may not contain, because the multipart header is built by hand:
a quote closes `filename="..."` early and a CR or LF starts a header line of the caller's choosing."""

RESULT_CEILING = 200
"""The hard cap on a search's `maxResults`, whatever a caller asks for. An unbounded search on a
busy project is a large response assembled in memory for a caller that wanted a page."""


class JiraAdapter:
    """The canonical tracker verbs, implemented against the Jira Cloud REST API."""

    name = "jira"

    def __init__(self) -> None:
        self._account_id: str | None = None

    async def create_issue(self, issue_type: str, title: str, body: str = "", parent: str | None = None) -> dict:
        """Create an issue of a canonical type in the configured project; `parent` makes it a child.

        `reporter` is listed as required by Jira's own `createmeta` but is deliberately not sent:
        omitting it makes Jira default the reporter to the authenticated account, and sending it
        would let a shared config file decide who a person's issues are reported by.

        Jira enforces its own hierarchy here — a type that cannot be parented to `parent`'s type is
        rejected with a 400 naming the field, which surfaces as that same detail on a `TrackerError`.
        """
        project = _project()
        native = native_type(issue_type)
        base, auth = _credentials()
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

        Two calls, because Jira serves comments from their own endpoint: the field read is scoped to
        `ISSUE_FIELDS`, and the comment read is bounded and newest-first.

        `comments_truncated` reports whether that bound actually cut anything off. A silently short
        thread reads as a complete ship log, so a caller deciding on the strength of "no one raised
        this" has to be able to tell a quiet issue from a clipped page.
        """
        base, auth = _credentials()
        fields = await _read_fields(base, auth, issue, ISSUE_FIELDS)
        _, thread = await request(
            "GET", f"{base}{API}/issue/{issue}/comment?maxResults={COMMENT_PAGE}&orderBy=-created", auth
        )
        comments, truncated = _comments(issue, thread)
        return {
            **_summary(base, issue, fields),
            "body": adf.adf_to_markdown(fields.get("description")),
            "children": _keys(fields.get("subtasks")),
            "dependencies": _linked(fields.get("issuelinks"), BLOCKER_SIDE),
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
    ) -> dict:
        """One page of issues in the configured project matching the given filters, newest API only.

        The classic `GET /search` endpoint is gone (410), so this posts JQL to `/search/jql`, which
        pages by opaque token and reports no total. Only the first page is fetched: a caller that
        wants more asks again with the returned token rather than having this verb walk the board.

        `is_last` is derived from the absence of `nextPageToken`, not from `isLast`: the endpoint's
        contract does not guarantee that field, and reading a missing one as "done" would report a
        partial page as the whole result set.

        Every interpolated value is a quoted JQL literal, so a title containing a quote cannot break
        out of its clause and widen the search.
        """
        project = _project()
        if limit <= 0:
            raise TrackerError(f"limit must be a positive number of issues, got {limit}")
        clauses = [f"project = {_jql(project)}"]
        if status:
            clauses.append(f"status = {_jql(native_status(status))}")
        if issue_type:
            clauses.append(f"issuetype = {_jql(native_type(issue_type))}")
        if parent:
            clauses.append(f"parent = {_jql(parent)}")
        if text:
            clauses.append(f"text ~ {_jql(text)}")
        base, auth = _credentials()
        payload = {
            "jql": " AND ".join(clauses),
            "maxResults": min(limit, RESULT_CEILING),
            "fields": list(SUMMARY_FIELDS),
        }
        page = await _send_json("POST", f"{base}{API}/search/jql", auth, payload, expect=(200,))
        entries = page.get("issues") if isinstance(page, dict) else None
        if not isinstance(page, dict) or not isinstance(entries, list):
            raise TrackerError(f"search returned no issues list; got {_shape(page)}")
        items = []
        for entry in entries:
            fields = entry.get("fields") if isinstance(entry, dict) else None
            if not isinstance(fields, dict):
                raise TrackerError(f"a search result carried no fields block; got {_shape(entry)}")
            items.append(_summary(base, _field(entry, "key") or "", fields))
        return {
            "issues": items,
            "count": len(items),
            "is_last": not _field(page, "nextPageToken"),
            "next_page_token": _field(page, "nextPageToken"),
        }

    async def set_status(self, issue: str, status: str) -> dict:
        """Move `issue` to the column this repo uses for a canonical status, verified by reading back.

        The target is matched on each transition's `to.name`, never on the transition's own `name`:
        they often coincide, but they are different fields and matching the wrong one is a silent
        move to the wrong column. When nothing reachable matches, this fails listing the reachable
        targets rather than retrying blind or accepting the current status — a workflow gap has to be
        seen, not absorbed.
        """
        target = native_status(status)
        base, auth = _credentials()
        _, listing = await request("GET", f"{base}{API}/issue/{issue}/transitions", auth)
        available = listing.get("transitions") if isinstance(listing, dict) else None
        if not isinstance(available, list) or not available:
            raise TrackerError(f"no transitions are available on {issue}; got {_shape(listing)}")
        wanted = target.strip().lower()
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
        """Assign `issue` to the authenticated account. Only self-assignment is supported.

        Any other assignee is refused rather than quietly self-assigned: an assignment silently
        landing on the wrong person is worse than a failure that names the limit.
        """
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
        """Record that `blocked_by` blocks `issue`, then re-read to prove the direction really took.

        Direction comes straight from Jira's REST model, which is unambiguous: the outward issue
        performs the type's outward action, so for `Blocks` the outward issue is the blocker. The
        verification read is not belt-and-braces — a reversed dependency reads as plausible and
        misleads every later decomposition, so a link whose direction cannot be confirmed is a
        failure rather than a warning.
        """
        base, auth = _credentials()
        payload = {
            "type": {"name": BLOCKS},
            BLOCKER_SIDE: {"key": blocked_by},
            BLOCKED_SIDE: {"key": issue},
        }
        await _send_json("POST", f"{base}{API}/issueLink", auth, payload, expect=(200, 201, 204))
        links = (await _read_fields(base, auth, blocked_by, ("issuelinks",))).get("issuelinks")
        if issue not in _linked(links, BLOCKED_SIDE):
            raise TrackerError(
                f"direction not confirmed after creating the link: reading {blocked_by} shows no "
                f"{BLOCKS} link naming {issue} as the blocked issue. Refusing to report the "
                "dependency; check and fix by hand"
            )
        return {"id": issue, "blocked_by": blocked_by, "verified": True}

    async def add_label(self, issue: str, label: str) -> dict:
        """Add `label` to `issue`, preserving the labels already there.

        Jira has no append: the field is replaced wholesale, so the current set is read and written
        back with `label` unioned in. A labels field that does not read back as a list of strings
        aborts the write instead of being coerced — coercing it would delete labels.
        """
        base, auth = _credentials()
        current = (await _read_fields(base, auth, issue, ("labels",))).get("labels") or []
        if not isinstance(current, list) or not all(isinstance(x, str) for x in current):
            raise TrackerError(
                f"{issue} returned a labels field that is not a list of strings; refusing to write it "
                "back and lose the labels it does have"
            )
        intended = current if label in current else [*current, label]
        await _send_json("PUT", f"{base}{API}/issue/{issue}", auth, {"fields": {"labels": intended}})
        return {"id": issue, "labels": intended}

    async def post_comment(self, issue: str, body: str) -> dict:
        """Comment on `issue` with `body` converted from Markdown to Jira rich text.

        The conversion happens here rather than client-side because the node classes a Markdown-ish
        client drops silently — bullet lists and fenced code — are exactly the ones a ship log is
        made of.
        """
        base, auth = _credentials()
        created = await _send_json(
            "POST", f"{base}{API}/issue/{issue}/comment", auth, {"body": adf.markdown_to_adf(body)}, expect=(200, 201)
        )
        comment_id = _field(created, "id")
        if not comment_id:
            raise TrackerError(f"comment on {issue} returned no id; treat the comment as not posted")
        return {"id": issue, "comment_id": comment_id, "url": f"{_browse(base, issue)}?focusedCommentId={comment_id}"}

    async def attach_artifact(self, issue: str, path: Path) -> dict:
        """Upload `path` to `issue` and return the response evidence confirming the write.

        The filename is checked before the body is built, not escaped: it goes into a hand-assembled
        `Content-Disposition` header, where a quote malforms the part and a CR or LF — both legal in a
        POSIX filename — appends attacker-chosen headers to the request. Refusing the three characters
        is one comparison; escaping them correctly is a multipart quoting implementation.
        """
        if any(ch in path.name for ch in FORBIDDEN_IN_FILENAME):
            raise TrackerError(
                "attachment filename may not contain a quote, carriage return or newline: those would "
                "break the multipart header this upload builds by hand. Rename the file and retry"
            )
        if not path.is_file():
            raise TrackerError(f"attachment not found: {path}")
        base, auth = _credentials()
        boundary = "----shipyard-" + secrets.token_hex(16)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        payload = b"".join([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode(),
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
        confirmed = _confirmation(result, path.name)
        if confirmed is None:
            raise TrackerError(
                f"upload response did not confirm {path.name!r} on {issue}; treat the attachment as failed"
            )
        attachment_id = _field(confirmed, "id")
        if not attachment_id:
            raise TrackerError(
                f"upload of {path.name!r} on {issue} came back without an attachment id; there is nothing "
                "to point at later, so treat the attachment as failed rather than reported"
            )
        return {
            "issue": issue,
            "filename": path.name,
            "id": attachment_id,
            "size": confirmed.get("size"),
            "created": confirmed.get("created"),
        }

    async def preflight(self) -> dict:
        """Prove the configured account and its credential authenticate, reporting no secret value.

        A credential can be present and still be dead, so this is a real authenticated read
        rather than a presence check. `myself` is the cheapest one Jira offers.
        """
        base, auth = _credentials()
        return {"ok": True, "site": base, "account_id": await self._account(base, auth)}

    async def _account(self, base: str, auth: str) -> str:
        """The authenticated account's id, read from `myself` once per adapter instance.

        The cache is per-instance and not a module global on purpose: `tracker.adapter()` builds a
        fresh adapter per call, so an instance attribute expires naturally when a credential is
        rotated, where a module-level cache would keep serving the previous account's id.
        """
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
) -> tuple[int, object]:
    """One authenticated REST call, returning `(status, parsed body or None)`.

    The timeout is not optional. Being async no longer wedges the whole server, but an unbounded
    call still leaves its own caller awaiting forever while holding a connection, and the tool that
    was asked to attach a transcript never answers. Handing it to the client bounds every phase —
    connect, write, read and pool — not just the request write.

    The two `except` clauses are ordered, not interchangeable: `TimeoutException` is a subclass of
    `RequestError`, so catching the family first would rename every stall an unreachable host and
    lose the one distinction that decides whether retrying is worth anything.

    `data` is sent as a raw body (`content=`), never form-encoded: the multipart payload carries a
    hand-built boundary that must reach Jira byte-for-byte. `transport` is the seam a test uses to
    drive this mapping without a network; production callers leave it None.
    """
    sent = {"Authorization": auth, "Accept": "application/json"}
    sent.update(headers or {})
    try:
        async with httpx2.AsyncClient(timeout=TIMEOUT_SECONDS, transport=transport) as client:
            resp = await client.request(method, url, content=data, headers=sent)
            if not resp.is_success:
                raise TrackerError(f"HTTP {resp.status_code} from {method} {url}: {resp.text[:2000]}")
            return resp.status_code, json.loads(resp.content) if resp.content else None
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

    Jira answers its writes with either a 204 and an empty body or a 201 and an echo, and a status
    outside what the endpoint is known to return means the write did not land the way the caller
    reports it did — so `expect` is per call site rather than "any 2xx".
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


def _summary(base: str, key: str, fields: dict) -> dict:
    """The keys `find-issues` reports per item, which `get-issue` also returns verbatim.

    Both verbs build their common half here so the two can never drift into reporting the same issue
    under different key names or with one side canonicalised and the other native.
    """
    return {
        "id": key,
        "title": str(fields.get("summary") or ""),
        "status": canonical_status(_field(fields.get("status"), "name")),
        "type": canonical_type(_field(fields.get("issuetype"), "name")),
        "parent": _field(fields.get("parent"), "key"),
        "labels": [x for x in fields.get("labels") or [] if isinstance(x, str)],
        "url": _browse(base, key),
    }


def _comments(issue: str, thread: object) -> tuple[list[dict], bool]:
    """One issue's comments plus whether the page left any out, in the order Jira returned them.

    The comment endpoint pages, so a bounded read cannot tell its caller "that is the whole thread"
    without checking. Completeness is read off Jira's own `startAt`/`total` when both are there; when
    `total` is absent the only honest signal left is a page that came back full, which is reported as
    possibly truncated rather than assumed complete.
    """
    entries = thread.get("comments") if isinstance(thread, dict) else None
    if not isinstance(entries, list):
        raise TrackerError(f"comment read of {issue} returned no comments list; got {_shape(thread)}")
    items = [
        {
            "id": _field(entry, "id") or "",
            "author": _field(entry.get("author"), "displayName") or "",
            "created": _field(entry, "created") or "",
            "body": adf.adf_to_markdown(entry.get("body")),
        }
        for entry in entries
        if isinstance(entry, dict)
    ]
    total = thread.get("total") if isinstance(thread, dict) else None
    start = thread.get("startAt") if isinstance(thread, dict) else None
    if isinstance(total, int) and not isinstance(total, bool):
        seen = (start if isinstance(start, int) and not isinstance(start, bool) else 0) + len(entries)
        return items, seen < total
    return items, len(entries) >= COMMENT_PAGE


def _linked(links: object, side: str) -> list[str]:
    """The `Blocks`-linked issues sitting on one absolute side of a read issue's links.

    A read carries only the *counterpart* of each link, and the field it arrives under names that
    counterpart's absolute role — the same roles the write posts, not roles relative to the issue
    being read. So on a read of X, a counterpart under `BLOCKER_SIDE` blocks X, and one under
    `BLOCKED_SIDE` is blocked by X.

    This was measured, not assumed, because getting it backwards is silent and inverts every
    dependency: a link posted as "AM-1245 blocks AM-1246" reads back on AM-1246 with AM-1245 under
    `outwardIssue`, and on AM-1245 with AM-1246 under `inwardIssue`. Confirmed against real linked
    issues whose summaries make the intended direction unambiguous.
    """
    if not isinstance(links, list):
        return []
    found = []
    for link in links:
        if not isinstance(link, dict) or (_field(link.get("type"), "name") or "").lower() != BLOCKS.lower():
            continue
        key = _field(link.get(side), "key")
        if key:
            found.append(key)
    return found


def _keys(value: object) -> list[str]:
    """Every issue key in a list-of-issues field, ignoring entries that carry none."""
    if not isinstance(value, list):
        return []
    return [key for entry in value if (key := _field(entry, "key"))]


def _field(value: object, key: str) -> str | None:
    """One string member of a nested object, or None when the object or the member is absent.

    Every relational Jira field is a nested object whose presence is optional (`parent` is missing
    rather than null on an orphan), so reaching into one is a guard, not an index.
    """
    if not isinstance(value, dict):
        return None
    member = value.get(key)
    return str(member) if member else None


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


def _credentials() -> tuple[str, str]:
    """Base URL and the `Authorization` header value: config identifiers plus the env-held secret.

    The site is rejected if it carries userinfo, which is what makes this module's promise that no
    credential reaches a URL true of the configured value too and not just of the env-held token.
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
