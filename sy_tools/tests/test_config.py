"""`sy_tools.config` must resolve byte-identically to the shipped CLI resolver.

The MCP deployment reimplements resolution rather than importing it, so parity is a claim that
has to be tested, not asserted. Both resolvers run over the same fixture layer chain and their
resolved values and provenance are compared key for key.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from sy_tools import config

PLUGIN_ROOT = Path(__file__).resolve().parents[2]

FIXTURE_COLUMNS = {
    "backlog": "Fixture Backlog", "ready": "Fixture Ready", "in_progress": "Fixture In Progress",
    "in_review": "Fixture In Review", "done": "Fixture Done",
}
FIXTURE_LAYER = {
    "$schema": "https://raw.githubusercontent.com/nearmap/shipyard/main/config/schema.json",
    "columns": FIXTURE_COLUMNS,
    "models": {"agents": {"sweep": {"model": "opus"}}},
    "transcript": {"attach": True},
    "redaction": {"extra_words": ["bearer"]},
}


def _cli(root: Path, *args: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "scripts" / "sy_config.py"), *args],
        cwd=root, capture_output=True, text=True, check=False,
        env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT)},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _agreed_repo_root() -> Path | None:
    """The repo root both resolvers derive from the current environment, or None if both refuse.

    Asked of the two together because the failure this guards is a *disagreement*: the CLI runs in the
    same cwd and the same environment as this process, so the only thing that can separate them is
    their own resolution logic. One resolving while the other refuses is a disagreement too, and fails.
    """
    proc = subprocess.run(
        [sys.executable, "-c", "import sy_config; print(sy_config.repo_root())"],
        capture_output=True, text=True, check=False,
        env={**os.environ, "PYTHONPATH": str(PLUGIN_ROOT / "scripts")},
    )
    if proc.returncode != 0:
        with pytest.raises(config.ConfigError, match="CLAUDE_PROJECT_DIR"):
            config.repo_root()
        assert "CLAUDE_PROJECT_DIR" in proc.stderr, proc.stderr
        return None
    resolved = config.repo_root()
    assert resolved == Path(proc.stdout.strip()), (
        f"the resolvers disagree: sy_tools.config says {resolved}, sy_config.py says {proc.stdout.strip()!r}"
    )
    return resolved


@pytest.fixture
def fixture_repo(tmp_path, monkeypatch):
    """A throwaway git checkout carrying one committed config layer, with both resolvers pointed at it."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".shipyard").mkdir()
    (tmp_path / ".shipyard" / "config.json").write_text(json.dumps(FIXTURE_LAYER), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN_ROOT))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # keep the user-global layer out of the fixture
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)  # exercise the cwd-derived fallback by default
    monkeypatch.chdir(tmp_path)
    config.reload()
    yield tmp_path
    config.reload()


def test_repo_root_prefers_claude_project_dir_over_cwd(fixture_repo, tmp_path, monkeypatch):
    """A `pixi run <declared-task>` dispatch resets cwd to the manifest's own directory (a real,
    measured pixi behaviour — see `sy_tools/server.py`'s module docstring), so `repo_root()` must
    not trust cwd when Claude Code's own pointer is available; it should win even when cwd disagrees.

    The pointer resolves through git exactly as cwd does, so a pointer at a *subdirectory* of the
    checkout still lands on the checkout root — which is where the only `.shipyard/` layers are.
    """
    other = tmp_path.parent / "not-the-cwd"
    (other / "deep" / "nested").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=other, check=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(other / "deep" / "nested"))

    assert _agreed_repo_root() == other.resolve(), "a subdirectory pointer must resolve to the checkout root"
    assert fixture_repo != other, "the fixture must actually be a different directory than cwd"


def test_a_claude_project_dir_that_names_no_checkout_is_refused(fixture_repo, tmp_path, monkeypatch):
    """Silently falling back leaves every layer above the shipped defaults unread and says nothing.

    That is the shape of the failure: `tracker` reports `shipped-default`, `columns.ready` is None,
    and no error names the pointer that caused it — so the pointer is validated instead.
    """
    for bogus in (tmp_path / "definitely-not-a-repo", tmp_path / "not-a-repo" / "either"):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(bogus))
        assert _agreed_repo_root() is None, f"{bogus} resolved to something rather than being refused"
    monkeypatch.delenv("CLAUDE_PROJECT_DIR")  # the fixture teardown re-resolves before monkeypatch unwinds


def test_repo_root_falls_back_to_git_toplevel_without_the_env_var(fixture_repo):
    """Every invocation Claude Code doesn't launch (manual `pixi run sy-server`, `docs/smoke_mcp.py`,
    pytest itself) has no `CLAUDE_PROJECT_DIR` to read, so `repo_root()` must keep resolving from cwd.
    """
    assert _agreed_repo_root() == fixture_repo.resolve()


def test_resolution_matches_the_cli_resolver(fixture_repo):
    expected = _cli(fixture_repo, "show", "--json")
    values, provenance = config.resolve()
    assert values == expected["values"], "resolved values must match sy_config.py show --json exactly"
    assert provenance == expected["provenance"], "each key must be attributed to the same layer"


def test_resolution_matches_the_cli_resolver_through_the_project_pointer(fixture_repo, tmp_path, monkeypatch):
    """Parity with the env pointer set, from a cwd that is a *different* checkout.

    The deleted-var case cannot reveal a resolver that never learned the pointer: with cwd already
    inside the fixture both agree by accident. Here cwd is the plugin's own checkout and only the
    pointer names the fixture, so a resolver ignoring it reads the wrong repo's layers — which is
    exactly what a worktree-local `.shipyard/config.local.json` invisible to one side looks like.
    """
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(fixture_repo))
    monkeypatch.chdir(PLUGIN_ROOT)
    config.reload()

    assert _agreed_repo_root() == fixture_repo.resolve()
    expected = _cli(PLUGIN_ROOT, "show", "--json")
    values, provenance = config.resolve()
    assert values["columns"]["ready"] == FIXTURE_COLUMNS["ready"], "the pointer's repo layer must be read"
    assert values == expected["values"], "resolved values must match sy_config.py show --json exactly"
    assert provenance == expected["provenance"], "each key must be attributed to the same layer"


def test_layer_precedence_and_derived_defaults(fixture_repo):
    assert config.get("columns.ready") == "Fixture Ready", "a repo layer must win over the shipped default"
    assert config.get("transcript.attach") is True
    assert config.get("limits.max_depth_agents") == 3, "an unset key must fall through to the shipped default"
    assert config.get("worktree.root") == str(fixture_repo.parent / f"{fixture_repo.name}-worktrees")
    assert config.resolve()[1]["worktree.root"] == "derived-default"
    assert config.get("scratch.dir") == str(Path.home() / ".claude" / "shipyard" / "scratch")
    assert config.resolve()[1]["scratch.dir"] == "derived-default"


def test_scratch_dir_creates_under_the_resolved_root_and_refuses_an_escape(fixture_repo):
    """An identifier that resolves anywhere but strictly inside the root is refused, root included.

    `"."` and `"./"` are the load-bearing cases: they have no path parts, so a string-shaped guard
    admits them and hands back the scratch root itself — shared by every identifier, and deleted by
    the first caller that cleans up what it was given.
    """
    root = Path(str(config.get("scratch.dir")))
    created = config.scratch_dir("AM-1")
    assert created == root / "AM-1"
    assert created.is_dir(), "scratch_dir must create the directory it returns"

    outside = fixture_repo / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    escapes = ("", ".", "./", " ", "..", "../elsewhere", "a/../../b", "link/x", "a\0b", str(fixture_repo))
    for escape in escapes:
        with pytest.raises(config.ConfigError, match="stays inside the resolved scratch root"):
            config.scratch_dir(escape)
    assert not any(outside.iterdir()), "a symlink inside the scratch root must not be followed out of it"


def test_scratch_dir_refuses_the_same_identifiers_as_the_cli_resolver(fixture_repo):
    """The guard is duplicated across the two deployments, so its rejections are compared, not trusted."""
    for identifier in (".", "..", "a/../../b"):
        proc = subprocess.run(
            [sys.executable, str(PLUGIN_ROOT / "scripts" / "sy_config.py"), "scratch-dir", identifier],
            cwd=fixture_repo, capture_output=True, text=True, check=False,
            env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT)},
        )
        assert proc.returncode != 0, f"the CLI accepted {identifier!r}: {proc.stdout}"
        assert "stays inside the resolved scratch root" in proc.stderr, proc.stderr
        with pytest.raises(config.ConfigError, match="stays inside the resolved scratch root"):
            config.scratch_dir(identifier)


def test_agent_binding_matches_the_cli_resolver(fixture_repo):
    for agent in ("sweep", "gate", "ship-build", "img-inspector"):
        assert config.agent_binding(agent) == _cli(fixture_repo, "agent", agent, "--json"), agent


def test_no_agent_resolves_below_its_shipped_floor(fixture_repo):
    """A floor is a quality floor, not a cost dial: resolution may raise one, never lower it."""
    tiers = config.resolve()[0]["models"]["tiers"]
    floors = json.loads((PLUGIN_ROOT / "config" / "floors.json").read_text(encoding="utf-8"))
    order = config.MODEL_ORDER
    for agent, floor in floors.items():
        resolved = config.agent_binding(agent)["model"]
        minimum = tiers.get(floor["min_model"], floor["min_model"])
        assert order.index(resolved) >= order.index(minimum), f"{agent} resolved below its floor"


def test_unknown_key_raises_but_an_explicit_default_does_not(fixture_repo):
    with pytest.raises(config.ConfigError, match="unknown config key"):
        config.get("columns.nonexistent")
    assert config.get("columns.nonexistent", default=None) is None


def test_credential_shaped_keys_are_refused(fixture_repo):
    for key in ("tracker_config.token", "tracker_config.api_key", "some.password"):
        with pytest.raises(config.ConfigError, match="credential-shaped"):
            config.get(key)


def test_validate_reports_a_missing_required_key(fixture_repo, monkeypatch):
    broken = {**FIXTURE_LAYER, "columns": {**FIXTURE_COLUMNS, "ready": ""}}
    (fixture_repo / ".shipyard" / "config.json").write_text(json.dumps(broken), encoding="utf-8")
    config.reload()
    assert any("columns.ready is required" in e for e in config.validate())


def test_the_cli_validator_collects_a_bogus_project_pointer_rather_than_exiting(tmp_path):
    """`sy_config.validate()` reaches `repo_root()` through `layers()`, before it calls `resolve()`.

    Only the `resolve()` call was guarded, so a `CLAUDE_PROJECT_DIR` naming no checkout exited the
    process from inside the one function whose contract is to *return* every problem it found as a
    string. Any in-process caller — a hook, another script — got a `SystemExit` where it expected a
    list, and the pointer's error surfaced as a crash rather than as one collected line. Asserted
    through a subprocess because the escape is a process exit, which an in-process call cannot
    distinguish from the collected result once it has already happened.

    Also pinned here: what a resolution failure may and may not suppress. The pointer's own error comes
    *first*, because the legacy-variable comparison absorbs the same failure into an empty flat config
    and would lead with "disagrees with <key>, which resolves to None" — wrong on its face, since the
    shipped defaults give that key a value. That derived line must be gone, but a check that needs
    nothing resolved must survive: `CLAUDE_CODE_SUBAGENT_MODEL` reads only the environment, and an
    unusable root is no reason to stop reporting it. Both variables are set here, so the assertions
    cannot pass on a `validate()` that returns the root failure alone.
    """
    proc = _validate_probe(
        tmp_path,
        # Set through the resolver's own map: spelling a retired variable's name in this file would
        # trip the config seam that `scripts/validate.py` enforces over every file but the resolver.
        preamble="os.environ[next(n for n, p in sy_config.LEGACY_ENV.items() if p == 'ci.poll_timeout')] = '60'\n",
        CLAUDE_PROJECT_DIR=str(tmp_path / "definitely-not-a-repo"),
        CLAUDE_CODE_SUBAGENT_MODEL="sonnet",
    )
    assert proc.returncode == 0, f"validate() exited instead of returning its errors: {proc.stderr}"
    lines = proc.stdout.strip().splitlines()
    assert "CLAUDE_PROJECT_DIR" in lines[0], f"the real cause must be the first line: {proc.stdout!r}"
    assert any("CLAUDE_CODE_SUBAGENT_MODEL" in line for line in lines[1:]), (
        f"a root failure must not swallow the checks that need no root: {proc.stdout!r}"
    )
    assert "resolves to None" not in proc.stdout, f"no derived follow-on error: {proc.stdout!r}"


def test_the_cli_validator_reports_a_git_it_cannot_run_rather_than_crashing(tmp_path):
    """`repo_root()` shells out to `git`, so a `git` missing from `PATH` raises a plain `OSError`.

    The guard caught only `SystemExit`, so that escaped as an uncaught `FileNotFoundError` from the
    one function contracted to return its problems as strings — the same fail-open that took the
    secret-scanning hook down with it.
    """
    proc = _validate_probe(tmp_path, PATH=str(_empty_bin(tmp_path)))
    assert proc.returncode == 0, f"validate() crashed instead of collecting the failure: {proc.stderr}"
    assert "git could not be run" in proc.stdout, f"the failure must name its cause: {proc.stdout!r}"


def test_the_cli_validator_collects_an_unreadable_layer_rather_than_exiting(tmp_path):
    """The per-layer schema pass re-reads every present layer, outside the guard around `resolve()`.

    A layer the process cannot read — a bad mode, a dead symlink target — arrives from `_load_json` as
    the module's own `SystemExit`: the right shape for the CLI, the wrong one inside the function
    contracted to *return* its problems as strings, where it exited the process from the schema pass
    instead. Resolution is now asked first, so the schema pass only ever reads files the resolver has
    already read successfully.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".shipyard").mkdir()
    layer = tmp_path / ".shipyard" / "config.json"
    layer.write_text(json.dumps(FIXTURE_LAYER), encoding="utf-8")
    layer.chmod(0o000)
    try:
        proc = _validate_probe(tmp_path, HOME=str(tmp_path / "home"))
    finally:
        layer.chmod(0o644)
    assert proc.returncode == 0, f"validate() exited instead of returning its errors: {proc.stderr}"
    assert "could not be read" in proc.stdout, f"the failure must name its cause: {proc.stdout!r}"


def test_migrate_refuses_rather_than_writing_a_config_missing_the_adapter_variables(tmp_path):
    """`migrate` is a one-time, data-preserving conversion, so a partial result must be a refusal.

    Half the legacy map is the selected adapter's own `legacy_env` block, reached through
    `_adapter_map()`, which degrades to `{}` on any resolution failure — best-effort is right for a
    caller that only wants tracker metadata, and wrong here. With `git` off `PATH` that degradation
    made `migrate` exit 0 having quietly dropped every `tracker_config.*` variable from the file whose
    entire purpose is to carry them across: the same inputs migrated the adapter's keys with `git`
    present and omitted them without it, with nothing printed about what vanished.
    """
    tracker, legacy = _an_adapter_declaring_legacy_env()
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".shipyard").mkdir()
    (tmp_path / ".shipyard" / "config.json").write_text(json.dumps({"tracker": tracker}), encoding="utf-8")
    values = {name: f"legacy-value-{i}" for i, name in enumerate(sorted(legacy))}
    (tmp_path / "settings.json").write_text(json.dumps({"env": values}), encoding="utf-8")

    with_git = _migrate_probe(tmp_path)
    assert with_git.returncode == 0, with_git.stderr
    for value in values.values():
        assert value in with_git.stdout, f"{value} never migrated at all: {with_git.stdout!r}"

    without_git = _migrate_probe(tmp_path, PATH=str(_empty_bin(tmp_path)))
    assert without_git.returncode != 0, (
        f"migrate wrote a config missing the adapter's own keys: {without_git.stdout!r}"
    )
    assert without_git.stdout == "", f"a refusal must not also emit a partial config: {without_git.stdout!r}"
    assert "git could not be run" in without_git.stderr, without_git.stderr


def _an_adapter_declaring_legacy_env() -> tuple[str, dict[str, str]]:
    """One shipped tracker adapter that declares legacy variable names of its own, read from the adapter.

    Read rather than spelled: those names are the adapter's own vocabulary, and `scripts/validate.py`'s
    config seam fails any file but the resolver and the adapters themselves for naming one.
    """
    for config_map in sorted((PLUGIN_ROOT / "skills" / "tracker").glob("*/config-map.json")):
        legacy = json.loads(config_map.read_text(encoding="utf-8")).get("legacy_env", {})
        if legacy:
            return config_map.parent.name, legacy
    pytest.fail("no shipped tracker adapter declares legacy_env")


def _migrate_probe(cwd: Path, **env: str) -> subprocess.CompletedProcess:
    """`sy_config.py migrate` onto stdout, where a silently partial conversion is visible in full."""
    return subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "scripts" / "sy_config.py"), "migrate", "--settings", "settings.json"],
        cwd=cwd, capture_output=True, text=True, check=False,
        env={
            **os.environ, "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT), "CLAUDE_PROJECT_DIR": str(cwd),
            "HOME": str(cwd / "home"), **env,
        },
    )


def test_the_server_validator_collects_an_unreadable_layer_rather_than_raising(fixture_repo):
    """`validate_config`'s contract is to report a broken config rather than crash on one.

    `_load_json` named a missing file and invalid JSON but let `PermissionError` through, so the SDK
    turned it into an `isError` result carrying a raw traceback string instead of the clean report the
    tool promises. The CLI resolver guards this; the module docstring claims the two resolve
    identically, and error handling is part of that.
    """
    layer = fixture_repo / ".shipyard" / "config.json"
    layer.chmod(0o000)
    try:
        with pytest.raises(config.ConfigError, match="could not be read"):
            config.reload()  # drops the hot state, then cannot re-resolve
        errors = config.validate()  # ... which validate() must report rather than raise again
    finally:
        layer.chmod(0o644)
    assert any("could not be read" in e for e in errors), errors


def test_a_working_directory_that_cannot_be_read_is_refused_by_name(tmp_path, monkeypatch):
    """The cwd fallback calls `Path.cwd()`, which a deleted working directory answers with `OSError`.

    A server process outlives the directory it was launched from, so this is the resolver's own fault
    to name — not a raw `FileNotFoundError` out of whichever tool call resolved first, and not
    something `validate()` may raise.
    """
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN_ROOT))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    gone = tmp_path / "deleted-under-us"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()

    with pytest.raises(config.ConfigError, match="working directory could not be read"):
        config.reload()
    assert any("working directory could not be read" in e for e in config.validate()), (
        "validate() must collect the failure rather than raise it"
    )


@pytest.mark.parametrize("pointer", [None, "self"])
def test_an_unrunnable_git_is_refused_by_name_from_every_call_path(tmp_path, monkeypatch, pointer):
    """A missing `git` binary must not traceback out of *any* caller, under either resolution path.

    `validate()` guards its own `repo_root()` call, but it was the only thing standing between an
    absent binary and a crash: `resolve()`, `fingerprint()` and the `show`/`get` subcommands reach
    `repo_root()` too and used to raise `FileNotFoundError` straight through. For the server that is
    the worse half — its resolver runs on every tool call — so the guard now lives in `_git_toplevel`
    and both resolvers are asked here through a non-`validate()` path.

    Refused rather than degraded, and refused *distinguishably*: a bogus pointer and an absent binary
    are separate causes, so the pointer's "not a directory inside a git checkout" must not be what a
    missing binary reports, and the no-pointer case must not take the cwd fallback silently.
    """
    monkeypatch.setenv("PATH", str(_empty_bin(tmp_path)))
    monkeypatch.chdir(tmp_path)
    if pointer:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    else:
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)

    with pytest.raises(config.ConfigError, match="git could not be run") as raised:
        config.reload()
    assert "CLAUDE_PROJECT_DIR" not in str(raised.value), "a missing binary is not the pointer's fault"

    probe = subprocess.run(  # the CLI's `get` path: a caller `validate()`'s own guard never covers
        [sys.executable, str(PLUGIN_ROOT / "scripts" / "sy_config.py"), "get", "tracker"],
        cwd=tmp_path, capture_output=True, text=True, check=False,
        env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT)},
    )
    assert probe.returncode == 1, f"the CLI must refuse, not crash or resolve: {probe.stderr!r}"
    assert "Traceback" not in probe.stderr, f"raw traceback from the CLI: {probe.stderr!r}"
    assert "git could not be run" in probe.stderr, f"the refusal must name its cause: {probe.stderr!r}"


def _empty_bin(tmp_path: Path) -> Path:
    """A directory holding no `git`, for use as the whole of `PATH`."""
    empty = tmp_path / "no-git-here"
    empty.mkdir(exist_ok=True)
    return empty


def _validate_probe(cwd: Path, preamble: str = "", **env: str) -> subprocess.CompletedProcess:
    """`sy_config.validate()` in a subprocess: the escape under test is a process exit or a crash."""
    probe = (
        "import os, sy_config\n"
        f"{preamble}"
        "errors = sy_config.validate()\n"
        "assert all(isinstance(e, str) for e in errors), errors\n"
        "print('\\n'.join(errors))\n"
    )
    base = {
        **{k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"},
        "PYTHONPATH": str(PLUGIN_ROOT / "scripts"), "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
    }
    return subprocess.run(
        [sys.executable, "-c", probe], cwd=cwd, capture_output=True, text=True, check=False,
        env={**base, **env},
    )


def test_reload_picks_up_an_edit_and_reports_the_change(fixture_repo):
    before = config.fingerprint()
    changed = {**FIXTURE_LAYER, "columns": {**FIXTURE_COLUMNS, "done": "Shipped"}}
    (fixture_repo / ".shipyard" / "config.json").write_text(json.dumps(changed), encoding="utf-8")
    summary = config.reload()
    assert summary["changed"] is True
    assert summary["previous_fingerprint"] == before
    assert config.get("columns.done") == "Shipped"
    assert config.reload()["changed"] is False, "a reload with no edit must report no change"
