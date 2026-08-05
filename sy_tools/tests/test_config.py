"""`sy_tools.config`, the resolver every `sy` tool call reads the Shipyard configuration through.

Almost everything here runs against a throwaway git checkout carrying its own layer chain, so what
is asserted is the resolver's own behaviour rather than whatever the developer's `.shipyard/` happens
to say. `scripts/sy_config.py` resolves the same chain for callers with no server to ask — a hook, a
shell script — and the tests that subprocess it are pinning that CLI's own behaviour, not parity.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from sy_tools import config, server, tracker

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


def _agreed_repo_root() -> Path | None:
    """The repo root both resolvers derive from the current environment, or None if both refuse.

    Asked of the two together because the failure this guards is a *disagreement*: the CLI runs in the
    same cwd and the same environment as this process, so the only thing that can separate them is
    their own resolution logic. One resolving while the other refuses is a disagreement too, and fails.

    The memoized root is dropped first, because the callers below change `CLAUDE_PROJECT_DIR` and then ask
    — the CLI side is a fresh process and reads the new pointer, this side would answer from the previous
    test's root and the comparison would fail on a cache rather than on a disagreement. `reset_cache()`
    and not `reload()`: `reload()` resolves eagerly, so it would raise `ConfigError` out of this helper
    before the refusal branch below could assert that both sides refuse the same bogus pointer.
    """
    config.reset_cache()
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


def test_the_whole_layer_chain_merges_in_precedence_order_and_reports_each_key_s_layer(fixture_repo):
    """Four layers, lowest precedence first: shipped defaults, user-global, repo-committed, repo-local.

    Each must win over the ones below it and each resolved key must name the layer it came from, or a
    caller has no way to know which file to edit. The two outer layers are the ones only this test
    reaches: the user-global layer must not outrank a repo's own committed settings, and the repo-local
    layer is the highest-precedence file and the only uncommitted one, so a resolver that never read it
    would still look correct against a fixture carrying a committed layer alone.
    """
    home_layer = Path.home() / ".shipyard"
    home_layer.mkdir(parents=True)
    (home_layer / "config.json").write_text(
        json.dumps({"columns": {"backlog": "Home Backlog"}, "limits": {"max_depth_agents": 7}}), encoding="utf-8"
    )
    (fixture_repo / ".shipyard" / "config.local.json").write_text(
        json.dumps({"columns": {"done": "Local Done"}}), encoding="utf-8"
    )
    config.reload()

    values, provenance = config.resolve()
    assert "$schema" not in values, "a layer's own schema pointer is not a setting"
    assert values["columns"] == {**FIXTURE_COLUMNS, "done": "Local Done"}, (
        "the committed layer must outrank the user-global one, and the local layer must outrank both"
    )
    assert provenance["columns.backlog"] == "repo-committed"
    assert provenance["columns.done"] == "repo-local"
    assert provenance["limits.max_depth_agents"] == "user-global", "a user-global key must reach the merge"
    assert provenance["transcript.attach"] == "repo-committed"
    assert provenance["ci.poll_timeout"] == "shipped-default"
    assert provenance["worktree.root"] == "derived-default"
    assert set(config._flatten(values)) <= set(provenance), "every resolved key must name a layer"


def test_resolution_reads_the_layers_of_the_repo_the_project_pointer_names(fixture_repo, monkeypatch):
    """With cwd inside a *different* checkout, only `CLAUDE_PROJECT_DIR` names the fixture.

    The deleted-var case cannot reveal a resolver that never learned the pointer: with cwd already
    inside the fixture it reads the right layers by accident. Here cwd is the plugin's own checkout and
    only the pointer names the fixture, so a resolver ignoring it reads the wrong repo's layers —
    exactly what a worktree-local `.shipyard/config.local.json` invisible to the server looks like.
    """
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(fixture_repo))
    monkeypatch.chdir(PLUGIN_ROOT)
    config.reload()

    assert config.repo_root() == fixture_repo.resolve()
    assert config.resolved_root() == fixture_repo.resolve(), "the hot values must have resolved against it too"
    values, provenance = config.resolve()
    assert values["columns"]["ready"] == FIXTURE_COLUMNS["ready"], "the pointer's repo layer must be read"
    assert provenance["columns.ready"] == "repo-committed"
    assert config.get("worktree.root") == str(fixture_repo.parent / f"{fixture_repo.name}-worktrees"), (
        "a derived default must derive from the pointer's repo, not from cwd's"
    )


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


def test_repo_scratch_dir_resolves_to_one_directory_from_every_worktree(fixture_repo):
    """The divergence this keys around is only reproducible from a linked worktree, so it is tested there.

    Claude Code exports `CLAUDE_PROJECT_DIR` to hook subprocesses but not to a subagent's own Bash
    tool, so a root keyed on `repo_root().name` resolves the main checkout from the guard's side and
    the worktree from the guarded agent's, and the hunt sandbox then denies writes the agent believes
    are permitted. Keyed on the shared git dir, the main checkout and a linked worktree of it — asked
    for either explicitly or as the resolved default — must all land on one path.
    """
    root = Path(str(config.get("scratch.dir")))
    expected = root / fixture_repo.name
    assert config.repo_scratch_dir() == expected
    assert config.repo_scratch_dir(fixture_repo) == expected

    common = config._git_common_dir(fixture_repo)
    assert common is not None and common.is_absolute(), (
        f"the derivation must stay absolute: a bare relative {common} has an empty parent name"
    )

    git = ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", "-C", str(fixture_repo)]
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "base"], check=True)
    linked = fixture_repo.parent / "linked-worktree"
    subprocess.run([*git, "worktree", "add", "-q", str(linked), "-b", "wt"], check=True)
    assert config.repo_scratch_dir(linked) == expected, "a worktree must not get its own scratch directory"
    assert config._logical_repo(linked) == fixture_repo, (
        "every per-repository derived default keys on this: `worktree.root` derived from a worktree "
        "would nest a second worktrees directory inside the first"
    )

    outside = fixture_repo.parent / "not-a-checkout"
    outside.mkdir()
    with pytest.raises(config.ConfigError, match="not a directory inside a git checkout"):
        config.repo_scratch_dir(outside)


def test_repo_scratch_dir_does_not_collapse_every_submodule_onto_one_directory(fixture_repo):
    """A submodule's shared git dir is `<superproject>/.git/modules/<name>`, so keyed on its parent's
    name every submodule on the machine would resolve the single identifier `modules`: one scratch
    directory shared by unrelated repositories, and a `worktree.root` nested inside `.git`. Keyed on
    the submodule's own working tree instead, each is as distinct as any other checkout.
    """
    root = Path(str(config.get("scratch.dir")))
    git = ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", "-c", "protocol.file.allow=always"]
    subprocess.run([*git, "-C", str(fixture_repo), "commit", "-q", "--allow-empty", "-m", "base"], check=True)

    subs = []
    for name in ("dep-alpha", "dep-beta"):
        source = fixture_repo.parent / f"source-{name}"
        source.mkdir()
        subprocess.run([*git, "-C", str(source), "init", "-q"], check=True)
        subprocess.run([*git, "-C", str(source), "commit", "-q", "--allow-empty", "-m", "base"], check=True)
        subprocess.run([*git, "-C", str(fixture_repo), "submodule", "add", "-q", str(source), name], check=True)
        subs.append(fixture_repo / name)

    super_common = config._git_common_dir(fixture_repo)
    assert super_common is not None and config._configured_worktree(super_common) is None, (
        "an ordinary checkout must not set core.worktree, so the fallback stays the normal path"
    )

    for sub in subs:
        common = config._git_common_dir(sub)
        assert common is not None and common.parent.name == "modules", (
            f"the collision this guards is gone from git, not merely handled: {common}"
        )
        assert config._logical_repo(sub) == config._git_toplevel(sub), (
            "a submodule must key on its own working tree, not on the superproject's .git/modules"
        )
        assert config.repo_scratch_dir(sub) == root / sub.name

    assert config.repo_scratch_dir(subs[0]) != config.repo_scratch_dir(subs[1]), (
        "two submodules must not share one scratch directory"
    )
    assert root / "modules" not in {config.repo_scratch_dir(s) for s in subs}

    resolved = []
    for sub in subs:
        proc = subprocess.run(
            [sys.executable, str(PLUGIN_ROOT / "scripts" / "sy_config.py"), "scratch-dir", "--repo"],
            cwd=sub, capture_output=True, text=True, check=False,
            env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT)},
        )
        assert proc.returncode == 0, proc.stderr
        resolved.append(Path(proc.stdout.strip()))
    assert resolved == [config.repo_scratch_dir(s) for s in subs], (
        f"the CLI resolver disagrees with the server resolver on submodules: {resolved}"
    )

    linked = fixture_repo.parent / "linked-submodule-worktree"
    subprocess.run([*git, "-C", str(subs[0]), "worktree", "add", "-q", str(linked), "-b", "wt"], check=True)
    assert not subprocess.run(
        ["git", "-C", str(linked), "rev-parse", "--show-superproject-working-tree"],
        capture_output=True, text=True, check=True,
    ).stdout.strip(), (
        "the case this keys around: a submodule's *linked* worktree reports no superproject, so any "
        "detection keyed on the checkout misses exactly the worktrees /sy:ship itself creates"
    )
    assert config._logical_repo(linked) == config._logical_repo(subs[0]) == subs[0], (
        "a linked worktree of a submodule must resolve the submodule's own working tree, neither "
        f".git/modules nor the worktree's own path: {config._logical_repo(linked)}"
    )
    assert config.repo_scratch_dir(linked) == config.repo_scratch_dir(subs[0]) == root / subs[0].name
    proc = subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "scripts" / "sy_config.py"), "scratch-dir", "--repo"],
        cwd=linked, capture_output=True, text=True, check=False,
        env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT)},
    )
    assert proc.returncode == 0, proc.stderr
    assert Path(proc.stdout.strip()) == config.repo_scratch_dir(subs[0]), (
        f"the CLI resolver disagrees from a submodule's linked worktree: {proc.stdout!r}"
    )


def test_repo_scratch_dir_keys_a_sparse_checkout_submodule_correctly(fixture_repo):
    """`git sparse-checkout init` inside a submodule migrates `core.worktree` from `<common>/config`
    to `<common>/config.worktree` (git's per-worktree config extension) and never reverts it on
    `sparse-checkout disable`. A resolver that only reads `<common>/config` would silently fall back
    to `common.parent` (`.git/modules`) the moment a consumer repo's submodule ever turns sparse
    checkout on, even once it is switched back off.
    """
    root = Path(str(config.get("scratch.dir")))
    git = ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", "-c", "protocol.file.allow=always"]
    subprocess.run([*git, "-C", str(fixture_repo), "commit", "-q", "--allow-empty", "-m", "base"], check=True)

    source = fixture_repo.parent / "source-dep-sparse"
    source.mkdir()
    subprocess.run([*git, "-C", str(source), "init", "-q"], check=True)
    subprocess.run([*git, "-C", str(source), "commit", "-q", "--allow-empty", "-m", "base"], check=True)
    subprocess.run([*git, "-C", str(fixture_repo), "submodule", "add", "-q", str(source), "dep-sparse"], check=True)
    sub = fixture_repo / "dep-sparse"

    subprocess.run([*git, "-C", str(sub), "sparse-checkout", "init", "--cone"], check=True)
    common = config._git_common_dir(sub)
    assert common is not None and (common / "config.worktree").is_file(), (
        "sparse-checkout must have migrated core.worktree into config.worktree for this test to be non-vacuous"
    )
    assert config._logical_repo(sub) == config._git_toplevel(sub) == sub
    assert config.repo_scratch_dir(sub) == root / "dep-sparse"

    subprocess.run([*git, "-C", str(sub), "sparse-checkout", "disable"], check=True)
    assert config._logical_repo(sub) == sub, (
        "core.worktree must still resolve from config.worktree after sparse-checkout is disabled again"
    )
    assert config.repo_scratch_dir(sub) == root / "dep-sparse", (
        "disabling sparse-checkout must not regress the identifier back to .git/modules"
    )


def test_repo_scratch_dir_keys_a_deinited_submodule_on_common_rather_than_modules(fixture_repo):
    """`git submodule deinit` clears `core.worktree` from both config files while leaving
    `.git/modules/<name>` itself in place, and any of the submodule's own linked worktrees checked out
    and healthy. Falling back to `common.parent` there would silently key on the fixed string
    `modules`, colliding with every other deinit'd submodule on the machine. `common` itself (the
    absolute git dir path, `.git/modules/<name>`) is used instead: still ends in the submodule's own
    name, still identical from every worktree of it, and never machine-global.

    This exercises the general working-tree-verification mechanism (`_is_resolved_working_tree`), not
    a `modules`-shaped pattern match: `test_logical_repo_keys_unresolvable_nested_and_detached_submodules_distinctly`
    below proves the same mechanism also catches shapes where `common.parent` is not literally named
    `modules`, which a name-pattern detector was previously found not to generalize to.
    """
    git = ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", "-c", "protocol.file.allow=always"]
    subprocess.run([*git, "-C", str(fixture_repo), "commit", "-q", "--allow-empty", "-m", "base"], check=True)

    source = fixture_repo.parent / "source-dep-deinit"
    source.mkdir()
    subprocess.run([*git, "-C", str(source), "init", "-q"], check=True)
    subprocess.run([*git, "-C", str(source), "commit", "-q", "--allow-empty", "-m", "base"], check=True)
    subprocess.run([*git, "-C", str(fixture_repo), "submodule", "add", "-q", str(source), "dep-deinit"], check=True)
    sub = fixture_repo / "dep-deinit"
    subprocess.run([*git, "-C", str(sub), "commit", "-q", "--allow-empty", "-m", "sub"], check=True)

    linked = fixture_repo.parent / "linked-deinit-submodule-worktree"
    subprocess.run([*git, "-C", str(sub), "worktree", "add", "-q", str(linked), "-b", "wt"], check=True)
    subprocess.run([*git, "-C", str(fixture_repo), "submodule", "deinit", "-f", "dep-deinit"], check=True)

    assert subprocess.run(["git", "-C", str(linked), "status", "-sb"], capture_output=True, check=True), (
        "the linked worktree must still be a healthy checkout after its submodule is deinit'd"
    )
    common = config._git_common_dir(linked)
    assert common is not None and config._configured_worktree(common) is None, (
        "deinit must have cleared core.worktree from both config files for this test to be non-vacuous"
    )
    assert not config._is_resolved_working_tree(common.parent, common), (
        ".git/modules is not itself a working tree; the fallback must not be trusted here"
    )
    root = Path(str(config.get("scratch.dir")))
    assert config._logical_repo(linked) == common
    assert config.repo_scratch_dir(linked) == root / "dep-deinit"
    proc = subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "scripts" / "sy_config.py"), "scratch-dir", "--repo"],
        cwd=linked, capture_output=True, text=True, check=False,
        env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT)},
    )
    assert proc.returncode == 0 and Path(proc.stdout.strip()) == root / "dep-deinit", (
        f"the CLI resolver must resolve the same way: rc={proc.returncode}, stdout={proc.stdout!r}, "
        f"stderr={proc.stderr!r}"
    )


def test_logical_repo_keys_unresolvable_nested_and_detached_submodules_distinctly(fixture_repo):
    """The resolution above must not be a `modules`-shaped pattern match: it must hold for any layout
    where `core.worktree` is unresolvable and `common.parent` is not itself a working tree, including
    shapes where `common.parent`'s name is not literally `modules` at all, and two unrelated such
    repos must still resolve to two distinct identifiers.

    Two such shapes, both structurally guaranteed (no co-naming or co-location needed):
    - a nested submodule (`outer/inner`), whose deinit'd common dir is
      `<super>/.git/modules/outer/modules/inner`, so `common.parent.name` is `modules` but
      `common.parent.parent.name` is `outer`, not `.git` -- the two-level name check a prior fix used
      would have missed this.
    - a submodule of a `--separate-git-dir` superproject, whose common dir is
      `<detached-gitdir>/modules/<name>` -- there is no `.git` component in its ancestry at all.
    """
    git = ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", "-c", "protocol.file.allow=always"]

    inner_src = fixture_repo.parent / "source-inner"
    outer_src = fixture_repo.parent / "source-outer"
    for src in (inner_src, outer_src):
        src.mkdir()
        subprocess.run([*git, "-C", str(src), "init", "-q"], check=True)
        subprocess.run([*git, "-C", str(src), "commit", "-q", "--allow-empty", "-m", "base"], check=True)
    subprocess.run([*git, "-C", str(outer_src), "submodule", "add", "-q", str(inner_src), "inner"], check=True)
    subprocess.run([*git, "-C", str(outer_src), "commit", "-q", "-m", "add inner"], check=True)

    nested_super = fixture_repo.parent / "nested-super"
    nested_super.mkdir()
    subprocess.run([*git, "-C", str(nested_super), "init", "-q"], check=True)
    subprocess.run([*git, "-C", str(nested_super), "commit", "-q", "--allow-empty", "-m", "base"], check=True)
    subprocess.run([*git, "-C", str(nested_super), "submodule", "add", "-q", str(outer_src), "outer"], check=True)
    subprocess.run([*git, "-C", str(nested_super), "submodule", "update", "--init", "--recursive"], check=True)
    outer = nested_super / "outer"
    inner = outer / "inner"
    subprocess.run([*git, "-C", str(outer), "commit", "-q", "--allow-empty", "-m", "sub"], check=True)
    nested_linked = nested_super.parent / "nested-inner-linked"
    subprocess.run([*git, "-C", str(inner), "worktree", "add", "-q", str(nested_linked), "-b", "wt"], check=True)
    subprocess.run([*git, "-C", str(outer), "submodule", "deinit", "-f", "inner"], check=True)

    common = config._git_common_dir(nested_linked)
    assert common is not None and common.parent.name == "modules", "fixture must reproduce the nested shape"
    assert common.parent.parent.name != ".git", (
        "must be the shape a two-level '.git'-then-'modules' name check would miss"
    )
    assert config._configured_worktree(common) is None
    assert not config._is_resolved_working_tree(common.parent, common)
    assert config._logical_repo(nested_linked) == common

    dep_src = fixture_repo.parent / "source-detached-dep"
    dep_src.mkdir()
    subprocess.run([*git, "-C", str(dep_src), "init", "-q"], check=True)
    subprocess.run([*git, "-C", str(dep_src), "commit", "-q", "--allow-empty", "-m", "base"], check=True)
    detached_work = fixture_repo.parent / "detached-work"
    detached_gitdir = fixture_repo.parent / "detached-work.git"
    subprocess.run(
        [*git, "init", "-q", f"--separate-git-dir={detached_gitdir}", str(detached_work)], check=True
    )
    subprocess.run([*git, "-C", str(detached_work), "commit", "-q", "--allow-empty", "-m", "base"], check=True)
    subprocess.run([*git, "-C", str(detached_work), "submodule", "add", "-q", str(dep_src), "dep"], check=True)
    dep = detached_work / "dep"
    subprocess.run([*git, "-C", str(dep), "commit", "-q", "--allow-empty", "-m", "sub"], check=True)
    detached_linked = detached_work.parent / "detached-dep-linked"
    subprocess.run([*git, "-C", str(dep), "worktree", "add", "-q", str(detached_linked), "-b", "wt"], check=True)
    subprocess.run([*git, "-C", str(detached_work), "submodule", "deinit", "-f", "dep"], check=True)

    common2 = config._git_common_dir(detached_linked)
    assert common2 is not None and ".git" not in common2.parts, "must be the detached-gitdir shape"
    assert config._configured_worktree(common2) is None
    assert not config._is_resolved_working_tree(common2.parent, common2)
    assert config._logical_repo(detached_linked) == common2
    assert config._logical_repo(detached_linked) != config._logical_repo(nested_linked), (
        "two unrelated unresolvable submodules must still resolve to two distinct identifiers"
    )


def test_logical_repo_resolves_a_plain_separate_git_dir_checkout_without_raising(fixture_repo):
    """`--separate-git-dir` is an ordinary, documented git feature with no submodule involved at all,
    and previously raised outright here (a real regression: no `sy_config` invocation at all -- not
    just scratch/worktree resolution -- could succeed from such a checkout, with a misleading
    "run `git submodule update --init`" message on a repo with no submodules).

    `core.worktree` is never set for a plain `--separate-git-dir` checkout, and the directory holding
    the detached gitdir is not itself a working tree, so this correctly falls through to the final,
    always-safe tier: `common` itself (the detached gitdir's own path). That is less readable than the
    checkout's own directory name, but stable and distinct -- and, most importantly, does not crash.
    """
    git = ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", "-c", "protocol.file.allow=always"]

    detached_gitdir = fixture_repo.parent / "plain-detached.git"
    detached_work = fixture_repo.parent / "plain-detached-work"
    subprocess.run(
        [*git, "init", "-q", f"--separate-git-dir={detached_gitdir}", str(detached_work)], check=True
    )
    subprocess.run([*git, "-C", str(detached_work), "commit", "-q", "--allow-empty", "-m", "base"], check=True)

    common = config._git_common_dir(detached_work)
    assert common is not None
    assert common == detached_gitdir.resolve()
    assert config._configured_worktree(common) is None, "a plain --separate-git-dir checkout never sets core.worktree"
    assert config._is_resolved_working_tree(common.parent, common) is False, (
        "the directory holding the detached gitdir is not itself the checkout"
    )
    assert config._logical_repo(detached_work) == common, (
        "must fall through to the common-dir tier rather than raise for an ordinary, submodule-free checkout"
    )
    root = Path(str(config.get("scratch.dir")))
    assert config.repo_scratch_dir(detached_work) == root / common.name


def test_scratch_dir_refuses_a_non_absolute_root(fixture_repo):
    """`review_guard.py`'s hunt-mode write sandbox is exactly `scratch_dir()`'s containment check, and
    `scratch.dir` is one of the values a repo-committed `.shipyard/config.json` can set. A relative
    value resolves against whatever the calling process's cwd happens to be rather than any fixed
    location -- a committed `{"scratch": {"dir": ".."}}` can silently put the "sandbox" root at an
    ancestor of the checkout itself, so every file inside the checkout would satisfy the containment
    check that was supposed to keep hunt out of it. Refused outright rather than resolved.
    """
    layer = {**FIXTURE_LAYER, "scratch": {"dir": ".."}}
    (fixture_repo / ".shipyard" / "config.json").write_text(json.dumps(layer), encoding="utf-8")
    config.reload()
    with pytest.raises(config.ConfigError, match="not absolute"):
        config.scratch_dir("anything")
    with pytest.raises(config.ConfigError, match="not absolute"):
        config.repo_scratch_dir(fixture_repo)


def test_same_directory_identifies_a_symlinked_alias_portably(tmp_path):
    """A basic, portable correctness check for `_same_directory` on any filesystem/OS: it identifies a
    symlinked alias by device+inode even though the two paths differ as strings.

    This does NOT discriminate `_same_directory` from the plain `a.resolve() == b.resolve()` it
    replaced -- `Path.resolve()` already normalizes symlinks, so a symlink alone does not exercise the
    regression that motivated the switch to device+inode comparison. Only a differently-*cased*
    spelling on a case-insensitive filesystem does that (the case-variant spelling in
    `test_repo_scratch_dir_refuses_a_root_that_overlaps_the_checkout` below), and CI runs
    `ubuntu-latest` only, where that spelling is never appended and the regression has no automated
    coverage. Recorded here as a known gap rather than silently assumed covered.
    """
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert str(link) != str(real)
    assert config._same_directory(link, real)
    other = tmp_path / "other"
    other.mkdir()
    assert not config._same_directory(link, other)


def test_repo_scratch_dir_refuses_a_root_that_overlaps_the_checkout(fixture_repo):
    """Even an absolute `scratch.dir` must not resolve to a directory that equals or contains the
    checkout it is asked to provide a scratch directory for -- `scratch_dir()`'s own containment check
    only constrains the *identifier* relative to the root, not the root itself, so a repo-committed
    `.shipyard/config.json` pointing `scratch.dir` at (or above) its own checkout would otherwise hand
    `review_guard.py`'s hunt-mode write sandbox the checkout's own source.

    A literal, already-canonical parent path is the vacuous spelling of this exploit -- pytest's own
    `tmp_path` is already resolved, so that spelling alone would pass even without comparing resolved
    paths. A `..`-suffixed spelling, a spelling through a symlinked ancestor, and -- on a
    case-insensitive filesystem, probed rather than assumed -- a differently-cased spelling of the
    same ancestor are exercised too, since each is indistinguishable from an ordinary absolute value
    and `Path.resolve()` alone normalizes none of them the way a device+inode comparison does.
    """
    literal_parent = str(fixture_repo.parent)
    dotdot_spelling = str(fixture_repo / "..")
    symlinked_dir = fixture_repo.parent / "symlinked-ancestor"
    symlinked_dir.symlink_to(fixture_repo.parent, target_is_directory=True)
    symlink_spelling = str(symlinked_dir)
    spellings = [literal_parent, dotdot_spelling, symlink_spelling]

    # Probed with os.path.samestat directly, never config._same_directory (the function under test):
    # a probe built from the same code path it is meant to catch a regression in would pass vacuously
    # if that code regressed to a spelling-based comparison, since the same wrong logic would decide
    # both "is this filesystem case-insensitive" and "does the case-variant spelling overlap".
    probe_dir = fixture_repo.parent / "CaseProbeDir"
    probe_dir.mkdir()
    case_variant_probe = probe_dir.parent / "caseprobedir"
    if case_variant_probe.is_dir() and os.path.samestat(case_variant_probe.stat(), probe_dir.stat()):
        case_variant_ancestor = fixture_repo.parent.parent / fixture_repo.parent.name.swapcase()
        spellings.append(str(case_variant_ancestor))

    for spelling in spellings:
        layer = {**FIXTURE_LAYER, "scratch": {"dir": spelling}}
        (fixture_repo / ".shipyard" / "config.json").write_text(json.dumps(layer), encoding="utf-8")
        config.reload()
        with pytest.raises(config.ConfigError, match="contains a worktree of this repository"):
            config.repo_scratch_dir(fixture_repo)


def test_repo_scratch_dir_refuses_a_root_that_overlaps_any_worktree_regardless_of_cwd(fixture_repo):
    """A `PreToolUse` hook's cwd is the *main* checkout in the overwhelming majority of `sy:gate`/
    `sy:hunt` runs, not the build/slice/review worktree the tool call actually targets -- `/sy:ship`
    names the worktree only in the dispatched agent's prompt text, never as the subagent's own cwd.
    So checking the overlap guard only against `start`'s own working tree is not enough: it must catch
    a `scratch.dir` that overlaps some *other*, currently-inactive worktree of the repo regardless of
    which one -- main or any linked worktree -- the current call happens to resolve from. A plausible,
    non-adversarial layout reproduces this: a `worktree.root` nested inside the resolved `scratch.dir`
    for this same repository (naturally plausible when both are configured under one shared parent),
    so the review worktree `/sy:ship` creates there lands inside the very directory the "sandbox" is
    rooted at.
    """
    git = ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", "-c", "protocol.file.allow=always"]

    shared_root = fixture_repo.parent / "shared-root"
    resolved_scratch_dir = shared_root / fixture_repo.name  # what scratch_dir(logical.name) will be
    resolved_scratch_dir.mkdir(parents=True)

    # Committed (not just written) before the worktree is created, so it is a *tracked* file git
    # actually checks out into the linked worktree too -- matching a real repo-committed
    # .shipyard/config.json, which every worktree of the repo carries an identical copy of.
    layer = {**FIXTURE_LAYER, "scratch": {"dir": str(shared_root)}}
    (fixture_repo / ".shipyard" / "config.json").write_text(json.dumps(layer), encoding="utf-8")
    subprocess.run([*git, "-C", str(fixture_repo), "add", ".shipyard/config.json"], check=True)
    subprocess.run([*git, "-C", str(fixture_repo), "commit", "-q", "-m", "malicious scratch.dir"], check=True)

    linked = resolved_scratch_dir / "AM-9999"  # the worktree.root this repo would derive there
    subprocess.run([*git, "-C", str(fixture_repo), "worktree", "add", "-q", str(linked), "-b", "wt"], check=True)
    config.reload()

    # The main checkout is the cwd that always occurs in practice, and is exactly where this must
    # refuse -- the resolved scratch directory is an ancestor of the linked worktree regardless of
    # which checkout the current invocation happens to resolve from.
    with pytest.raises(config.ConfigError, match="contains a worktree of this repository"):
        config.repo_scratch_dir(fixture_repo)

    # And from the linked worktree itself, for the same reason.
    with pytest.raises(config.ConfigError, match="contains a worktree of this repository"):
        config.repo_scratch_dir(linked)

    # review_guard.py imports scripts/sy_config.py, not sy_tools.config -- confirm that copy refuses
    # identically from both cwds, not just the in-process resolver exercised above. The committed
    # .shipyard/config.json is tracked, so it is present at the same relative path in both checkouts.
    for cwd in (fixture_repo, linked):
        proc = subprocess.run(
            [sys.executable, str(PLUGIN_ROOT / "scripts" / "sy_config.py"), "scratch-dir", "--repo"],
            cwd=cwd, capture_output=True, text=True, check=False,
            env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT)},
        )
        assert proc.returncode != 0 and "contains a worktree of this repository" in proc.stderr, (
            f"the CLI resolver must refuse the same way from {cwd}: rc={proc.returncode}, "
            f"stderr={proc.stderr!r}"
        )


def test_repo_scratch_dir_refuses_an_overlap_on_a_plain_separate_git_dir_checkout(fixture_repo, monkeypatch):
    """A `--separate-git-dir` (or bare-plus-`worktree-add`) main checkout has no `core.worktree` and
    no entry under `<common>/worktrees/` at all -- `<common>/worktrees/` only ever holds *linked*
    worktrees, never the main one. `_logical_repo` falls back to `common` (the detached gitdir itself)
    for this shape, so a fix that checks only `_all_worktrees` (which starts from `logical`) would
    compare against the *gitdir's* location, not the actual working tree `start` is sitting in right
    now -- the gitdir and the working tree share a basename here (both named `sepwork`) precisely so
    the resolved scratch directory's *name* matches either, and only comparing against
    `_git_toplevel(start)` catches the real overlap on the working tree's own, different, parent.
    """
    git = ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", "-c", "protocol.file.allow=always"]

    gitdirs_parent = fixture_repo.parent / "gitdirs"
    checkouts_parent = fixture_repo.parent / "checkouts"
    gitdirs_parent.mkdir()
    detached_gitdir = gitdirs_parent / "sepwork"
    detached_work = checkouts_parent / "sepwork"
    subprocess.run(
        [*git, "init", "-q", f"--separate-git-dir={detached_gitdir}", str(detached_work)], check=True
    )
    subprocess.run([*git, "-C", str(detached_work), "commit", "-q", "--allow-empty", "-m", "base"], check=True)

    common = config._git_common_dir(detached_work)
    assert common is not None and common == detached_gitdir.resolve()
    assert config._configured_worktree(common) is None
    assert not config._is_resolved_working_tree(common.parent, common), (
        "the directory holding the detached gitdir must not be mistaken for the checkout"
    )
    assert config._logical_repo(detached_work) == common, (
        "fixture must reproduce the tier-3 (common-dir) fallback for this test to be non-vacuous"
    )

    # detached_work is its own, unrelated checkout (not a worktree of fixture_repo), so its own
    # .shipyard/config.json -- not fixture_repo's -- is what a fresh resolver anchored there would
    # read. Written there, and cwd moved there, so the in-process and CLI resolvers agree on what a
    # real invocation from detached_work would see.
    (detached_work / ".shipyard").mkdir()
    layer = {**FIXTURE_LAYER, "scratch": {"dir": str(checkouts_parent)}}
    (detached_work / ".shipyard" / "config.json").write_text(json.dumps(layer), encoding="utf-8")
    monkeypatch.chdir(detached_work)
    config.reload()

    # The resolved scratch directory (checkouts_parent/"sepwork") is NOT an ancestor of `common`
    # (gitdirs_parent/"sepwork") -- confirming _all_worktrees([logical]) alone would miss this.
    directory = checkouts_parent / "sepwork"
    assert not config._same_directory(directory, common) and not any(
        config._same_directory(directory, p) for p in common.parents
    )
    # But it is exactly the actual working tree `start` sits in.
    assert directory.resolve() == detached_work.resolve()

    with pytest.raises(config.ConfigError, match="contains a worktree of this repository"):
        config.repo_scratch_dir(detached_work)

    # scripts/sy_config.py is the copy review_guard.py actually enforces with -- confirm it refuses
    # the same way, not just the sy_tools.config resolver exercised above.
    proc = subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "scripts" / "sy_config.py"), "scratch-dir", "--repo"],
        cwd=detached_work, capture_output=True, text=True, check=False,
        env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT)},
    )
    assert proc.returncode != 0 and "contains a worktree of this repository" in proc.stderr, (
        f"the CLI resolver must refuse the same way: rc={proc.returncode}, stderr={proc.stderr!r}"
    )


def test_all_worktrees_resolves_a_relative_gitdir_record(fixture_repo):
    """`git worktree add --relative-paths` (or `worktree.useRelativePaths`) writes the linked
    worktree's `gitdir` record as a path relative to `<common>/worktrees/<id>/` itself, not absolute
    and not relative to any process's cwd. Comparing that value as-is would silently stat whatever the
    *guard process's* own cwd happens to be instead of the worktree, missing the overlap entirely.

    The linked worktree is placed *inside* the resolved scratch directory (mirroring the
    main-checkout-overlap test above) rather than merely beside the main checkout, so only the
    (potentially broken) linked-worktree registry entry -- not the main checkout or `start`'s own
    working tree, both already covered separately -- can catch this.
    """
    git = ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", "-c", "protocol.file.allow=always"]
    subprocess.run([*git, "-C", str(fixture_repo), "commit", "-q", "--allow-empty", "-m", "base"], check=True)

    shared_root = fixture_repo.parent / "relative-shared-root"
    resolved_scratch_dir = shared_root / fixture_repo.name  # what scratch_dir(logical.name) will be
    resolved_scratch_dir.mkdir(parents=True)
    linked = resolved_scratch_dir / "AM-relative"
    subprocess.run(
        [*git, "-C", str(fixture_repo), "worktree", "add", "--relative-paths", "-q", str(linked), "-b", "wt"],
        check=True,
    )
    common = config._git_common_dir(fixture_repo)
    assert common is not None
    gitdir_files = list((common / "worktrees").glob("*/gitdir"))
    assert len(gitdir_files) == 1
    recorded = gitdir_files[0].read_text(encoding="utf-8").strip()
    assert not Path(recorded).is_absolute(), (
        "fixture must reproduce a relative gitdir record for this test to be non-vacuous"
    )

    layer = {**FIXTURE_LAYER, "scratch": {"dir": str(shared_root)}}
    (fixture_repo / ".shipyard" / "config.json").write_text(json.dumps(layer), encoding="utf-8")
    config.reload()

    # The resolved scratch directory is not an ancestor of the main checkout or of start's own working
    # tree -- both already covered separately -- so only the linked-worktree registry entry itself
    # can catch this. It must, because the linked worktree lives inside the resolved scratch directory.
    with pytest.raises(config.ConfigError, match="contains a worktree of this repository"):
        config.repo_scratch_dir(fixture_repo)

    # scripts/sy_config.py is the copy review_guard.py actually enforces with -- confirm it refuses
    # the same way, not just the sy_tools.config resolver exercised above.
    proc = subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "scripts" / "sy_config.py"), "scratch-dir", "--repo"],
        cwd=fixture_repo, capture_output=True, text=True, check=False,
        env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT)},
    )
    assert proc.returncode != 0 and "contains a worktree of this repository" in proc.stderr, (
        f"the CLI resolver must refuse the same way: rc={proc.returncode}, stderr={proc.stderr!r}"
    )


def test_all_worktrees_accepts_the_bare_directory_form_but_refuses_a_blank_record(fixture_repo):
    """`git-worktree(1)`'s DETAILS section documents writing a linked worktree's `gitdir` record as
    the bare directory path (no trailing `.git`) when hand-repairing it after moving the worktree
    outside `git worktree move` -- a form git's own reader accepts by stripping an *optional* `.git`
    suffix, not a form it requires. Refusing that form (an earlier version of this fix did) would deny
    every `sy:hunt` write, including into its own sandbox, for a repository git itself considers
    healthy. A genuinely blank record -- unambiguous corruption, not a git-documented spelling -- must
    still refuse rather than silently resolve to `entry` itself.
    """
    git = ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", "-c", "protocol.file.allow=always"]
    subprocess.run([*git, "-C", str(fixture_repo), "commit", "-q", "--allow-empty", "-m", "base"], check=True)

    linked = fixture_repo.parent / "bare-form-linked"
    subprocess.run(
        [*git, "-C", str(fixture_repo), "worktree", "add", "-q", str(linked), "-b", "wt"], check=True
    )
    common = config._git_common_dir(fixture_repo)
    assert common is not None
    gitdir_files = list((common / "worktrees").glob("*/gitdir"))
    assert len(gitdir_files) == 1
    gitdir_file = gitdir_files[0]

    # The bare directory form (hand-repair spelling): accepted, resolves to the worktree itself.
    gitdir_file.write_text(str(linked.resolve()), encoding="utf-8")
    result = config._all_worktrees(common, fixture_repo)
    assert any(config._same_directory(w, linked) for w in result), (
        "the bare-directory gitdir form must resolve to the worktree, not raise"
    )

    # A blank record: unambiguous corruption, must refuse.
    gitdir_file.write_text("", encoding="utf-8")
    with pytest.raises(config.ConfigError, match="blank"):
        config._all_worktrees(common, fixture_repo)


def test_agent_binding_reports_the_clamped_dispatch_values_and_where_they_came_from(fixture_repo):
    """Every field a dispatcher reads off a binding, on a request that clamps and one that does not.

    `sweep` is bound to `opus` by the fixture's own layer, well above its floor, so the request
    survives verbatim and nothing is reported as clamped. `img-inspector` is bound to a *tier* alias
    rather than a model name, the indirection a dispatcher must never be handed raw. The clamped case
    is constructed rather than looked for, because an agent already at its floor cannot tell a working
    clamp from a report that never sets the flag: `gate`'s floor is the one cost-scaling may never
    touch, so a layer asking for the cheapest model and the cheapest effort must come back as the
    floor's own values with both flags raised and the request still legible beside them.
    """
    assert config.agent_binding("sweep") == {
        "agent": "sweep", "model": "opus", "effort": "high", "model_requested": "opus",
        "effort_requested": "high", "model_clamped": False, "effort_clamped": False,
        "source": "repo-committed",
    }
    inspector = config.agent_binding("img-inspector")
    assert inspector["model"] in config.MODEL_ORDER, "a tier alias must be resolved to a model, never passed on"
    assert inspector["source"] == "shipped-default"

    (fixture_repo / ".shipyard" / "config.local.json").write_text(
        json.dumps({"models": {"agents": {"gate": {"model": "haiku", "effort": "low"}}}}), encoding="utf-8"
    )
    config.reload()
    floor = json.loads((PLUGIN_ROOT / "config" / "floors.json").read_text(encoding="utf-8"))["gate"]
    tiers = config.resolve()[0]["models"]["tiers"]
    assert config.agent_binding("gate") == {
        "agent": "gate", "model": tiers[floor["min_model"]], "effort": floor["min_effort"],
        "model_requested": "haiku", "effort_requested": "low", "model_clamped": True,
        "effort_clamped": True, "source": "repo-local",
    }

    with pytest.raises(config.ConfigError, match="unknown agent"):
        config.agent_binding("no-such-agent")


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


def test_validate_reports_an_outranking_subagent_model_even_with_nothing_resolvable(fixture_repo, monkeypatch):
    """`CLAUDE_CODE_SUBAGENT_MODEL` outranks every per-agent model the resolver computes, silently.

    So it is an error rather than an override, and it must reach the `validate_config` tool's own report
    — the MCP server is the only path a session takes, so a fault nothing surfaces there is a fault
    nobody sees. It must also survive a configuration that cannot be resolved at all: the check reads
    only the environment, and a root that will not resolve is no reason to hide a live problem that has
    nothing to do with it.
    """
    monkeypatch.setenv("CLAUDE_CODE_SUBAGENT_MODEL", "sonnet")
    expected = (
        "CLAUDE_CODE_SUBAGENT_MODEL is set. It outranks the per-invocation model parameter and "
        "would silently reroute every agent off the model this config resolved. Unset it."
    )
    assert expected in config.validate()
    assert expected in server.validate_config()["errors"], "it must reach the tool's own report"

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(fixture_repo / "definitely-not-a-repo"))
    config.reset_cache()
    errors = config.validate()
    assert "CLAUDE_PROJECT_DIR" in errors[0], f"the unresolvable root must be the first line: {errors}"
    assert expected in errors, f"a root failure must not swallow the checks that need no root: {errors}"

    monkeypatch.delenv("CLAUDE_PROJECT_DIR")
    monkeypatch.delenv("CLAUDE_CODE_SUBAGENT_MODEL")
    config.reset_cache()
    assert not any("CLAUDE_CODE_SUBAGENT_MODEL" in e for e in config.validate()), (
        "the check must read the live environment, not report unconditionally"
    )


def test_validate_reports_a_retired_setting_variable_still_set_in_the_environment(fixture_repo, monkeypatch):
    """A setting that used to be an environment variable now has exactly one home: a config layer.

    Left set, a retired name is a second resolution path for one key that nothing reads — so it is
    reported rather than honoured, whether or not it happens to agree with what the key now resolves to,
    and an unrecognised `SY_*` name is reported too because a typo'd setting is indistinguishable from a
    retired one to whoever exported it. The names are read out of the resolver's own map: spelling one
    here would trip the config seam `scripts/validate.py` enforces over every file but the resolvers.
    """
    disagreeing = next(name for name, path in config.LEGACY_ENV.items() if path == "columns.ready")
    agreeing = next(name for name, path in config.LEGACY_ENV.items() if path == "ci.poll_timeout")
    monkeypatch.setenv(disagreeing, "Env Ready")
    monkeypatch.setenv(agreeing, str(config.get("ci.poll_timeout")))
    monkeypatch.setenv("SY_NOT_A_REAL_SETTING", "1")

    errors = config.validate()
    assert any(
        f"{disagreeing} is set in the environment (to 'Env Ready') and disagrees with columns.ready, which "
        f"resolves to {FIXTURE_COLUMNS['ready']!r}" in e for e in errors
    ), errors
    assert any(f"{agreeing} is set in the environment and agrees with ci.poll_timeout" in e for e in errors), errors
    assert any("SY_NOT_A_REAL_SETTING is set but is not a Shipyard setting" in e for e in errors), errors
    report = server.validate_config()
    assert report["valid"] is False and any(disagreeing in e for e in report["errors"]), (
        f"it must reach the tool's own report: {report}"
    )

    for name in (disagreeing, agreeing, "SY_NOT_A_REAL_SETTING"):
        monkeypatch.delenv(name)
    assert not any("SY_" in e for e in config.validate()), "the report must read the live environment"


def test_the_config_read_tools_serve_the_resolved_values_and_hold_their_argument_contracts(fixture_repo):
    """The tools are the only way a session reads the configuration, so each one's contract is pinned.

    A refusal is a `ToolError`, which the SDK returns to the caller as a tool result rather than a
    protocol error, so a caller asking for something it may not have gets an answer it can act on.
    `scratch_dir` is the one with a contract of its own beyond the resolver's: either one identifier or
    the repository, and a call giving both or neither would otherwise silently pick one.
    """
    assert server.get_config("columns.ready") == {"key": "columns.ready", "value": "Fixture Ready"}
    assert server.get_config("columns.nonexistent", default="fallback")["value"] == "fallback"
    with pytest.raises(server.ToolError, match="unknown config key"):
        server.get_config("columns.nonexistent")
    with pytest.raises(server.ToolError, match="credential-shaped"):
        server.get_config("tracker_config.token")

    shown = server.show_config()
    assert shown["values"] == config.resolve()[0] and shown["provenance"] == config.resolve()[1]
    assert shown["fingerprint"] == server.fingerprint_config()["fingerprint"] == config.fingerprint()
    assert {layer["label"] for layer in shown["layers"]} == {"user-global", "repo-committed", "repo-local"}
    assert [layer["present"] for layer in shown["layers"]] == [False, True, False]

    assert server.agent_model("sweep") == config.agent_binding("sweep")
    with pytest.raises(server.ToolError, match="unknown agent"):
        server.agent_model("no-such-agent")

    root = Path(str(config.get("scratch.dir")))
    assert server.scratch_dir(identifier="AM-1") == {"path": str(root / "AM-1")}
    assert server.scratch_dir(repo=True) == {"path": str(root / fixture_repo.name)}
    for identifier, repo in (("", False), ("AM-1", True), ("  ", False)):
        with pytest.raises(server.ToolError, match="not both and not neither"):
            server.scratch_dir(identifier=identifier, repo=repo)
    with pytest.raises(server.ToolError, match="stays inside the resolved scratch root"):
        server.scratch_dir(identifier="..")


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


def test_migrate_reads_the_adapter_map_from_the_tracker_the_settings_block_names(tmp_path):
    """The adapter half of the legacy map belongs to the tracker being migrated *to*, not the resolved one.

    `skills/init-repo/SKILL.md` runs `migrate` at step 1b, before step 2 resolves a tracker at all, so
    on the documented path there is no `.shipyard/config.json` yet and the resolved value is whatever
    the shipped default says. Deriving the adapter's `legacy_env` names from that resolved value wrote
    a config carrying `tracker` and nothing else adapter-specific — every `tracker_config.*` variable in
    the block silently dropped, exit 0 — on the one path that is guaranteed rather than an edge case.
    """
    tracker, legacy = _a_tracker_the_shipped_defaults_do_not_select()
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    values = {
        _legacy_env_name("tracker"): tracker,
        **{name: f"legacy-value-{i}" for i, name in enumerate(sorted(legacy))},
    }
    (tmp_path / "settings.json").write_text(json.dumps({"env": values}), encoding="utf-8")

    probe = _migrate_probe(tmp_path)
    assert probe.returncode == 0, probe.stderr
    migrated = json.loads(probe.stdout)
    assert migrated["tracker"] == tracker
    for name, key in sorted(legacy.items()):
        node = migrated
        for part in key.split("."):
            assert isinstance(node, dict) and part in node, f"{key} was dropped: {probe.stdout!r}"
            node = node[part]
        assert node == values[name], f"{key} migrated the wrong value: {probe.stdout!r}"


def test_migrate_refuses_a_tracker_no_shipped_adapter_implements(tmp_path):
    """A typo'd tracker name reached the same silent drop by a different route.

    The lenient adapter lookup answers `{}` for a tracker with no `config-map.json`, which is right for
    a caller that only wants tracker metadata and wrong for a one-time conversion: it turned a
    misspelling into a config file that looked complete and had lost every adapter-specific value.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    settings = {"env": {_legacy_env_name("tracker"): "no-such-tracker"}}
    (tmp_path / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    probe = _migrate_probe(tmp_path)
    assert probe.returncode != 0, f"a tracker with no adapter must refuse: {probe.stdout!r}"
    assert probe.stdout == "", f"a refusal must not also emit a partial config: {probe.stdout!r}"
    assert "names no adapter" in probe.stderr, probe.stderr


def test_migrate_merges_into_an_existing_out_file_rather_than_truncating_it(tmp_path):
    """`--out` is pointed straight at `.shipyard/config.json` by the documented command.

    `write_text` overwrote it unconditionally, so migrating a single leftover variable onto an
    already-configured repo destroyed every key that was there — the tracker, the column names, the
    adapter's own settings — and exited 0 with nothing said. `docs/configuration.md` treats a config
    file coexisting with a lingering `env` block as a real state, and SKILL.md's instruction for this
    exact file is to preserve every existing key, so the migrated values merge over what is there.
    """
    tracker, _ = _a_tracker_the_shipped_defaults_do_not_select()
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".shipyard").mkdir()
    out = tmp_path / ".shipyard" / "config.json"
    existing = {"tracker": tracker, "columns": {"ready": "Ready", "done": "Done"}, "ci": {"poll_interval": 15}}
    out.write_text(json.dumps(existing), encoding="utf-8")
    settings = {"env": {_legacy_env_name("ci.poll_timeout"): "45"}}
    (tmp_path / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    probe = _migrate_probe(tmp_path, "--out", str(out))
    assert probe.returncode == 0, probe.stderr
    after = json.loads(out.read_text(encoding="utf-8"))
    assert after["tracker"] == tracker, f"the destination was truncated: {after!r}"
    assert after["columns"] == existing["columns"], f"pre-existing keys must survive: {after!r}"
    assert after["ci"]["poll_interval"] == 15, f"a sibling of a migrated key must survive: {after!r}"
    assert after["ci"]["poll_timeout"] == 45, f"the migrated value must still land: {after!r}"
    assert "columns.done" in json.loads(probe.stdout)["preserved"], probe.stdout


def test_migrate_leaves_an_existing_out_file_intact_when_the_write_fails(tmp_path):
    """`--out` is pointed at the repo's own `.shipyard/config.json`, so a half-written merge destroys it.

    `write_text` truncates the destination before it writes a byte, so a write that fails partway — a full
    disk, a quota, the file-size limit this probe imposes — left that file cut off mid-value and
    unparseable, every later read of it a refusal, while the operator got a raw `OSError` traceback saying
    nothing about the destination now being broken. Written through a sibling temporary file and one
    `os.replace` instead: the destination either carries the whole merge or is byte-identical to before.
    """
    tracker, _ = _a_tracker_the_shipped_defaults_do_not_select()
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".shipyard").mkdir()
    out = tmp_path / ".shipyard" / "config.json"
    out.write_text(json.dumps({"tracker": tracker, "columns": FIXTURE_COLUMNS}, indent=2) + "\n", encoding="utf-8")
    before = out.read_bytes()
    settings = {"env": {_legacy_env_name("ci.poll_timeout"): "45"}}
    (tmp_path / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    probe = _size_limited_migrate_probe(tmp_path, out, limit=64)
    assert probe.returncode != 0, f"a write that could not complete must refuse: {probe.stdout!r}"
    assert "Traceback" not in probe.stderr, f"raw traceback from the CLI: {probe.stderr!r}"
    assert "could not be written" in probe.stderr, f"the refusal must name its cause: {probe.stderr!r}"
    assert probe.stdout == "", f"a refusal must not also report a file it wrote: {probe.stdout!r}"
    assert out.read_bytes() == before, "the destination was modified by a migration that failed"
    json.loads(out.read_text(encoding="utf-8"))  # raises if the destination was left truncated
    assert not list(out.parent.glob("*.tmp")), "a failed write must not leave its temporary file behind"


def _size_limited_migrate_probe(cwd: Path, out: Path, *, limit: int) -> subprocess.CompletedProcess:
    """`sy_config.py migrate --out` in a child process whose writes fail past `limit` bytes.

    The limit is imposed after the import, so it constrains the migration's own write rather than
    anything on the way in, and `SIGXFSZ` is ignored so exceeding it arrives as the `OSError` a full disk
    or a quota would raise instead of killing the child on the signal Linux delivers alongside it.
    """
    probe = (
        "import resource, signal, sy_config\n"
        "signal.signal(signal.SIGXFSZ, signal.SIG_IGN)\n"
        f"resource.setrlimit(resource.RLIMIT_FSIZE, ({limit}, {limit}))\n"
        f"raise SystemExit(sy_config.main(['migrate', '--settings', 'settings.json', '--out', {str(out)!r}]))\n"
    )
    return subprocess.run(
        [sys.executable, "-c", probe], cwd=cwd, capture_output=True, text=True, check=False,
        env={
            **os.environ, "PYTHONPATH": str(PLUGIN_ROOT / "scripts"), "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
            "CLAUDE_PROJECT_DIR": str(cwd), "HOME": str(cwd / "home"),
        },
    )


def test_both_validators_report_two_columns_configured_under_one_name(fixture_repo):
    """The collision the canonical vocabulary refuses on, which only the MCP validator used to name.

    The CLI's `validate` and the `validate_config` tool are one contract read two ways, so the CLI
    reporting a config the tool refuses would send a repo into a session that breaks on its first
    status read. Compared line for line against `column_collisions()` rather than against a copy of the
    wording, like every other parity assertion in this file: two hand-maintained mirrors of one message
    are exactly what drifts.
    """
    colliding = {**FIXTURE_LAYER, "columns": {**FIXTURE_COLUMNS, "ready": "fixture in progress"}}
    (fixture_repo / ".shipyard" / "config.json").write_text(json.dumps(colliding), encoding="utf-8")
    config.reload()

    proc = _validate_probe(fixture_repo)

    assert proc.returncode == 0, f"validate() must return its errors, not exit: {proc.stderr}"
    reported = [line for line in proc.stdout.splitlines() if "shared by more than one" in line]
    assert len(reported) == 1, f"one collision must be one line: {proc.stdout!r}"
    assert reported == tracker.column_collisions(), "both validators must report the same sentence"
    assert "columns.ready" in reported[0] and "columns.in_progress" in reported[0], reported[0]


def test_both_validators_report_a_whitespace_only_column_as_unset(fixture_repo):
    """`"   "` is schema-valid and reads as absent everywhere else, so calling it configured is the fault.

    `columns.*` is `["string", "null"]` with no `minLength`, and `tracker.column_names()` treats a value
    as unset via `str(value or "").strip()`. Both validators tested emptiness as `in (None, "")`, so a
    whitespace-only column passed as present and the session then failed its very first status read with
    "missing required column name(s): columns.ready" — the same "validates clean, breaks on first use"
    fault `column_collisions()` exists to prevent, reached through the other predicate.
    """
    blank = {**FIXTURE_LAYER, "columns": {**FIXTURE_COLUMNS, "ready": "   "}}
    (fixture_repo / ".shipyard" / "config.json").write_text(json.dumps(blank), encoding="utf-8")
    config.reload()

    with pytest.raises(tracker.TrackerError, match=r"columns\.ready"):
        tracker.column_names()  # the first real use, which is what makes a clean report a lie

    errors = server.validate_config()["errors"]
    assert [e for e in errors if e.startswith("columns.ready is required and unset")], errors

    proc = _validate_probe(fixture_repo)
    assert proc.returncode == 0, f"validate() must return its errors, not exit: {proc.stderr}"
    reported = [line for line in proc.stdout.splitlines() if line.startswith("columns.ready is required")]
    assert len(reported) == 1, f"the CLI must name it too, once: {proc.stdout!r}"


def test_an_unset_column_is_reported_once_by_the_tool_not_twice(fixture_repo):
    """An unconfigured repo is the commonest input there is, and it named each fault twice.

    Asking `column_names()` for the collision check made it so: the required-key loop already reports
    every unset `columns.*` key, and that call added its own "missing required column name(s)" line
    naming the same five. `column_collisions()` answers the collision question alone, so a column that
    is merely unset is reported by the one check whose business it is.
    """
    (fixture_repo / ".shipyard" / "config.json").write_text(
        json.dumps({**FIXTURE_LAYER, "columns": {}}), encoding="utf-8",
    )
    config.reload()

    errors = server.validate_config()["errors"]

    for key in (f"columns.{name}" for name in ("backlog", "ready", "in_progress", "in_review", "done")):
        assert sum(key in error for error in errors) == 1, f"{key} is named twice: {errors}"
    assert not any("missing required column name(s)" in error for error in errors), errors


def test_both_validators_refuse_a_tracker_that_only_resolves_as_a_path(fixture_repo):
    """`tracker` must be checked against the enumerated adapter names, not by joining it onto a path.

    `"."` and `".."` name existing directories — `skills/tracker/` itself and its parent — so the
    `.is_dir()` form reported a clean configuration and then found no `config-map.json` for either,
    silently skipping every adapter-declared `required` key and `secret_env` variable, which is exactly
    the class of fault config validation exists to catch. A traversal like `../tracker/<adapter>` went
    further and loaded a real adapter's map under a name no adapter answers to. `sy_tools/tracker`
    refuses all three at tool-call time, so nothing is broken end to end — but catching them *before*
    runtime is the whole purpose of this check, and the guard is duplicated across both deployments, so
    both are asked rather than one trusted to stand in for the other.
    """
    adapter, _ = _a_tracker_the_shipped_defaults_do_not_select()
    layer = fixture_repo / ".shipyard" / "config.json"
    try:
        for bogus in (".", "..", f"../tracker/{adapter}"):
            layer.write_text(json.dumps({**FIXTURE_LAYER, "tracker": bogus}), encoding="utf-8")
            config.reload()
            assert any("has no adapter" in e for e in config.validate()), (
                f"the server validator passed tracker {bogus!r} clean"
            )
            proc = subprocess.run(
                [sys.executable, str(PLUGIN_ROOT / "scripts" / "sy_config.py"), "validate"],
                cwd=fixture_repo, capture_output=True, text=True, check=False, env={**os.environ},
            )
            assert proc.returncode != 0, f"the CLI validator passed tracker {bogus!r} clean: {proc.stdout!r}"
            assert "has no adapter" in proc.stderr, proc.stderr
        layer.write_text(json.dumps({**FIXTURE_LAYER, "tracker": adapter}), encoding="utf-8")
        config.reload()
        assert not any("has no adapter" in e for e in config.validate()), "a shipped adapter must pass"
    finally:
        layer.write_text(json.dumps(FIXTURE_LAYER), encoding="utf-8")
        config.reload()


def _a_tracker_the_shipped_defaults_do_not_select() -> tuple[str, dict[str, str]]:
    """A shipped adapter declaring legacy names that is *not* the tracker `defaults.json` selects.

    A block naming the default tracker cannot show the bug: the resolved answer and the block's own
    answer agree, so a resolver reading the wrong one still looks correct. Both sides are read rather
    than spelled — the names are the adapter's own vocabulary, which `scripts/validate.py`'s config
    seam fails any file but the resolver and the adapters for naming.
    """
    default = json.loads((PLUGIN_ROOT / "config" / "defaults.json").read_text(encoding="utf-8"))["tracker"]
    for config_map in sorted((PLUGIN_ROOT / "skills" / "tracker").glob("*/config-map.json")):
        legacy = json.loads(config_map.read_text(encoding="utf-8")).get("legacy_env", {})
        if legacy and config_map.parent.name != default:
            return config_map.parent.name, legacy
    pytest.fail("no shipped adapter declaring legacy_env differs from the default tracker")


def _legacy_env_name(config_key: str) -> str:
    """The retired environment variable name for one config key, read out of the resolver's own map."""
    proc = subprocess.run(
        [sys.executable, "-c", (
            f"import sy_config; print(next(n for n, p in sy_config.LEGACY_ENV.items() if p == {config_key!r}))"
        )],
        capture_output=True, text=True, check=True,
        env={**os.environ, "PYTHONPATH": str(PLUGIN_ROOT / "scripts")},
    )
    return proc.stdout.strip()


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


def _migrate_probe(cwd: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    """`sy_config.py migrate` onto stdout by default, where a partial conversion is visible in full."""
    return subprocess.run(
        [
            sys.executable, str(PLUGIN_ROOT / "scripts" / "sy_config.py"),
            "migrate", "--settings", "settings.json", *args,
        ],
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


def test_the_server_validator_reports_a_layer_corrupted_after_the_cache_warmed(fixture_repo):
    """The guard around `resolve()` cannot cover this one: once the hot copy is warm, it never fails again.

    This deployment resolves once per *process lifetime*, so after the first successful call the only
    things still touching disk are the per-layer schema pass and the adapter map. A layer edited into
    invalid JSON after that point — and `reload()` is the only thing that would notice — raised straight
    out of `validate_config`, the one tool whose entire job is diagnosing exactly this fault: the
    operator got `Error executing tool validate_config: ...` instead of the report it promises. Reachable
    for the whole life of a long-running server, not a race, so `validate()` reports it warm or cold.
    """
    assert not any("not valid JSON" in e for e in config.validate()), "the layer must parse before it is corrupted"
    config.fingerprint()  # warms the hot copy, exactly as any earlier tool call would have

    layer = fixture_repo / ".shipyard" / "config.json"
    layer.write_text('{"columns": {"ready": "Ready",}}', encoding="utf-8")
    try:
        errors = config.validate()  # no reload(): the hot copy is still the good one
        report = server.validate_config()
    finally:
        layer.write_text(json.dumps(FIXTURE_LAYER), encoding="utf-8")  # the fixture re-resolves on teardown
    assert any("not valid JSON" in e for e in errors), errors
    assert report["valid"] is False and any("not valid JSON" in e for e in report["errors"]), report


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


@pytest.mark.parametrize("command", [["show"], ["get", "tracker"], ["fingerprint"], ["validate"]])
def test_a_deleted_working_directory_is_refused_by_every_cli_subcommand(tmp_path, command):
    """The cwd guard was pushed into `sy_tools/config.py` but never mirrored into the CLI's own resolver.

    `validate` has a guard of its own, so it was the only subcommand that reported a deleted working
    directory cleanly; `show`, `get` and `fingerprint` reach `repo_root()` with nothing between them and
    `Path.cwd()`, and each tracebacked raw — the same shape as the missing-`git` fail-open, on the same
    call paths, and the same asymmetry (one guarded caller standing in for every unguarded one).
    """
    gone = tmp_path / "deleted-under-us"
    gone.mkdir()
    proc = _deleted_cwd_probe(gone, *command)
    assert proc.returncode == 1, f"the CLI must refuse, not crash or resolve: {proc.stdout!r} {proc.stderr!r}"
    assert "Traceback" not in proc.stderr, f"raw traceback from the CLI: {proc.stderr!r}"
    assert "working directory could not be read" in proc.stderr, proc.stderr


def _deleted_cwd_probe(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """One `sy_config.py` subcommand, from a working directory the child process deletes under itself.

    Deleted from inside rather than before the spawn: `subprocess` needs the directory to exist to start
    the child at all, and the fault is a directory that disappears under a process already sitting in it.
    """
    probe = f"import os, sy_config\nos.rmdir(os.getcwd())\nraise SystemExit(sy_config.main({list(args)!r}))\n"
    return subprocess.run(
        [sys.executable, "-c", probe], cwd=cwd, capture_output=True, text=True, check=False,
        env={
            **{k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"},
            "PYTHONPATH": str(PLUGIN_ROOT / "scripts"), "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
            "HOME": str(cwd.parent / "home"),
        },
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


GIT_CALL_SITES = ("--show-toplevel", "--git-common-dir", "core.worktree", "--is-inside-work-tree")
"""One marker per git call site config resolution reaches, in the order a cold resolution reaches them."""


def test_a_wedged_git_is_refused_rather_than_hanging_every_tool_call(tmp_path, monkeypatch):
    """The same call site as the test above, failing the one way no `except` clause can catch.

    A `git` that blocks rather than fails — a wrapper or credential helper waiting on something, a binary
    that does not return — is not an exception this resolver could have handled: it is the server never
    answering. And this resolver is the hotter of the two twins, because every tool call reaches a
    resolved value through it, where the CLI's own `_git_toplevel` runs once per process; the CLI's was
    bounded first only because the secret gate's fail-open reaches that one. So the bound is asserted on
    the call's own kwargs, not just on the refusal: `TimeoutExpired` is raised here by the fake either
    way, so removing `timeout=` from the real code would still produce a `ConfigError` and pass a test
    that only checked that. What proves the hang is actually bounded is that the real call asks for it.

    Every call site is covered, not just the first. Resolution shells out to git in four places
    (`GIT_CALL_SITES`) and the fake wedges on the *last* of them, so `seen` accumulates the real kwargs
    of every earlier one before the refusal: wedging on the first call meant `seen` held a single entry,
    and an `all(...)` over one element stayed green while the other three sites carried no `timeout=` at
    all — which is exactly how three unbounded calls arrived here unnoticed. Each site is asserted to
    have been reached as well, so removing a call rather than its bound also fails.
    """
    seen: list[dict] = []
    commands: list[list[str]] = []

    def wedge(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        commands.append(list(cmd))
        seen.append(kwargs)
        if "--is-inside-work-tree" in cmd:  # the last site a cold resolution reaches
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=config.GIT_TIMEOUT_SECONDS)
        if "--show-toplevel" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{tmp_path}\n", stderr="")
        if "--git-common-dir" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{tmp_path / '.git'}\n", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")  # no core.worktree

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # so worktree.root stays derived, as it is by default
    monkeypatch.setattr(config.subprocess, "run", wedge)
    with pytest.raises(config.ConfigError, match="is a working tree") as raised:
        config.reload()
    assert f"within {config.GIT_TIMEOUT_SECONDS}s" in str(raised.value), raised.value
    assert "CLAUDE_PROJECT_DIR" not in str(raised.value), "a wedged binary is not the pointer's fault"
    assert len(seen) > 1, f"the refusal must come from a later site, with earlier ones recorded: {commands}"
    for site in GIT_CALL_SITES:
        assert any(site in cmd for cmd in commands), f"{site} was never reached, so its bound is unproven"
    assert all(kwargs.get("timeout") == config.GIT_TIMEOUT_SECONDS for kwargs in seen), (
        f"only `timeout=` can refuse a hang, and every real call must pass it: {list(zip(commands, seen, strict=True))}"
    )
    assert all(kwargs.get("stdin") is subprocess.DEVNULL for kwargs in seen), (
        f"no git call may inherit the server's JSON-RPC stdin: {list(zip(commands, seen, strict=True))}"
    )


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


def test_the_root_resolving_git_call_does_not_inherit_the_servers_stdin(fixture_repo, monkeypatch):
    """Config resolution runs inside the MCP server, whose stdin is the JSON-RPC transport.

    Same invariant the tracker adapters pin for their own spawns: a child that inherits this stdin
    can consume a frame the server was going to read, desynchronising the session.
    """
    seen: list[dict] = []
    real_run = subprocess.run

    def record(cmd, **kwargs):
        seen.append(kwargs)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(config.subprocess, "run", record)
    config.reload()
    assert seen and all(kwargs.get("stdin") == subprocess.DEVNULL for kwargs in seen), (
        f"a git call was handed the server's own stdin: {seen}"
    )


def test_the_repo_root_resolves_once_per_process_refusal_included(fixture_repo, tmp_path, monkeypatch):
    """The bound `GIT_TIMEOUT_SECONDS` documents is per resolution, so resolution must happen once.

    This resolver is on the path every tool call takes to a resolved value and it shelled out to `git
    rev-parse` on each of them. Worse for the failing case, which is what the bound is for: one failed
    `get()` asks for the root twice — the credential-shape gate resolves first and the value's own
    `resolve()` asks again — so an unmemoized refusal made the real wait on a wedged git twice the
    number anyone reading that constant would expect. The refusal is therefore memoized alongside the
    answer, exactly as `scripts/sy_config.py::repo_root` memoizes its own `SystemExit`.

    Spawn counts are the assertion because a returned value cannot tell a fresh resolution from a cached
    one, and `reset_cache()` clearing *both* is the other half: a root that outlived the cache it is
    stored beside would leave `reload()` re-reading the previous repository's layers.
    """
    calls: list[list[str]] = []
    real_run = subprocess.run

    def record(cmd, **kwargs):
        calls.append(list(cmd))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(config.subprocess, "run", record)
    config.reset_cache()
    resolved = config.repo_root()
    assert config.repo_root() == resolved and len(calls) == 1, f"the root must be resolved once: {calls}"

    bogus = tmp_path.parent / "not-a-checkout"  # exists, so git really runs and reports no checkout
    bogus.mkdir(exist_ok=True)
    config.reset_cache()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(bogus))
    with pytest.raises(config.ConfigError, match="CLAUDE_PROJECT_DIR"):
        config.repo_root()
    refused_after = len(calls)
    with pytest.raises(config.ConfigError, match="CLAUDE_PROJECT_DIR"):
        config.repo_root()
    assert len(calls) == refused_after, f"a memoized refusal must not shell out again: {calls}"

    monkeypatch.delenv("CLAUDE_PROJECT_DIR")
    config.reset_cache()
    assert config.repo_root() == resolved, "reset_cache() must clear the refusal, not only the answer"
    assert len(calls) == refused_after + 1, f"and the call after it must really re-resolve: {calls}"

    calls.clear()
    config.reload()
    assert calls, "reload() must clear the root too, or it re-reads the layers of the previous repo"


def test_a_git_timeout_is_retried_on_the_next_call_rather_than_refusing_the_session(fixture_repo, monkeypatch):
    """This module backs a long-lived server, so a transient refusal must not outlive the transient fault.

    A timeout says nothing about the repository — only that git did not answer inside five seconds, which a
    momentary index lock or a slow filesystem produces — and memoizing that verdict turned one hiccup into a
    server that refused every later tool call for the rest of its uptime. `reset_cache()` *is* reachable
    without a restart — the `reload_config` tool reaches it through `config.reload()`, and that is the
    recovery path for the settled refusal which is memoized — but nothing tells a client that a five-second
    git hiccup is what it is now stuck on, so clearing it would depend on someone guessing to reload the
    configuration, where the retry needs no client to know anything. The CLI twin in `scripts/sy_config.py`
    keeps memoizing its own, correctly: its process ends with the command. A settled refusal is still
    memoized here — the case above covers that — so this pins the distinction, not a blanket un-memoizing.
    """
    real_run = subprocess.run
    wedged = [True]

    def hang(cmd, **kwargs):
        if wedged[0]:
            raise subprocess.TimeoutExpired(cmd, config.GIT_TIMEOUT_SECONDS)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(config.subprocess, "run", hang)
    config.reset_cache()
    with pytest.raises(config.ConfigError, match="did not resolve the repository root"):
        config.repo_root()
    wedged[0] = False
    assert config.repo_root() == fixture_repo, "a timeout must not be remembered: the next call must retry"


def test_the_other_two_settled_root_refusals_are_memoized_as_well(fixture_repo, monkeypatch):
    """Memoization is decided by exception class, so all three settled refusals must behave alike.

    The case above pins only the `CLAUDE_PROJECT_DIR`-is-not-a-checkout branch, and the other two are
    environment faults reached by different code paths: a git that cannot be run at all, and a working
    directory that can no longer be read under a long-lived server. Neither can come out differently on
    the next call, so both belong on the remembered side with it, and only the timeout on the retried
    side. The property asserted is repeat-call consistency: the next call refuses with the same class and
    the same message, and without resolving again — which, unlike a spawn count alone, also holds for the
    branch that never reaches git, since each side counts its own attempts here.

    Deliberately *not* object identity, which this used to assert. `raise` extends an exception's own
    `__traceback__` in place, so re-raising one cached instance grew that chain by two frames per call —
    ~400 frames and ~53KB of rendered traceback after 200 calls in a server that stays up refusing. The
    frame count of the raised exception is therefore asserted constant across repeats, because the
    cheapest way to be repeat-consistent is exactly the one that leaks.

    Each break is scoped to its own `monkeypatch.context()` because `fixture_repo`'s teardown resolves
    the configuration again, and a still-broken git or cwd fails that teardown rather than this test.
    """
    def frames(exc: BaseException) -> int:
        traceback, depth = exc.__traceback__, 0
        while traceback is not None:
            traceback, depth = traceback.tb_next, depth + 1
        return depth

    def repeats(first: config.ConfigError, attempts: list) -> None:
        attempted = len(attempts)
        counts = []
        for _ in range(20):
            with pytest.raises(config.ConfigError) as again:
                config.repo_root()
            counts.append(frames(again.value))
        assert (type(again.value), str(again.value)) == (type(first), str(first)), (
            f"a memoized refusal must repeat itself: {again.value!r} after {first!r}"
        )
        assert len(attempts) == attempted, f"and must not resolve again: {attempts}"
        assert len(set(counts)) == 1, (
            f"each refusal must be a fresh exception, or its traceback grows per call: {counts}"
        )

    attempts: list = []

    def unrunnable(cmd, **kwargs):
        attempts.append(list(cmd))
        raise FileNotFoundError(2, "No such file or directory: 'git'")

    with monkeypatch.context() as mp:
        mp.setattr(config.subprocess, "run", unrunnable)
        config.reset_cache()
        with pytest.raises(config.ConfigError, match="git could not be run") as first:
            config.repo_root()
        repeats(first.value, attempts)

    reads: list = []

    def dead_cwd():
        reads.append(1)
        raise FileNotFoundError(2, "the working directory no longer exists")

    with monkeypatch.context() as mp:
        mp.setattr(config.Path, "cwd", staticmethod(dead_cwd))
        config.reset_cache()
        with pytest.raises(config.ConfigError, match="working directory could not be read") as first_cwd:
            config.repo_root()
        repeats(first_cwd.value, reads)
    config.reset_cache()


def test_env_present_reports_presence_and_reads_an_empty_variable_as_absent(monkeypatch):
    """The presence-only primitive `check_env` serves, pinned where it lives rather than only at the tool.

    Empty-counts-as-absent is the load-bearing half: it matches `_post_resolution_violations`' own
    `secret_env` loop exactly, so a credential exported empty is a missing credential to both, and the
    return type is `bool` so there is nothing for a caller to accidentally render.
    """
    monkeypatch.setenv("SY_ENV_PRESENT_PROBE", "anything at all")
    assert config.env_present("SY_ENV_PRESENT_PROBE") is True
    monkeypatch.setenv("SY_ENV_PRESENT_PROBE", "")
    assert config.env_present("SY_ENV_PRESENT_PROBE") is False, "an empty variable holds no credential"
    monkeypatch.delenv("SY_ENV_PRESENT_PROBE")
    assert config.env_present("SY_ENV_PRESENT_PROBE") is False


def test_a_name_the_environment_cannot_encode_is_absent_rather_than_a_crash():
    """`os.environ.get("\\ud800")` raises `UnicodeEncodeError`, which reached `check_env` as a crash.

    A refusal is fine and a `False` is fine; an internal error out of a tool whose whole job is a
    presence answer is not. Nothing can export an unpaired surrogate, so absent is also the true answer.
    """
    assert config.env_present("\ud800") is False
    assert config.env_present("ACLI_\udfff_TOKEN") is False
