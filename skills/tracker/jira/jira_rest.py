#!/usr/bin/env python3
"""Small Jira REST helper for operations ACLI does not cover.

The account identifiers (email, site, project) come from resolved Shipyard config; only the
credential itself stays in the environment, where `scripts/secret_guard.py` covers it and no
`cat` of a config file can burn it into transcript history. Neither is ever passed in process
arguments.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
from pathlib import Path
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

# The resolver lives in the plugin's scripts/ directory, which is not importable from an adapter
# helper's own location, so the path is bootstrapped before importing it.
sys.path.insert(0, str(Path(os.environ.get('CLAUDE_PLUGIN_ROOT') or Path(__file__).resolve().parents[3]) / 'scripts'))
from sy_config import get as config_get  # noqa: E402


def config() -> tuple[str, str]:
    """Base URL and Basic auth header, from resolved config plus the one env-held secret."""
    email = str(config_get('tracker_config.email') or '')
    token = os.environ.get('ACLI_TOKEN')
    if not email:
        raise SystemExit('jira_rest: tracker_config.email must be set in .shipyard/config.json')
    if not token:
        raise SystemExit('jira_rest: ACLI_TOKEN must be set in the environment (it is a secret, not config)')
    site = str(config_get('tracker_config.site') or '')
    if not site:
        raise SystemExit('jira_rest: tracker_config.site must be set, e.g. yourorg.atlassian.net')
    base = site if site.startswith('http') else f'https://{site}'
    auth = base64.b64encode(f'{email}:{token}'.encode()).decode()
    return base.rstrip('/'), f'Basic {auth}'


def request(method: str, url: str, auth: str, data: bytes | None = None, headers: dict[str, str] | None = None):
    h = {'Authorization': auth, 'Accept': 'application/json'}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read()
            return resp.status, json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')
        raise SystemExit(f'jira_rest: HTTP {exc.code}: {detail[:2000]}') from exc


def attach(issue: str, file: Path) -> None:
    if not file.is_file():
        raise SystemExit(f'jira_rest: attachment not found: {file}')
    base, auth = config()
    boundary = '----toolbox-' + secrets.token_hex(16)
    ctype = mimetypes.guess_type(file.name)[0] or 'application/octet-stream'
    payload = b''.join([
        f'--{boundary}\r\n'.encode(),
        f'Content-Disposition: form-data; name="file"; filename="{file.name}"\r\n'.encode(),
        f'Content-Type: {ctype}\r\n\r\n'.encode(),
        file.read_bytes(), b'\r\n', f'--{boundary}--\r\n'.encode(),
    ])
    _, result = request('POST', f'{base}/rest/api/3/issue/{issue}/attachments', auth, payload, {
        'X-Atlassian-Token': 'no-check',
        'Content-Type': f'multipart/form-data; boundary={boundary}',
    })
    if not isinstance(result, list) or not result or not any(x.get('filename') == file.name for x in result if isinstance(x, dict)):
        raise SystemExit('jira_rest: upload response did not confirm requested filename')
    print(json.dumps(result, indent=2))



def add_label(issue: str, label: str) -> None:
    base, auth = config()
    _, item = request('GET', f'{base}/rest/api/3/issue/{issue}?fields=labels', auth)
    if not isinstance(item, dict):
        raise SystemExit('jira_rest: unexpected issue response while reading labels')
    fields = item.get('fields') or {}
    labels = fields.get('labels') or []
    if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
        raise SystemExit('jira_rest: unexpected labels shape')
    intended = labels if label in labels else [*labels, label]
    payload = json.dumps({'fields': {'labels': intended}}).encode()
    status, _ = request('PUT', f'{base}/rest/api/3/issue/{issue}', auth, payload, {'Content-Type': 'application/json'})
    if status != 204:
        raise SystemExit(f'jira_rest: expected HTTP 204, got {status}')
    print(json.dumps({'issue': issue, 'labels': intended}, indent=2))


def set_parent(issue: str, parent: str) -> None:
    base, auth = config()
    payload = json.dumps({'fields': {'parent': {'key': parent}}}).encode()
    status, _ = request('PUT', f'{base}/rest/api/3/issue/{issue}', auth, payload, {'Content-Type': 'application/json'})
    if status != 204:
        raise SystemExit(f'jira_rest: expected HTTP 204, got {status}')
    print(f'{issue} parent -> {parent}')


def create_link(blocker: str, blocked: str, link_type: str) -> None:
    """Create a dependency link where `blocker` blocks `blocked`, then verify the direction.

    Uses Jira's REST model directly, which is unambiguous: the outward issue performs the
    type's outward action, so for `Blocks` the outward issue blocks the inward issue. acli's
    `--out`/`--in` flags are empirically inverted relative to this, so they are not used here.
    """
    base, auth = config()
    payload = json.dumps({
        'type': {'name': link_type},
        'outwardIssue': {'key': blocker},
        'inwardIssue': {'key': blocked},
    }).encode()
    status, _ = request('POST', f'{base}/rest/api/3/issueLink', auth, payload, {'Content-Type': 'application/json'})
    if status not in (200, 201):
        raise SystemExit(f'jira_rest: expected HTTP 201 creating link, got {status}')
    _assert_blocks(base, auth, blocker, blocked, link_type)
    print(f'{blocker} {link_type.lower()} {blocked} (verified: {blocker} is the blocker)')


def preflight() -> None:
    """Real, minimal read: proves EMAIL/TOKEN/SITE together authenticate AND that PROJECT
    actually exists and is visible to this account. A credential can be present and still be
    dead, and an unknown project key does not error a JQL search — it just returns zero
    results — so this reads the project itself, which 404s loudly on a bad key."""
    project = str(config_get('tracker_config.project') or '')
    if not project:
        raise SystemExit('jira_rest: tracker_config.project must be set in .shipyard/config.json')
    base, auth = config()
    _, item = request('GET', f'{base}/rest/api/3/project/{urllib.parse.quote(project)}', auth)
    if not isinstance(item, dict) or item.get('key') != project:
        raise SystemExit(f'jira_rest: preflight read did not confirm project {project!r}')
    print(json.dumps({'ok': True, 'project': project, 'name': item.get('name')}, indent=2))


def get(issue: str, fields: str | None) -> None:
    """Print the issue JSON (optionally limited to a fields csv), indent=2."""
    base, auth = config()
    _, item = request('GET', _issue_url(base, issue, fields), auth)
    if not isinstance(item, dict):
        raise SystemExit('jira_rest: unexpected issue response')
    print(json.dumps(item, indent=2))


def transitions(issue: str) -> None:
    """List valid transitions for the issue as compact JSON lines: id, name, target status name."""
    base, auth = config()
    _, item = request('GET', f'{base}/rest/api/3/issue/{issue}/transitions', auth)
    trans = (item or {}).get('transitions')
    if not isinstance(trans, list) or not all(isinstance(t, dict) for t in trans):
        raise SystemExit('jira_rest: unexpected transitions response')
    if not trans:
        raise SystemExit(f'jira_rest: no transitions available for {issue}')
    for t in trans:
        print(json.dumps({'id': t.get('id'), 'name': t.get('name'), 'to': (t.get('to') or {}).get('name')}))


def comment_get(issue: str, comment_id: str) -> None:
    """Print one issue comment as JSON indent=2."""
    base, auth = config()
    _, item = request('GET', f'{base}/rest/api/3/issue/{issue}/comment/{comment_id}', auth)
    if not isinstance(item, dict):
        raise SystemExit('jira_rest: unexpected comment response')
    print(json.dumps(item, indent=2))


def attachment_download(issue: str, filename: str, attachment_id: str | None, output: Path | None) -> None:
    """Download the unique attachment matching filename (or --id) to --output (default: filename in cwd)."""
    base, auth = config()
    att = _resolve_attachment(_get_attachments(base, auth, issue), filename, attachment_id)
    content_url = att.get('content')
    if not isinstance(content_url, str) or not content_url:
        raise SystemExit(f'jira_rest: attachment {att.get("id")} has no content URL')
    data = _fetch_binary(content_url, auth)
    dest = output or Path(filename)
    dest.write_bytes(data)
    print(f'{len(data)} bytes -> {dest}')


def attachment_delete(issue: str, filename: str, attachment_id: str | None) -> None:
    """Delete the unique attachment matching filename (or --id) and verify it is gone from the issue."""
    base, auth = config()
    att = _resolve_attachment(_get_attachments(base, auth, issue), filename, attachment_id)
    att_id = str(att.get('id'))
    status, _ = request('DELETE', _attachment_url(base, att_id), auth)
    if status != 204:
        raise SystemExit(f'jira_rest: expected HTTP 204 deleting attachment {att_id}, got {status}')
    if any(str(a.get('id')) == att_id for a in _get_attachments(base, auth, issue)):
        raise SystemExit(f'jira_rest: attachment {att_id} still present on {issue} after delete')
    print(f'deleted attachment {att_id} ({att.get("filename")}) from {issue}')


def attachment_update(issue: str, file: Path) -> None:
    """Replace-by-filename: delete every attachment named like FILE (zero is fine), then upload FILE."""
    if not file.is_file():
        raise SystemExit(f'jira_rest: attachment not found: {file}')
    base, auth = config()
    existing = [a for a in _get_attachments(base, auth, issue) if a.get('filename') == file.name]
    for att in existing:
        status, _ = request('DELETE', _attachment_url(base, str(att.get("id"))), auth)
        if status != 204:
            raise SystemExit(f'jira_rest: expected HTTP 204 deleting attachment {att.get("id")}, got {status}')
    if existing:
        print(f'deleted {len(existing)} existing attachment(s) named {file.name}')
    attach(issue, file)


def type_convert(issue: str, issue_type: str) -> None:
    """Set the issue type to Task or Epic and verify the change took effect (loud failure if restricted)."""
    payload = json.dumps({'fields': {'issuetype': {'name': issue_type}}}).encode()
    base, auth = config()
    status, _ = request('PUT', _issue_url(base, issue), auth, payload, {'Content-Type': 'application/json'})
    if status != 204:
        raise SystemExit(f'jira_rest: expected HTTP 204 setting issue type, got {status}')
    _, item = request('GET', _issue_url(base, issue, 'issuetype'), auth)
    actual = (((item or {}).get('fields') or {}).get('issuetype') or {}).get('name')
    if actual != issue_type:
        raise SystemExit(f'jira_rest: type did not change (workflow may restrict this); {issue} is still {actual!r}')
    print(f'{issue} type -> {issue_type}')


def self_test() -> None:
    """Offline check of subcommand parsing, config(), and URL builders; no network, prints pass and exits 0."""
    parser = _build_parser()
    cases: list[tuple[list[str], str]] = [
        (['attach', 'AM-1', 'f.txt'], 'attach'),
        (['add-label', 'AM-1', 'x'], 'add-label'),
        (['set-parent', 'AM-1', 'AM-2'], 'set-parent'),
        (['link', '--blocker', 'AM-1', '--blocked', 'AM-2'], 'link'),
        (['preflight'], 'preflight'),
        (['get', 'AM-1', '--fields', 'summary,labels'], 'get'),
        (['transitions', 'AM-1'], 'transitions'),
        (['comment-get', 'AM-1', '10001'], 'comment-get'),
        (['attachment-download', 'AM-1', 'f.txt', '--id', '5', '--output', 'out.txt'], 'attachment-download'),
        (['attachment-delete', 'AM-1', 'f.txt', '--id', '5'], 'attachment-delete'),
        (['attachment-update', 'AM-1', 'f.txt'], 'attachment-update'),
        (['type-convert', 'AM-1', 'Epic'], 'type-convert'),
        (['self-test'], 'self-test'),
    ]
    for argv, cmd in cases:
        parsed = parser.parse_args(argv)
        assert parsed.cmd == cmd, f'jira_rest self-test: {argv} parsed to {parsed.cmd!r}, expected {cmd!r}'
    # Identifiers come from the resolver now; only the credential is still an env var.
    saved_token = os.environ.get('ACLI_TOKEN')
    fake = {'tracker_config.email': 'a@b.c', 'tracker_config.site': 'example.atlassian.net'}
    original = globals()['config_get']
    globals()['config_get'] = lambda key: fake[key] if key in fake else original(key)
    try:
        os.environ['ACLI_TOKEN'] = 'tok'
        base, auth = config()
        assert base == 'https://example.atlassian.net', base
        assert auth.startswith('Basic '), auth
        assert _issue_url(base, 'AM-1') == 'https://example.atlassian.net/rest/api/3/issue/AM-1'
        assert _issue_url(base, 'AM-1', 'attachment') == (
            'https://example.atlassian.net/rest/api/3/issue/AM-1?fields=attachment'
        )
        assert _attachment_url(base, '5') == 'https://example.atlassian.net/rest/api/3/attachment/5'
        del os.environ['ACLI_TOKEN']
        try:
            config()
        except SystemExit as exc:
            assert 'secret, not config' in str(exc), exc
        else:  # pragma: no cover
            raise AssertionError('a missing credential must fail loudly and name where it belongs')
    finally:
        globals()['config_get'] = original
        if saved_token is None:
            os.environ.pop('ACLI_TOKEN', None)
        else:
            os.environ['ACLI_TOKEN'] = saved_token
    print('jira_rest self-test passed')


def _assert_blocks(base: str, auth: str, blocker: str, blocked: str, link_type: str) -> None:
    _, item = request('GET', f'{base}/rest/api/3/issue/{blocker}?fields=issuelinks', auth)
    links = ((item or {}).get('fields') or {}).get('issuelinks') or []
    for link in links:
        if not isinstance(link, dict) or (link.get('type') or {}).get('name') != link_type:
            continue
        if (link.get('outwardIssue') or {}).get('key') == blocked:
            return
    raise SystemExit(
        f'jira_rest: direction NOT confirmed after create — {blocker} shows no outward '
        f'{link_type} link to {blocked}. Refusing to assert the dependency; check and fix by hand.'
    )


def _issue_url(base: str, issue: str, fields: str | None = None) -> str:
    url = f'{base}/rest/api/3/issue/{issue}'
    return f'{url}?fields={fields}' if fields else url


def _attachment_url(base: str, attachment_id: str) -> str:
    return f'{base}/rest/api/3/attachment/{attachment_id}'


def _get_attachments(base: str, auth: str, issue: str) -> list[dict]:
    _, item = request('GET', _issue_url(base, issue, 'attachment'), auth)
    atts = ((item or {}).get('fields') or {}).get('attachment')
    if not isinstance(atts, list) or not all(isinstance(a, dict) for a in atts):
        raise SystemExit(f'jira_rest: unexpected attachment field shape on {issue}')
    return atts


def _resolve_attachment(attachments: list[dict], filename: str, attachment_id: str | None) -> dict:
    if attachment_id:
        matches = [a for a in attachments if str(a.get('id')) == attachment_id]
        if len(matches) != 1:
            raise SystemExit(f'jira_rest: attachment id {attachment_id} not found on issue')
        return matches[0]
    matches = [a for a in attachments if a.get('filename') == filename]
    if len(matches) == 1:
        return matches[0]
    listing = ', '.join(
        f'id={a.get("id")} filename={a.get("filename")!r} created={a.get("created")}'
        for a in (matches or attachments)
    ) or 'none'
    raise SystemExit(
        f'jira_rest: {len(matches)} attachments named {filename!r}; expected exactly one '
        f'(pass --id to disambiguate). Candidates: {listing}'
    )


def _fetch_binary(url: str, auth: str) -> bytes:
    req = urllib.request.Request(url, headers={'Authorization': auth})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')
        raise SystemExit(f'jira_rest: HTTP {exc.code}: {detail[:2000]}') from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='cmd', required=True)
    a = sub.add_parser('attach')
    a.add_argument('issue')
    a.add_argument('file', type=Path)
    l = sub.add_parser('add-label')
    l.add_argument('issue')
    l.add_argument('label')
    p = sub.add_parser('set-parent')
    p.add_argument('issue')
    p.add_argument('parent')
    sub.add_parser('preflight', help='real minimal project read: proves REST credentials are live, not just present')
    k = sub.add_parser('link', help='create a verified dependency link (blocker blocks blocked)')
    k.add_argument('--blocker', required=True, help='the issue that blocks')
    k.add_argument('--blocked', required=True, help='the issue that is blocked')
    k.add_argument('--type', default='Blocks')
    g = sub.add_parser('get', help='print issue JSON')
    g.add_argument('issue')
    g.add_argument('--fields', help='comma-separated field list')
    t = sub.add_parser('transitions', help='list valid transitions (id, name, target status)')
    t.add_argument('issue')
    c = sub.add_parser('comment-get', help='print one comment as JSON')
    c.add_argument('issue')
    c.add_argument('comment_id')
    d = sub.add_parser('attachment-download', help='download an attachment by exact filename (or --id)')
    d.add_argument('issue')
    d.add_argument('filename')
    d.add_argument('--id', help='attachment id, required when filename is ambiguous')
    d.add_argument('--output', type=Path, help='destination path (default: filename in cwd)')
    x = sub.add_parser('attachment-delete', help='delete an attachment by exact filename (or --id), verified')
    x.add_argument('issue')
    x.add_argument('filename')
    x.add_argument('--id', help='attachment id, required when filename is ambiguous')
    u = sub.add_parser('attachment-update', help='replace attachments matching FILE name, then upload FILE')
    u.add_argument('issue')
    u.add_argument('file', type=Path)
    v = sub.add_parser('type-convert', help='set issue type to Task or Epic, verified')
    v.add_argument('issue')
    v.add_argument('type', choices=['Task', 'Epic'])
    sub.add_parser('self-test', help='offline parser/config/URL self-test')
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    if args.cmd == 'attach':
        attach(args.issue, args.file)
    elif args.cmd == 'add-label':
        add_label(args.issue, args.label)
    elif args.cmd == 'set-parent':
        set_parent(args.issue, args.parent)
    elif args.cmd == 'preflight':
        preflight()
    elif args.cmd == 'link':
        create_link(args.blocker, args.blocked, args.type)
    elif args.cmd == 'get':
        get(args.issue, args.fields)
    elif args.cmd == 'transitions':
        transitions(args.issue)
    elif args.cmd == 'comment-get':
        comment_get(args.issue, args.comment_id)
    elif args.cmd == 'attachment-download':
        attachment_download(args.issue, args.filename, args.id, args.output)
    elif args.cmd == 'attachment-delete':
        attachment_delete(args.issue, args.filename, args.id)
    elif args.cmd == 'attachment-update':
        attachment_update(args.issue, args.file)
    elif args.cmd == 'type-convert':
        type_convert(args.issue, args.type)
    else:
        self_test()


if __name__ == '__main__':
    main()
