"""The whiteboard board's write/read contract, exercised the way a user edits the scene file by hand.

Each "user edit" below changes the `.excalidraw` JSON exactly as an Excalidraw editor saves it: a drag
rewrites the note's position and `frameId`, a text edit rewrites the bound text's `originalText`, a
delete drops the elements.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sy_tools import board, config, server

SPEC = {
    "lanes": [{"id": "alive", "name": "Alive"}, {"id": "out", "name": "Ruled out"}],
    "notes": [
        {"id": "opt-a", "lane": "alive", "kind": "option", "text": "Option A\nplain JSON file"},
        {"id": "opt-b", "lane": "alive", "kind": "option", "text": "Option B"},
    ],
    "links": [{"from": "opt-a", "to": "opt-b"}],
}


def _scene(directory: Path) -> dict:
    return json.loads((directory / board.SCENE_NAME).read_text(encoding="utf-8"))


def _save(directory: Path, scene: dict) -> None:
    (directory / board.SCENE_NAME).write_text(json.dumps(scene), encoding="utf-8")


def _element(scene: dict, ident: str) -> dict:
    return next(e for e in scene["elements"] if e["id"] == ident)


def _user_drags(directory: Path, note: str, lane: str) -> None:
    scene = _scene(directory)
    target = _element(scene, lane)
    for ident in (note, f"{note}-text"):
        element = _element(scene, ident)
        element["x"], element["frameId"] = target["x"] + 40, lane
    _save(directory, scene)


def _user_edits_text(directory: Path, note: str, text: str) -> None:
    scene = _scene(directory)
    _element(scene, f"{note}-text").update(text=text, originalText=text)
    _save(directory, scene)


def _with(note: str, **fields: str | None) -> dict:
    return {**SPEC, "notes": [{**n, **fields} if n["id"] == note else n for n in SPEC["notes"]]}


def _note(report: dict, ident: str) -> dict:
    return next(n for n in report["notes"] if n["id"] == ident)


def test_a_first_write_makes_a_scene_whose_notes_lanes_and_link_read_back_unchanged(tmp_path):
    board.write(tmp_path, SPEC)
    scene = _scene(tmp_path)
    assert scene["type"] == "excalidraw", "the file must be an Excalidraw scene an editor can open"
    shape, text = _element(scene, "opt-a"), _element(scene, "opt-a-text")
    assert text["containerId"] == "opt-a" and {"id": "opt-a-text", "type": "text"} in shape["boundElements"], (
        "a note is a shape with its text bound to it, or the editor drags the two apart"
    )
    assert shape["frameId"] == text["frameId"] == "alive", "a note and its text must sit in the lane's frame"
    report = board.read(tmp_path)
    assert [(n["id"], n["lane"], n["owner"]) for n in report["notes"]] == [
        ("opt-a", "alive", "claude"), ("opt-b", "alive", "claude"),
    ], report["notes"]
    assert not any("user_changed" in n for n in report["notes"]), "nothing has been edited yet"
    assert report["links"] == [{"from": "opt-a", "to": "opt-b", "owner": "claude"}], report["links"]
    dx, dy = _element(scene, "link:opt-a:opt-b")["points"][1]
    assert dx == 0 and dy > 0, f"a link between stacked notes must run straight down the gap, not across them: {dx, dy}"


@pytest.mark.parametrize(
    ("edit", "field", "adopted"),
    [
        (lambda d: _user_drags(d, "opt-b", "out"), "lane", {"lane": "out"}),
        (lambda d: _user_edits_text(d, "opt-b", "Option B, cheaper"), "text", {"text": "Option B, cheaper"}),
    ],
)
def test_a_user_edit_is_reported_refuses_an_overwrite_and_clears_once_adopted(tmp_path, edit, field, adopted):
    board.write(tmp_path, SPEC)
    edit(tmp_path)
    assert _note(board.read(tmp_path), "opt-b")["user_changed"] == [field], "the edit must surface on read"
    with pytest.raises(board.BoardConflict, match="opt-b"):
        board.write(tmp_path, SPEC)
    board.write(tmp_path, _with("opt-b", **adopted))
    assert "user_changed" not in _note(board.read(tmp_path), "opt-b"), "an adopted edit is no longer a change"


def _user_drags_off_every_lane(directory: Path) -> None:
    scene = _scene(directory)
    for ident in ("opt-b", "opt-b-text"):
        _element(scene, ident).update(x=-900, frameId=None)
    _save(directory, scene)


def _user_recolours(directory: Path) -> None:
    scene = _scene(directory)
    _element(scene, "opt-b")["backgroundColor"] = "#ffffff"
    _save(directory, scene)


@pytest.mark.parametrize(
    ("edit", "spec"),
    [(_user_drags_off_every_lane, _with("opt-b", lane=None)), (_user_recolours, _with("opt-b", kind="objection"))],
)
def test_an_edit_outside_the_lanes_or_to_a_colour_never_wedges_the_board(tmp_path, edit, spec):
    board.write(tmp_path, SPEC)
    edit(tmp_path)
    board.write(tmp_path, spec)
    assert "user_changed" not in _note(board.read(tmp_path), "opt-b"), "the edit must be adoptable, not a dead end"


def test_a_stale_editor_save_is_kept_aside_and_the_last_write_restored(tmp_path):
    board.write(tmp_path, SPEC)
    opened_before = (tmp_path / board.SCENE_NAME).read_text(encoding="utf-8")
    added = {"id": "opt-c", "lane": "out", "kind": "option", "text": "C"}
    board.write(tmp_path, {**SPEC, "notes": [*SPEC["notes"], added]})
    (tmp_path / board.SCENE_NAME).write_text(opened_before, encoding="utf-8")
    with pytest.raises(board.BoardConflict, match="board_read"):
        board.write(tmp_path, SPEC)
    report = board.read(tmp_path)
    assert report["stale_save"] and Path(report["stale_save"]["kept_as"]).read_text(encoding="utf-8") == opened_before
    assert report["deleted_notes"] == [] and _note(report, "opt-c")["owner"] == "claude", (
        "a stale save must not read as the user deleting what the last write added"
    )


def test_claude_moves_its_own_unedited_note_into_another_lane(tmp_path):
    board.write(tmp_path, SPEC)
    result = board.write(tmp_path, _with("opt-b", lane="out"))
    assert "note opt-b moved to out" in result["changed"], result["changed"]
    scene = _scene(tmp_path)
    lane, note = _element(scene, "out"), _element(scene, "opt-b")
    assert note["frameId"] == "out" and lane["x"] <= note["x"] < lane["x"] + lane["width"], (
        "a moved note must sit inside the lane it now belongs to"
    )


@pytest.mark.parametrize("removed", [{"opt-b", "opt-b-text"}, {"opt-b-text"}], ids=["whole note", "only its text"])
def test_a_deleted_note_is_reported_refuses_a_rewrite_and_is_forgotten_once_dropped(tmp_path, removed):
    board.write(tmp_path, SPEC)
    scene = _scene(tmp_path)
    scene["elements"] = [e for e in scene["elements"] if e["id"] not in removed]
    _save(tmp_path, scene)
    assert board.read(tmp_path)["deleted_notes"] == ["opt-b"]
    with pytest.raises(board.BoardConflict, match="deleted"):
        board.write(tmp_path, SPEC)
    board.write(tmp_path, {**SPEC, "notes": SPEC["notes"][:1], "links": []})
    assert board.read(tmp_path)["deleted_notes"] == [], "an answered deletion must not be reported again"


def test_a_note_the_user_adds_is_reported_as_theirs_and_left_alone(tmp_path):
    board.write(tmp_path, SPEC)
    scene = _scene(tmp_path)
    shape = {**_element(scene, "opt-a"), "id": "hand1", "y": 400, "boundElements": [{"id": "hand1t", "type": "text"}]}
    text = {**_element(scene, "opt-a-text"), "id": "hand1t", "containerId": "hand1", "originalText": "What about X?"}
    scene["elements"] += [shape, text]
    _save(tmp_path, scene)
    assert _note(board.read(tmp_path), "hand1") == {
        "id": "hand1", "lane": "alive", "text": "What about X?", "owner": "user",
    }
    board.write(tmp_path, SPEC)
    assert _element(_scene(tmp_path), "hand1t")["originalText"] == "What about X?", "a write never touches a user note"


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (_with("opt-a", kind="idea"), "kind"),
        (_with("opt-a", lane="nowhere"), "not a frame"),
        ({**SPEC, "links": [{"from": "opt-a", "to": "alive"}]}, "not a note"),
        ({**SPEC, "notes": SPEC["notes"] * 2}, "unique"),
    ],
)
def test_a_malformed_spec_is_refused_before_anything_is_written(tmp_path, spec, message):
    with pytest.raises(ValueError, match=message):
        board.write(tmp_path, spec)
    assert not (tmp_path / board.SCENE_NAME).exists(), "a refused spec must leave no board behind"


def test_the_tools_resolve_the_board_from_its_identifier_and_surface_a_conflict(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "repo_scratch_dir", lambda: tmp_path)
    server.board_write("pricing-idea", SPEC)
    _user_drags(tmp_path / "whiteboards" / "pricing-idea", "opt-a", "out")
    assert _note(server.board_read("pricing-idea"), "opt-a")["user_changed"] == ["lane"]
    with pytest.raises(server.ToolError, match="opt-a"):
        server.board_write("pricing-idea", SPEC)
    with pytest.raises(server.ToolError, match="board id"):
        server.board_read("../elsewhere")
