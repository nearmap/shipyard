#!/usr/bin/env python3
"""GitHub Projects v2 field helper for the GitHub tracker adapter.

Shipyard drives issue **Type** and **Status** as Projects v2 single-select fields (works identically on
a personal user-owned project and an org project — no organization or native issue types required).
This helper isolates the only unavoidable node-id juggling (`updateProjectV2ItemFieldValue`, exposed
through `gh project item-edit`) behind one command so the adapter markdown never handles raw IDs. It
discovers the project/field/option node IDs once, caches them per project under
`.scratch/sy/github-project-cache.json` (gitignored working-tree scratch), and invalidates on a
lookup miss.

Column names are NOT hard-coded. Shipyard is opinionated only about the five lifecycle columns; each
column's actual name on the board is a REQUIRED per-repo config key (set in the repo's
`.shipyard/config.json`, so different repos can differ), shared by every tracker adapter:

    columns.backlog      columns.ready      columns.in_progress
    columns.in_review    columns.done

canonical status -> that config key -> the board's "Status" single-select option (matched
case-insensitively). Type uses a "Type" single-select with options Epic/Task/Bug (case-insensitive).

Commands:
  set-status --project <owner>/<number> --issue <url> --status <canonical>
  set-type   --project <owner>/<number> --issue <url> --type <canonical>
  get   --project <owner>/<number> --issue <url>                 # {number,title,url,type,status} (canonical)
  list  --project <owner>/<number> [--type <c>] [--status <c>]   # all items, optionally filtered
  check --project <owner>/<number>                               # verify every canonical value resolves
  resolve --project <owner>/<number> [--refresh]
  cache-clear [--project <owner>/<number>]
  self-test

Canonical values come from skills/tracker/CONTRACT.md.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

# The resolver lives in the plugin's scripts/ directory, which is not importable from an adapter
# helper's own location, so the path is bootstrapped before importing it.
sys.path.insert(0, str(Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parents[3]) / "scripts"))
from sy_config import get as config_get  # noqa: E402

STATUS_FIELD = "Status"
TYPE_FIELD = "Type"
STATUS_KEYS = {
    "backlog": "columns.backlog",
    "ready": "columns.ready",
    "in-progress": "columns.in_progress",
    "in-review": "columns.in_review",
    "done": "columns.done",
}
TYPE_TO_OPTION = {"epic": "Epic", "task": "Task", "bug": "Bug"}
CACHE_PATH = Path(".scratch/sy/github-project-cache.json")


def set_status(project_ref: str, issue_url: str, canonical: str) -> dict[str, Any]:
    return set_field(project_ref, issue_url, STATUS_FIELD, _map(canonical, status_map(), "status"))


def set_type(project_ref: str, issue_url: str, canonical: str) -> dict[str, Any]:
    return set_field(project_ref, issue_url, TYPE_FIELD, _map(canonical, TYPE_TO_OPTION, "type"))


def set_field(project_ref: str, issue_url: str, field_name: str, option_name: str) -> dict[str, Any]:
    """Ensure the issue is a project item, then set one single-select field to a named option."""
    owner, number = _parse_project_ref(project_ref)
    resolved = _resolve(owner, number, refresh=False)
    ids = _option_id(resolved, field_name, option_name)
    if ids is None:
        resolved = _resolve(owner, number, refresh=True)
        ids = _option_id(resolved, field_name, option_name)
    if ids is None:
        available = sorted((resolved["fields"].get(field_name) or {}).get("options", {}))
        raise SystemExit(
            f"project {project_ref} field {field_name!r} has no option matching {option_name!r} "
            f"(case-insensitive); available: {available}. Fix the board option or the columns.* config "
            f"key. See docs/github-setup.md."
        )
    field_id, option_id = ids
    item_id = _find_or_add_item(owner, number, resolved["project_id"], issue_url)
    _gh_json([
        "project", "item-edit",
        "--id", item_id,
        "--project-id", resolved["project_id"],
        "--field-id", field_id,
        "--single-select-option-id", option_id,
    ])
    return {"issue": issue_url, "field": field_name, "option": option_name, "item_id": item_id}


def get_item(project_ref: str, issue_url: str) -> dict[str, Any] | None:
    owner, number = _parse_project_ref(project_ref)
    for item in _raw_items(owner, number):
        norm = _normalize_item(item)
        if norm.get("url") == issue_url:
            return norm
    return None


def list_items(project_ref: str, *, type_filter: str | None, status_filter: str | None) -> list[dict[str, Any]]:
    owner, number = _parse_project_ref(project_ref)
    out = []
    for item in _raw_items(owner, number):
        norm = _normalize_item(item)
        if type_filter and norm.get("type") != type_filter:
            continue
        if status_filter and norm.get("status") != status_filter:
            continue
        out.append(norm)
    return out


def check(project_ref: str) -> dict[str, Any]:
    """Verify every canonical status and type resolves to a real option on the board."""
    owner, number = _parse_project_ref(project_ref)
    resolved = _resolve(owner, number, refresh=True)
    report: dict[str, Any] = {"project": project_ref, "ok": True, "status": {}, "type": {}}
    for canon, opt in status_map().items():
        ok = _option_id(resolved, STATUS_FIELD, opt) is not None
        report["status"][canon] = opt if ok else None
        report["ok"] = report["ok"] and ok
    for canon, opt in TYPE_TO_OPTION.items():
        ok = _option_id(resolved, TYPE_FIELD, opt) is not None
        report["type"][canon] = opt if ok else None
        report["ok"] = report["ok"] and ok
    for field in (STATUS_FIELD, TYPE_FIELD):
        report.setdefault("available", {})[field] = sorted((resolved["fields"].get(field) or {}).get("options", {}))
    return report


# ---- mapping (column names from required per-repo config; case-insensitive) ----

def status_map() -> dict[str, str]:
    """The board's column name for each canonical status, from resolved config. Fails fast."""
    out: dict[str, str] = {}
    missing: list[str] = []
    for canon, key in STATUS_KEYS.items():
        value = str(config_get(key) or "").strip()
        if value:
            out[canon] = value
        else:
            missing.append(key)
    if missing:
        raise SystemExit(
            "missing required column name(s): " + ", ".join(missing) +
            ". Set them in the repo's .shipyard/config.json; see docs/configuration.md."
        )
    return out


def _map(canonical: str, table: dict[str, str], kind: str) -> str:
    if canonical not in table:
        raise SystemExit(f"unknown canonical {kind} {canonical!r}; expected one of {sorted(table)}")
    return table[canonical]


def _option_id(resolved: dict[str, Any], field_name: str, option_name: str) -> tuple[str, str] | None:
    """Return (field_id, option_id) matching option_name case-insensitively, or None."""
    field = resolved["fields"].get(field_name)
    if not field:
        return None
    target = option_name.strip().lower()
    for name, oid in field["options"].items():
        if name.strip().lower() == target:
            return field["id"], oid
    return None


def _to_canonical(raw: str | None, table: dict[str, str]) -> str | None:
    if not raw:
        return raw
    for canon, opt in table.items():
        if opt.strip().lower() == raw.strip().lower():
            return canon
    return raw  # unmapped board value passes through rather than being dropped


def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    content = item.get("content") or {}
    return {
        "number": content.get("number"),
        "title": content.get("title") or item.get("title"),
        "url": content.get("url"),
        "type": _to_canonical(item.get("type"), TYPE_TO_OPTION),
        "status": _to_canonical(item.get("status"), status_map()),
    }


# ---- gh plumbing ---------------------------------------------------------

def _parse_project_ref(project_ref: str) -> tuple[str, str]:
    owner, sep, number = project_ref.rpartition("/")
    if not sep or not owner or not number.isdigit():
        raise SystemExit(f"--project must be <owner>/<number> (e.g. @me/3 or my-org/3); got {project_ref!r}")
    return owner, number


def _resolve(owner: str, number: str, *, refresh: bool) -> dict[str, Any]:
    key = f"{owner}/{number}"
    cache = _load_cache()
    if not refresh and key in cache:
        return cache[key]
    project = _gh_json(["project", "view", number, "--owner", owner, "--format", "json"])
    fields = _gh_json(["project", "field-list", number, "--owner", owner, "--format", "json"])
    resolved: dict[str, Any] = {"project_id": project["id"], "fields": {}}
    for field in _as_list(fields, "fields"):
        if isinstance(field, dict) and field.get("options") is not None:
            resolved["fields"][field["name"]] = {
                "id": field["id"],
                "options": {opt["name"]: opt["id"] for opt in field["options"]},
            }
    cache[key] = resolved
    _save_cache(cache)
    return resolved


def _find_or_add_item(owner: str, number: str, project_id: str, issue_url: str) -> str:
    for item in _raw_items(owner, number):
        if (item.get("content") or {}).get("url") == issue_url:
            return item["id"]
    added = _gh_json(["project", "item-add", number, "--owner", owner, "--url", issue_url, "--format", "json"])
    return added["id"]


def _raw_items(owner: str, number: str) -> list[dict[str, Any]]:
    data = _gh_json(["project", "item-list", number, "--owner", owner, "--format", "json", "--limit", "10000"])
    return _as_list(data, "items")


def _as_list(data: Any, key: str) -> list[Any]:
    if isinstance(data, dict):
        return data.get(key, [])
    return data if isinstance(data, list) else []


def _gh_json(args: list[str]) -> Any:
    proc = subprocess.run(["gh", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"gh {' '.join(args)} failed: {proc.stderr.strip()}")
    out = proc.stdout.strip()
    return json.loads(out) if out else {}


def _load_cache() -> dict[str, Any]:
    if not CACHE_PATH.is_file():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict[str, Any]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _self_test() -> None:
    import tempfile

    assert set(STATUS_KEYS) == {"backlog", "ready", "in-progress", "in-review", "done"}
    assert set(TYPE_TO_OPTION) == {"epic", "task", "bug"}

    # Column names come from the resolver now, so the test overrides the resolver, not the environment.
    columns = {"columns.backlog": "Backlog", "columns.ready": "Ready", "columns.in_progress": "In progress",
               "columns.in_review": "In review", "columns.done": "Done"}
    original = globals()["config_get"]
    globals()["config_get"] = lambda key: columns[key] if key in columns else original(key)
    try:
        sm = status_map()
        assert sm == {"backlog": "Backlog", "ready": "Ready", "in-progress": "In progress",
                      "in-review": "In review", "done": "Done"}, sm
        columns["columns.ready"] = ""
        try:
            status_map()
        except SystemExit:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected SystemExit on an unset column name")
        columns["columns.ready"] = "Ready"

        # case-insensitive option resolution against a board that uses "In progress"
        resolved = {"project_id": "P", "fields": {
            "Status": {"id": "F", "options": {"Backlog": "b", "In progress": "p", "Done": "d"}},
        }}
        assert _option_id(resolved, "Status", "In progress") == ("F", "p")
        assert _option_id(resolved, "Status", "IN PROGRESS") == ("F", "p")
        assert _option_id(resolved, "Status", "On hold") is None
        assert _to_canonical("In progress", status_map()) == "in-progress"
        assert _to_canonical("Backlog", status_map()) == "backlog"
        assert _to_canonical("Weird", status_map()) == "Weird"
        assert _normalize_item({"type": "Epic", "status": "In review",
                                "content": {"number": 3, "title": "t", "url": "u"}}) == {
            "number": 3, "title": "t", "url": "u", "type": "epic", "status": "in-review"}
    finally:
        globals()["config_get"] = original

    assert _parse_project_ref("@me/7") == ("@me", "7")
    for bad in ("me", "me/", "me/x", "7"):
        try:
            _parse_project_ref(bad)
        except SystemExit:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"expected reject: {bad!r}")

    global CACHE_PATH
    original = CACHE_PATH
    with tempfile.TemporaryDirectory() as tmp:
        CACHE_PATH = Path(tmp) / "sy" / "cache.json"
        try:
            assert _load_cache() == {}
            payload = {"o/1": {"project_id": "P", "fields": {}}}
            _save_cache(payload)
            assert _load_cache() == payload
        finally:
            CACHE_PATH = original


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GitHub Projects v2 Type/Status helper")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, extra in (("set-status", ("--issue", "--status")), ("set-type", ("--issue", "--type"))):
        p = sub.add_parser(name)
        p.add_argument("--project", required=True)
        for flag in extra:
            p.add_argument(flag, required=True)

    p_get = sub.add_parser("get")
    p_get.add_argument("--project", required=True)
    p_get.add_argument("--issue", required=True)

    p_list = sub.add_parser("list")
    p_list.add_argument("--project", required=True)
    p_list.add_argument("--type")
    p_list.add_argument("--status")

    p_check = sub.add_parser("check")
    p_check.add_argument("--project", required=True)

    p_res = sub.add_parser("resolve")
    p_res.add_argument("--project", required=True)
    p_res.add_argument("--refresh", action="store_true")

    p_clear = sub.add_parser("cache-clear")
    p_clear.add_argument("--project")

    sub.add_parser("self-test")

    args = parser.parse_args(argv)
    if args.command == "self-test":
        _self_test()
        print("gh_project self-test passed")
        return 0
    if args.command == "cache-clear":
        cache = _load_cache()
        if args.project:
            cache.pop("/".join(_parse_project_ref(args.project)), None)
            _save_cache(cache)
        elif CACHE_PATH.is_file():
            CACHE_PATH.unlink()
        return 0
    if args.command == "resolve":
        owner, number = _parse_project_ref(args.project)
        print(json.dumps(_resolve(owner, number, refresh=args.refresh), indent=2))
        return 0
    if args.command == "set-status":
        print(json.dumps(set_status(args.project, args.issue, args.status), indent=2))
        return 0
    if args.command == "set-type":
        print(json.dumps(set_type(args.project, args.issue, args.type), indent=2))
        return 0
    if args.command == "get":
        print(json.dumps(get_item(args.project, args.issue), indent=2))
        return 0
    if args.command == "list":
        print(json.dumps(list_items(args.project, type_filter=args.type, status_filter=args.status), indent=2))
        return 0
    if args.command == "check":
        report = check(args.project)
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
