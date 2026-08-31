"""`sy_tools.config`, the resolver every `sy` tool call reads the Shipyard configuration through.

Almost everything here runs against a throwaway git checkout carrying its own layer chain, so what
is asserted is the resolver's own behaviour rather than whatever the developer's `.shipyard/` happens
to say. This is the only resolver there is: nothing else reads the layer chain, so every case below
drives this module in-process.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

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


def _resolved_repo_root() -> Path | None:
    """The repo root resolved from the current environment, or None when the pointer was refused."""
    # Dropped first: the callers below change `CLAUDE_PROJECT_DIR` and then ask, so this would answer
    # from the previous test's root. Not `reload()`, which resolves eagerly and raises before the
    # refusal can be inspected.
    config.reset_cache()
    try:
        return config.repo_root()
    except config.ConfigError as refusal:
        assert "CLAUDE_PROJECT_DIR" in str(refusal), f"a refusal must name the pointer it read: {refusal}"
        return None


@pytest.fixture
def fixture_repo(tmp_path, monkeypatch):
    """A throwaway git checkout carrying one committed config layer, with the resolver pointed at it."""
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
    """Claude Code's pointer outranks cwd, and a pointer at a subdirectory still lands on the root.

    A `pixi run <declared-task>` dispatch resets cwd to the manifest's own directory (measured; see
    `sy_tools/server.py`'s module docstring), so cwd cannot be trusted when the pointer is available.
    """
    other = tmp_path.parent / "not-the-cwd"
    (other / "deep" / "nested").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=other, check=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(other / "deep" / "nested"))

    assert _resolved_repo_root() == other.resolve(), "a subdirectory pointer must resolve to the checkout root"
    assert fixture_repo != other, "the fixture must actually be a different directory than cwd"


def test_a_claude_project_dir_that_names_no_checkout_is_refused(fixture_repo, tmp_path, monkeypatch):
    """Silently falling back leaves every layer above the shipped defaults unread and says nothing.

    That is the shape of the failure: `tracker` reports `shipped-default`, `columns.ready` is None,
    and no error names the pointer that caused it — so the pointer is validated instead.
    """
    for bogus in (tmp_path / "definitely-not-a-repo", tmp_path / "not-a-repo" / "either"):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(bogus))
        assert _resolved_repo_root() is None, f"{bogus} resolved to something rather than being refused"
    monkeypatch.delenv("CLAUDE_PROJECT_DIR")  # the fixture teardown re-resolves before monkeypatch unwinds


def test_repo_root_falls_back_to_git_toplevel_without_the_env_var(fixture_repo):
    """Every invocation Claude Code doesn't launch (manual `pixi run sy-server`, `docs/smoke_mcp.py`,
    pytest itself) has no `CLAUDE_PROJECT_DIR` to read, so `repo_root()` must keep resolving from cwd.
    """
    assert _resolved_repo_root() == fixture_repo.resolve()


def test_the_whole_layer_chain_merges_in_precedence_order_and_reports_each_key_s_layer(fixture_repo):
    """Four layers, lowest precedence first: shipped defaults, user-global, repo-committed, repo-local.

    Each must win over the ones below it and each resolved key must name the layer it came from, or a
    caller has no way to know which file to edit.
    """
    # The user-global layer must not outrank a repo's own committed settings.
    home_layer = Path.home() / ".shipyard"
    home_layer.mkdir(parents=True)
    (home_layer / "config.json").write_text(
        json.dumps({"columns": {"backlog": "Home Backlog"}, "limits": {"max_depth_agents": 7}}), encoding="utf-8"
    )
    # Highest precedence and uncommitted: unread, the committed layer alone would still look correct.
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
    inside the fixture it reads the right layers by accident. Here a resolver ignoring the pointer
    reads the wrong repo's layers — the symptom being a worktree-local layer the server never sees.
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

    Handing back the scratch root itself shares one directory with every identifier, which the first
    caller that cleans up what it was given then deletes.
    """
    root = Path(str(config.get("scratch.dir")))
    created = config.scratch_dir("AM-1")
    assert created == root / "AM-1"
    assert created.is_dir(), "scratch_dir must create the directory it returns"

    outside = fixture_repo / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    # `.` and `./` have no path parts at all, so a string-shaped guard admits them.
    escapes = ("", ".", "./", " ", "..", "../elsewhere", "a/../../b", "link/x", "a\0b", str(fixture_repo))
    for escape in escapes:
        with pytest.raises(config.ConfigError, match="stays inside the resolved scratch root"):
            config.scratch_dir(escape)
    assert not any(outside.iterdir()), "a symlink inside the scratch root must not be followed out of it"


def test_repo_scratch_dir_resolves_to_one_directory_from_every_worktree(fixture_repo):
    """The main checkout and a linked worktree of it land on one path, however the root is asked for.

    Claude Code exports `CLAUDE_PROJECT_DIR` to hook subprocesses but not to a subagent's own Bash
    tool, so a root keyed on `repo_root().name` resolves the main checkout from the guard's side and
    the worktree from the guarded agent's, and the hunt sandbox then denies writes the agent believes
    are permitted. Only a linked worktree reproduces that divergence, so it is tested there.
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

    The mechanism is `_is_resolved_working_tree`, not a `modules`-shaped pattern match: the test below
    carries the shapes where `common.parent` is not named `modules` at all.
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


def test_logical_repo_keys_unresolvable_nested_and_detached_submodules_distinctly(fixture_repo):
    """The resolution above must hold wherever `core.worktree` is unresolvable and `common.parent` is
    not itself a working tree -- not only where that parent is named `modules` -- and two unrelated
    such repos must still resolve to two distinct identifiers.

    Two structurally guaranteed shapes, needing no co-naming or co-location: a nested submodule, whose
    deinit'd common dir is `<super>/.git/modules/outer/modules/inner`, and a submodule of a
    `--separate-git-dir` superproject, whose common dir has no `.git` component in its ancestry at all.
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
    """`--separate-git-dir` is an ordinary documented git feature with no submodule involved at all.

    Unhandled, no configuration resolution at all succeeded from such a checkout and the refusal told a
    submodule-free repo to run `git submodule update --init`. `core.worktree` is never set for this
    shape and the directory holding the detached gitdir is not itself a working tree, so it falls
    through to the always-safe tier, `common` itself: less readable than the checkout's own directory
    name, but stable, distinct, and not a crash.
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
    """`_same_directory` identifies a symlinked alias by device+inode though the paths differ as strings.

    This does NOT discriminate it from the `a.resolve() == b.resolve()` it replaced: `Path.resolve()`
    already normalizes symlinks. Only a differently-*cased* spelling on a case-insensitive filesystem
    does, which the overlap test below appends and CI (`ubuntu-latest` only) never reaches -- a known
    coverage gap, recorded rather than assumed covered.
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
    """Even an absolute `scratch.dir` must not equal or contain the checkout it is asked to serve.

    `scratch_dir()`'s own containment check constrains the *identifier* relative to the root, not the
    root itself, so a repo-committed `.shipyard/config.json` pointing `scratch.dir` at (or above) its
    own checkout would otherwise hand `review_guard.py`'s hunt-mode write sandbox the checkout's source.
    """
    # The vacuous spelling: pytest's `tmp_path` is already resolved, so this one passes without
    # comparing resolved paths at all. The others are what `Path.resolve()` alone does not normalize.
    literal_parent = str(fixture_repo.parent)
    dotdot_spelling = str(fixture_repo / "..")
    symlinked_dir = fixture_repo.parent / "symlinked-ancestor"
    symlinked_dir.symlink_to(fixture_repo.parent, target_is_directory=True)
    symlink_spelling = str(symlinked_dir)
    spellings = [literal_parent, dotdot_spelling, symlink_spelling]

    # Probed with os.path.samestat, never `config._same_directory` (the function under test): a probe
    # built from that code path would pass vacuously if it regressed to a spelling-based comparison.
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
    """The guard must catch a `scratch.dir` overlapping *any* worktree, whichever one the call resolves.

    A `PreToolUse` hook's cwd is the *main* checkout in the overwhelming majority of `sy:gate`/`sy:hunt`
    runs, not the worktree the tool call targets -- `/sy:ship` names the worktree only in the dispatched
    agent's prompt text, never as the subagent's own cwd. A non-adversarial layout reproduces it: a
    `worktree.root` nested inside the same repository's resolved `scratch.dir`, as happens naturally
    when both are configured under one shared parent.
    """
    git = ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", "-c", "protocol.file.allow=always"]

    shared_root = fixture_repo.parent / "shared-root"
    resolved_scratch_dir = shared_root / fixture_repo.name  # what scratch_dir(logical.name) will be
    resolved_scratch_dir.mkdir(parents=True)

    # Committed before the worktree is created, so git checks this tracked file out into the linked
    # worktree too -- as every worktree of a repo carries its committed .shipyard/config.json.
    layer = {**FIXTURE_LAYER, "scratch": {"dir": str(shared_root)}}
    (fixture_repo / ".shipyard" / "config.json").write_text(json.dumps(layer), encoding="utf-8")
    subprocess.run([*git, "-C", str(fixture_repo), "add", ".shipyard/config.json"], check=True)
    subprocess.run([*git, "-C", str(fixture_repo), "commit", "-q", "-m", "malicious scratch.dir"], check=True)

    linked = resolved_scratch_dir / "AM-9999"  # the worktree.root this repo would derive there
    subprocess.run([*git, "-C", str(fixture_repo), "worktree", "add", "-q", str(linked), "-b", "wt"], check=True)
    config.reload()

    # The cwd that always occurs in practice, and the resolved scratch directory is an ancestor of the
    # linked worktree whichever checkout the invocation resolves from.
    with pytest.raises(config.ConfigError, match="contains a worktree of this repository"):
        config.repo_scratch_dir(fixture_repo)

    # And from the linked worktree itself, for the same reason.
    with pytest.raises(config.ConfigError, match="contains a worktree of this repository"):
        config.repo_scratch_dir(linked)


def test_repo_scratch_dir_refuses_an_overlap_on_a_plain_separate_git_dir_checkout(fixture_repo, monkeypatch):
    """The overlap must be caught for a main checkout whose gitdir is not inside its working tree.

    Such a checkout has no `core.worktree` and no entry under `<common>/worktrees/`, which only ever
    holds *linked* worktrees, so `_logical_repo` falls back to `common` and a check reading only
    `_all_worktrees` compares against the gitdir's location rather than the working tree `start` is in.
    The gitdir and the working tree are given the same basename precisely so the resolved scratch
    directory's *name* matches either, and only `_git_toplevel(start)` catches the real overlap.
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

    # detached_work is an unrelated checkout, so its own .shipyard/config.json -- not fixture_repo's --
    # is what a resolver anchored there reads; cwd is moved too, so this is what a real invocation sees.
    (detached_work / ".shipyard").mkdir()
    layer = {**FIXTURE_LAYER, "scratch": {"dir": str(checkouts_parent)}}
    (detached_work / ".shipyard" / "config.json").write_text(json.dumps(layer), encoding="utf-8")
    monkeypatch.chdir(detached_work)
    config.reload()

    # Not an ancestor of `common`, confirming _all_worktrees([logical]) alone would miss this.
    directory = checkouts_parent / "sepwork"
    assert not config._same_directory(directory, common) and not any(
        config._same_directory(directory, p) for p in common.parents
    )
    # But it is exactly the actual working tree `start` sits in.
    assert directory.resolve() == detached_work.resolve()

    with pytest.raises(config.ConfigError, match="contains a worktree of this repository"):
        config.repo_scratch_dir(detached_work)


def test_all_worktrees_resolves_a_relative_gitdir_record(fixture_repo):
    """`git worktree add --relative-paths` (or `worktree.useRelativePaths`) writes the linked
    worktree's `gitdir` record as a path relative to `<common>/worktrees/<id>/` itself, not absolute
    and not relative to any process's cwd. Comparing that value as-is would silently stat whatever the
    *guard process's* own cwd happens to be instead of the worktree, missing the overlap entirely.
    """
    git = ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", "-c", "protocol.file.allow=always"]
    subprocess.run([*git, "-C", str(fixture_repo), "commit", "-q", "--allow-empty", "-m", "base"], check=True)

    shared_root = fixture_repo.parent / "relative-shared-root"
    resolved_scratch_dir = shared_root / fixture_repo.name  # what scratch_dir(logical.name) will be
    resolved_scratch_dir.mkdir(parents=True)
    # Inside the resolved scratch directory, so only the registry entry -- not the main checkout or
    # `start`'s own working tree, both covered separately -- can catch the overlap.
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

    with pytest.raises(config.ConfigError, match="contains a worktree of this repository"):
        config.repo_scratch_dir(fixture_repo)


def test_all_worktrees_accepts_the_bare_directory_form_but_refuses_a_blank_record(fixture_repo):
    """A git-documented `gitdir` spelling must be accepted; unambiguous corruption must still refuse.

    `git-worktree(1)`'s DETAILS section documents the bare directory path (no trailing `.git`) for
    hand-repairing a moved worktree, and git's own reader strips an *optional* `.git` suffix. Refusing
    that form would deny every `sy:hunt` write, its own sandbox included, for a repository git itself
    considers healthy.
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
    rather than a model name, the indirection a dispatcher must never be handed raw.
    """
    assert config.agent_binding("sweep") == {
        "agent": "sweep", "model": "opus", "effort": "high", "model_requested": "opus",
        "effort_requested": "high", "model_clamped": False, "effort_clamped": False,
        "source": "repo-committed",
    }
    inspector = config.agent_binding("img-inspector")
    assert inspector["model"] in config.MODEL_ORDER, "a tier alias must be resolved to a model, never passed on"
    assert inspector["source"] == "shipped-default"

    # The clamped case is constructed, not looked for: an agent already at its floor cannot tell a
    # working clamp from a report that never sets the flag.
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

    # Set too, because the retired-name comparison absorbs the same resolution failure into an empty
    # flat config and would then lead with a "resolves to None" line that buries the real cause.
    # Read from the resolver's own map: spelling a retired name here would trip the config seam.
    retired = next(name for name, path in config.LEGACY_ENV.items() if path == "ci.poll_timeout")
    monkeypatch.setenv(retired, "60")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(fixture_repo / "definitely-not-a-repo"))
    config.reset_cache()
    errors = config.validate()
    assert "CLAUDE_PROJECT_DIR" in errors[0], f"the unresolvable root must be the first line: {errors}"
    assert expected in errors, f"a root failure must not swallow the checks that need no root: {errors}"
    assert not any("resolves to None" in e for e in errors), f"no derived follow-on error: {errors}"

    monkeypatch.delenv(retired)
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
    retired one to whoever exported it.
    """
    # Read from the resolver's own map: spelling a retired name here would trip the config seam.
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


def test_validate_maps_every_adapters_legacy_names_not_only_the_resolved_ones(fixture_repo, monkeypatch):
    """A migration's starting state still resolves to the tracker being migrated *away* from.

    This report is the whole migration worklist now that no `migrate` command exists, so it has to be
    adapter-agnostic. Mapped from the resolved adapter alone, the incoming adapter's legacy names — the
    ones an operator mid-migration actually has set — fell through to the unknown-`SY_*` branch and were
    reported as "not a Shipyard setting", i.e. "delete these", for the values being migrated.
    """
    # Read off the resolver rather than spelled here, for the same reason the test above does it: naming
    # one adapter's variables in this file would trip the config seam.
    resolved = str(config.get("tracker"))
    incoming = next(name for name in config._known_trackers() if name != resolved)
    mine = config.adapter_map().get("legacy_env", {})
    theirs = {
        name: path for name, path in config._all_adapters_legacy_env().items()
        if name not in mine and name.startswith("SY_")
    }
    assert theirs, "another shipped adapter must declare a prefixed legacy name for this to test anything"
    tracker_var = next(name for name, path in config.LEGACY_ENV.items() if path == "tracker")
    monkeypatch.setenv(tracker_var, incoming)
    for name in theirs:
        monkeypatch.setenv(name, "carried over")

    errors = config.validate()
    assert resolved != incoming, "the resolved tracker must not be the one being migrated to"
    for name, path in theirs.items():
        assert any(
            f"{name} is set in the environment (to 'carried over') and disagrees with {path}" in e
            for e in errors
        ), f"{name} must resolve to {path} rather than be reported as unknown: {errors}"
        assert not any(f"{name} is set but is not a Shipyard setting" in e for e in errors), errors


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


def test_the_validator_reports_two_columns_configured_under_one_name(fixture_repo):
    """The collision the canonical vocabulary refuses on, which the validator has to name up front.

    The canonical vocabulary matches a column name ignoring case and returns its first hit, so two
    statuses under one name leave an issue in that column reporting as only one of them for every
    reader — a config that validates clean and breaks on the first status read.
    """
    colliding = {**FIXTURE_LAYER, "columns": {**FIXTURE_COLUMNS, "ready": "fixture in progress"}}
    (fixture_repo / ".shipyard" / "config.json").write_text(json.dumps(colliding), encoding="utf-8")
    config.reload()

    reported = [e for e in server.validate_config()["errors"] if "shared by more than one" in e]

    assert len(reported) == 1, f"one collision must be one line: {reported}"
    # Its own sentence, not a copy: two hand-maintained mirrors of one message are what drifts.
    assert reported == tracker.column_collisions(), "the report must be that function's own sentence"
    assert "columns.ready" in reported[0] and "columns.in_progress" in reported[0], reported[0]


def test_the_validator_reports_a_whitespace_only_column_as_unset(fixture_repo):
    """`"   "` is schema-valid and reads as absent everywhere else, so calling it configured is the fault.

    `columns.*` is `["string", "null"]` with no `minLength`, and `tracker.column_names()` treats a value
    as unset via `str(value or "").strip()`, so an emptiness test of `in (None, "")` passes it as present
    and the session fails its very first status read — "validates clean, breaks on first use".
    """
    blank = {**FIXTURE_LAYER, "columns": {**FIXTURE_COLUMNS, "ready": "   "}}
    (fixture_repo / ".shipyard" / "config.json").write_text(json.dumps(blank), encoding="utf-8")
    config.reload()

    with pytest.raises(tracker.TrackerError, match=r"columns\.ready"):
        tracker.column_names()  # the first real use, which is what makes a clean report a lie

    errors = server.validate_config()["errors"]
    assert [e for e in errors if e.startswith("columns.ready is required and unset")], errors


def test_an_unset_column_is_reported_once_by_the_tool_not_twice(fixture_repo):
    """An unconfigured repo is the commonest input there is, and each fault must be named once.

    The required-key loop already reports every unset `columns.*` key, so asking `column_names()` for
    the collision check added a second "missing required column name(s)" line naming the same five.
    """
    (fixture_repo / ".shipyard" / "config.json").write_text(
        json.dumps({**FIXTURE_LAYER, "columns": {}}), encoding="utf-8",
    )
    config.reload()

    errors = server.validate_config()["errors"]

    for key in (f"columns.{name}" for name in ("backlog", "ready", "in_progress", "in_review", "done")):
        assert sum(key in error for error in errors) == 1, f"{key} is named twice: {errors}"
    assert not any("missing required column name(s)" in error for error in errors), errors


def test_the_validator_refuses_a_tracker_that_only_resolves_as_a_path(fixture_repo):
    """`tracker` must be checked against the enumerated adapter names, not by joining it onto a path.

    `"."` and `".."` name existing directories — `skills/tracker/` itself and its parent — so the
    `.is_dir()` form reports a clean configuration and then finds no `config-map.json` for either,
    silently skipping every adapter-declared `required` key and `secret_env` variable. A traversal like
    `../tracker/<adapter>` goes further and loads a real adapter's map under a name no adapter answers
    to. `sy_tools/tracker` refuses all three at tool-call time; catching them before runtime is this
    check's whole purpose.
    """
    adapter = _a_shipped_adapter_name()
    layer = fixture_repo / ".shipyard" / "config.json"
    try:
        for bogus in (".", "..", f"../tracker/{adapter}"):
            layer.write_text(json.dumps({**FIXTURE_LAYER, "tracker": bogus}), encoding="utf-8")
            config.reload()
            assert any("has no adapter" in e for e in config.validate()), (
                f"the validator passed tracker {bogus!r} clean"
            )
        layer.write_text(json.dumps({**FIXTURE_LAYER, "tracker": adapter}), encoding="utf-8")
        config.reload()
        assert not any("has no adapter" in e for e in config.validate()), "a shipped adapter must pass"
    finally:
        layer.write_text(json.dumps(FIXTURE_LAYER), encoding="utf-8")
        config.reload()


def _a_shipped_adapter_name() -> str:
    """The name of one tracker adapter this checkout actually ships."""
    # Read rather than spelled: an adapter's own name is vocabulary the seam checks fail this file for.
    names = sorted(p.parent.name for p in (PLUGIN_ROOT / "skills" / "tracker").glob("*/config-map.json"))
    if not names:
        pytest.fail("no shipped tracker adapter declares a config-map.json")
    return names[0]


def test_the_server_validator_collects_an_unreadable_layer_rather_than_raising(fixture_repo):
    """`validate_config`'s contract is to report a broken config rather than crash on one.

    An unreadable layer left unnamed reaches the SDK as an `isError` result carrying a raw traceback
    string instead of the clean report the tool promises.
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

    This deployment resolves once per *process lifetime*, so after the first successful call only the
    per-layer schema pass and the adapter map still touch disk. A layer edited into invalid JSON after
    that point is reachable for the whole life of a long-running server, not a race, so `validate()`
    must report it warm as well as cold rather than raising out of the one tool that diagnoses it.
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


@pytest.mark.parametrize("pointer", [None, "self"])
def test_an_unrunnable_git_is_refused_by_name_from_every_call_path(tmp_path, monkeypatch, pointer):
    """A missing `git` binary must not traceback out of *any* caller, under either resolution path.

    `validate()` guards its own `repo_root()` call, but `resolve()` and `fingerprint()` reach
    `repo_root()` too, on the path every tool call takes to a resolved value, so the guard lives in
    `_git_toplevel` and is asked here through a non-`validate()` path as well. Refused rather than
    degraded and refused *distinguishably*: a bogus pointer and an absent binary are separate causes,
    and the no-pointer case must not take the cwd fallback silently.
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
    assert any("git could not be run" in e for e in config.validate()), (
        "validate() must collect the failure rather than raise it"
    )


GIT_CALL_SITES = ("--show-toplevel", "--git-common-dir", "core.worktree", "--is-inside-work-tree")
"""One marker per git call site config resolution reaches, in the order a cold resolution reaches them."""


def test_a_wedged_git_is_refused_rather_than_hanging_every_tool_call(tmp_path, monkeypatch):
    """The same call site as the test above, failing the one way no `except` clause can catch.

    A `git` that blocks rather than fails — a wrapper or credential helper waiting on something, a binary
    that does not return — is not an exception this resolver could have handled: it is the server never
    answering, on the path every tool call takes to a resolved value.
    """
    seen: list[dict] = []
    commands: list[list[str]] = []

    def wedge(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        commands.append(list(cmd))
        seen.append(kwargs)
        # Wedged on the *last* site, so `seen` holds every earlier call's real kwargs: wedging the first
        # left `all(...)` running over one entry while the other three sites carried no `timeout=`.
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
    # Asserted on the real kwargs, not the refusal: the fake raises `TimeoutExpired` either way, so a
    # test checking only the `ConfigError` would pass with `timeout=` removed from the real code.
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


def test_reload_picks_up_an_edit_and_reports_the_change(fixture_repo):
    before = config.fingerprint()
    changed = {**FIXTURE_LAYER, "columns": {**FIXTURE_COLUMNS, "done": "Shipped"}}
    (fixture_repo / ".shipyard" / "config.json").write_text(json.dumps(changed), encoding="utf-8")
    summary = config.reload()
    assert summary["changed"] is True
    assert summary["previous_fingerprint"] == before
    assert config.get("columns.done") == "Shipped"
    assert config.reload()["changed"] is False, "a reload with no edit must report no change"


def test_the_fingerprint_moves_with_the_plugin_build_and_not_only_with_the_values(
    fixture_repo, tmp_path, monkeypatch,
):
    """`/sy:ship`'s mid-run drift guard compares this digest, so it has to cover the whole plugin.

    `config/floors.json`'s model and effort floors and `agents/*.md`'s `effort:` frontmatter are
    config-relevant without being resolved values: a digest over the values alone reported no drift when
    either changed under a running session, which is the one thing that comparison exists to catch.
    Coverage is build-granular: a plugin upgrade or a checkout's own commit moves the build identifier;
    an in-place edit under one build does not.
    """
    values = config.resolve()[0]
    first = config.fingerprint()
    assert first == config.fingerprint(), "the digest must be stable for one build and one config"

    plugin = tmp_path / "other-plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": "9.9.9"}), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin))
    try:
        assert config.plugin_build() == "9.9.9", "a non-checkout plugin root must identify by version"
        # No `reload()` between the two digests: resolution stays memoized, so the values below are
        # provably the same object the first digest was taken over, and the build is the only difference.
        assert config.resolve()[0] == values, "no resolved value changed, only the build"
        assert config.fingerprint() != first, "a changed build must move the digest with identical values"
    finally:
        # Restored inside the test, not left to teardown: the fixture's own teardown re-resolves, and it
        # runs before `monkeypatch` unwinds, so a plugin root with no shipped defaults would refuse there.
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN_ROOT))


def test_no_plugin_root_resolves_to_a_stable_placeholder(monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    assert config.plugin_build() == "unknown", "no CLAUDE_PLUGIN_ROOT must resolve to a stable placeholder"


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

    This resolver is on the path every tool call takes to a resolved value. Worse for the failing case,
    which is what the bound is for: one failed `get()` asks for the root twice — the credential-shape
    gate resolves first and the value's own `resolve()` asks again — so an unmemoized refusal waits on a
    wedged git for twice the number anyone reading that constant would expect.
    """
    # Spawn counts, not returned values: a value cannot tell a fresh resolution from a cached one.
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

    A timeout says nothing about the repository — only that git did not answer inside the bound, which a
    momentary index lock or a slow filesystem produces — so memoizing that verdict turns one hiccup into
    a server that refuses every later tool call for the rest of its uptime. The `reload_config` tool does
    reach `reset_cache()`, but nothing tells a client that a git hiccup is what it is stuck on, where the
    retry needs no client to know anything. A settled refusal stays memoized: this pins the distinction.
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
    the next call, so both belong on the remembered side and only the timeout on the retried side.

    Repeat-call consistency is the property, deliberately *not* object identity: `raise` extends an
    exception's own `__traceback__` in place, so re-raising one cached instance grows that chain by two
    frames per call — ~400 frames and ~53KB of rendered traceback after 200 calls in a server that stays
    up refusing — and the cheapest way to be repeat-consistent is exactly the one that leaks.
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

    # Scoped: `fixture_repo`'s teardown resolves again, and a still-broken git fails that, not this test.
    with monkeypatch.context() as mp:
        mp.setattr(config.subprocess, "run", unrunnable)
        config.reset_cache()
        with pytest.raises(config.ConfigError, match="git could not be run") as first:
            config.repo_root()
        repeats(first.value, attempts)

    # Its own counter: a spawn count says nothing about the branch that never reaches git.
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
    assert config.env_present("EXAMPLE_\udfff_TOKEN") is False


def _write_reader_layer(repo: Path, reader: object) -> None:
    layer = {**FIXTURE_LAYER, "text": reader if isinstance(reader, dict) else {"reader": reader}}
    (repo / ".shipyard" / "config.json").write_text(json.dumps(layer), encoding="utf-8")
    config.reload()


def test_text_reader_resolves_to_its_shipped_default_and_a_layer_outranks_it(fixture_repo):
    """The shipped default is read from `config/defaults.json`, never restated here, so the two cannot drift."""
    shipped = json.loads((PLUGIN_ROOT / "config" / "defaults.json").read_text(encoding="utf-8"))["text"]["reader"]
    assert isinstance(shipped, str) and shipped.strip(), "the shipped reader must be a non-empty string"

    values, provenance = config.resolve()
    assert values["text"]["reader"] == shipped
    assert provenance["text.reader"] == "shipped-default"

    _write_reader_layer(fixture_repo, "a tired reviewer on a phone")
    values, provenance = config.resolve()
    assert values["text"]["reader"] == "a tired reviewer on a phone", "a repo layer must outrank the shipped default"
    assert provenance["text.reader"] == "repo-committed"


def test_an_empty_text_reader_is_refused_rather_than_resolving_to_nothing(fixture_repo):
    """Without `minLength`, `""` would resolve as configured and silently mean no reader at all."""
    _write_reader_layer(fixture_repo, "")
    assert any("text.reader" in e and "at least 1 characters" in e for e in config.validate())


def test_a_text_reader_over_the_length_cap_is_refused(fixture_repo):
    """A declared `maxLength` nothing enforces is a lie: a 201-character value would otherwise pass unremarked."""
    _write_reader_layer(fixture_repo, "r" * 201)
    assert any("text.reader" in e and "at most 200 characters" in e for e in config.validate())


def test_a_non_string_text_reader_is_refused_rather_than_coerced(fixture_repo):
    """Type alone: an int would stringify happily downstream and land in a prompt as a bare number."""
    _write_reader_layer(fixture_repo, 15)
    assert any("text.reader" in e and "type" in e for e in config.validate())


def test_an_undeclared_key_beside_text_reader_is_refused(fixture_repo):
    """A misspelled or invented sibling would otherwise be accepted and then never read by anything."""
    _write_reader_layer(fixture_repo, {"reader": "a new joiner", "readers": "a new joiner"})
    assert any("text.readers" in e and "schema.json" in e for e in config.validate())


def _write_skills_layer(repo: Path, skills: object) -> None:
    layer = {**FIXTURE_LAYER, "skills": skills}
    (repo / ".shipyard" / "config.json").write_text(json.dumps(layer), encoding="utf-8")
    config.reload()


@pytest.mark.parametrize("key", ["standards", "reviewer"])
def test_a_skills_key_resolves_to_its_shipped_default_and_a_layer_outranks_it(fixture_repo, key):
    """Null from the shipped layer is what makes the pass inert until a repository opts in."""
    shipped = json.loads((PLUGIN_ROOT / "config" / "defaults.json").read_text(encoding="utf-8"))["skills"][key]
    assert shipped is None, "both keys must ship null, or every existing path changes on install"

    values, provenance = config.resolve()
    assert values["skills"][key] is None
    assert provenance[f"skills.{key}"] == "shipped-default"

    _write_skills_layer(fixture_repo, {key: "nearmap-reviewer"})
    values, provenance = config.resolve()
    assert values["skills"][key] == "nearmap-reviewer", "a repo layer must outrank the shipped default"
    assert provenance[f"skills.{key}"] == "repo-committed"


@pytest.mark.parametrize("key", ["standards", "reviewer"])
@pytest.mark.parametrize("value", ["dropbox:find-dropbox-content", "hunt"])
def test_both_documented_skill_name_forms_resolve(fixture_repo, key, value):
    """The pattern has to admit the `plugin:skill` form as well as a bare name; both appear in a live list."""
    _write_skills_layer(fixture_repo, {key: value})
    assert not [e for e in config.validate() if f"skills.{key}" in e]
    assert config.resolve()[0]["skills"][key] == value


@pytest.mark.parametrize("key", ["standards", "reviewer"])
def test_an_empty_skill_name_is_refused_rather_than_resolving_to_nothing(fixture_repo, key):
    """Without `minLength`, `""` would resolve as configured and mean neither "none" nor a skill."""
    _write_skills_layer(fixture_repo, {key: ""})
    assert any(f"skills.{key}" in e and "at least 1 characters" in e for e in config.validate())


@pytest.mark.parametrize("key", ["standards", "reviewer"])
def test_a_skill_name_over_the_length_cap_is_refused(fixture_repo, key):
    """A declared `maxLength` nothing enforces is a lie: a 101-character value would pass unremarked."""
    _write_skills_layer(fixture_repo, {key: "s" * 101})
    assert any(f"skills.{key}" in e and "at most 100 characters" in e for e in config.validate())


@pytest.mark.parametrize("key", ["standards", "reviewer"])
@pytest.mark.parametrize("value", ["Nearmap-Reviewer", "-hunt", "a b", "/sy:standards", "plugin:"])
def test_a_skill_name_the_pattern_rejects_is_refused(fixture_repo, key, value):
    """A leading slash, a capital, or a space names no invokable skill; unrefused it fails only at dispatch."""
    _write_skills_layer(fixture_repo, {key: value})
    assert any(f"skills.{key}" in e and "pattern" in e for e in config.validate())


@pytest.mark.parametrize("key", ["standards", "reviewer"])
def test_a_non_string_skill_name_is_refused_rather_than_coerced(fixture_repo, key):
    """Type alone: an int would stringify downstream and reach `Skill` as a bare number."""
    _write_skills_layer(fixture_repo, {key: 15})
    assert any(f"skills.{key}" in e and "type" in e for e in config.validate())


def test_an_undeclared_key_beside_the_skills_keys_is_refused(fixture_repo):
    """`additionalProperties: false` is the only thing standing between a typo and a silently unread key."""
    _write_skills_layer(fixture_repo, {"standards": "hunt", "standard": "hunt"})
    assert any("skills.standard" in e and "schema.json" in e for e in config.validate())
