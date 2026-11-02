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


def _user_drags(directory: Path, note: str, lane: str, scene: dict | None = None) -> None:
    scene = scene or _scene(directory)
    target = _element(scene, lane)
    for ident in (note, f"{note}-text"):
        element = _element(scene, ident)
        element["x"], element["frameId"] = target["x"] + 40, lane
    _save(directory, scene)


def _user_edits_text(directory: Path, note: str, text: str, scene: dict | None = None) -> None:
    scene = scene or _scene(directory)
    _element(scene, f"{note}-text").update(text=text, originalText=text)
    _save(directory, scene)


def _user_nudges(scene: dict, note: str, dx: float, dy: float) -> None:
    for ident in (note, f"{note}-text"):
        element = _element(scene, ident)
        element["x"] += dx
        element["y"] += dy


def _user_adds_note(scene: dict, ident: str, text: str, y: float) -> None:
    label_id = f"{ident}-text"
    shape = {**_element(scene, "opt-a"), "id": ident, "y": y, "boundElements": [{"id": label_id, "type": "text"}]}
    label = {**_element(scene, "opt-a-text"), "id": label_id, "containerId": ident, "y": y, "originalText": text}
    scene["elements"] += [shape, label]


def _changes(**kinds: list) -> dict:
    empty = {"notes": {"added": [], "deleted": [], "moved": [], "edited": []},
             "lanes": {"added": [], "deleted": [], "renamed": []}, "links": {"added": [], "deleted": []}}
    for key, value in kinds.items():
        group, kind = key.split("_")
        empty[group][kind] = value
    return empty


def _inside(element: dict, lane: dict) -> bool:
    return (lane["x"] <= element["x"] and element["x"] + element["width"] <= lane["x"] + lane["width"]
            and lane["y"] <= element["y"] and element["y"] + element["height"] <= lane["y"] + lane["height"])


def _content(report: dict) -> tuple:
    return (
        [(lane["id"], lane["name"]) for lane in report["lanes"]],
        sorted((n["id"], n["lane"], n["text"]) for n in report["notes"]),
        report["links"],
    )


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
    opened_before = _scene(tmp_path)
    added = {"id": "opt-c", "lane": "out", "kind": "option", "text": "C"}
    board.write(tmp_path, {**SPEC, "notes": [*SPEC["notes"], added]})
    _user_drags(tmp_path, "opt-b", "out", opened_before)
    saved = (tmp_path / board.SCENE_NAME).read_text(encoding="utf-8")
    with pytest.raises(board.BoardConflict, match="board_read"):
        board.write(tmp_path, SPEC)
    report = board.read(tmp_path)
    assert report["stale_save"] and Path(report["stale_save"]["kept_as"]).read_text(encoding="utf-8") == saved
    assert report["deleted_notes"] == [] and _note(report, "opt-c")["owner"] == "claude", (
        "a stale save must not read as the user deleting what the last write added"
    )
    assert report["stale_save"]["changes"] == _changes(
        notes_moved=[{"id": "opt-b", "lane": "out", "text": "Option B"}]
    ), "the stale copy's one user move must be reported, and nothing the later write added"


def test_a_reversed_decision_is_not_reported_again_by_a_later_save_from_the_same_tab(tmp_path):
    board.write(tmp_path, SPEC)
    tab = _scene(tmp_path)
    _user_drags(tmp_path, "opt-b", "out", tab)
    board.read(tmp_path)
    board.write(tmp_path, _with("opt-b", lane="out"))
    board.write(tmp_path, SPEC)
    _user_edits_text(tmp_path, "opt-a", "Option A, cached", tab)
    changes = board.read(tmp_path)["stale_save"]["changes"]
    assert changes == _changes(notes_edited=[{"id": "opt-a", "lane": "alive", "text": "Option A, cached"}]), (
        "the tab's earlier move was answered and reversed; only its new edit is the user's"
    )


def test_a_stale_save_reports_a_deleted_note_and_an_added_one(tmp_path):
    board.write(tmp_path, SPEC)
    tab = _scene(tmp_path)
    added = {"id": "opt-c", "lane": "out", "kind": "option", "text": "C"}
    board.write(tmp_path, {**SPEC, "notes": [*SPEC["notes"], added]})
    tab["elements"] = [e for e in tab["elements"] if e["id"] not in {"opt-b", "opt-b-text"}]
    _user_adds_note(tab, "hand1", "What about X?", 400)
    _save(tmp_path, tab)
    notes = board.read(tmp_path)["stale_save"]["changes"]["notes"]
    assert notes["deleted"] == [{"id": "opt-b", "lane": "alive", "text": "Option B"}], notes
    assert notes["added"] == [{"id": "hand1", "lane": "alive", "text": "What about X?"}], notes


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


def test_a_user_lane_rename_is_reported_refuses_an_overwrite_and_clears_once_adopted(tmp_path):
    board.write(tmp_path, SPEC)
    scene = _scene(tmp_path)
    _element(scene, "out")["name"] = "Dead ends"
    _save(tmp_path, scene)
    lane = next(lane for lane in board.read(tmp_path)["lanes"] if lane["id"] == "out")
    assert lane["user_changed"] == ["name"], lane
    with pytest.raises(board.BoardConflict, match="out"):
        board.write(tmp_path, SPEC)
    board.write(tmp_path, {**SPEC, "lanes": [SPEC["lanes"][0], {"id": "out", "name": "Dead ends"}]})
    assert not any("user_changed" in lane for lane in board.read(tmp_path)["lanes"]), "an adopted rename is no change"


def test_tidy_restacks_each_lane_in_spec_order_and_keeps_framed_free_text_inside_its_lane(tmp_path):
    board.write(tmp_path, SPEC)
    scene = _scene(tmp_path)
    _user_nudges(scene, "opt-a", 25, 300)
    _user_nudges(scene, "opt-b", -10, -80)
    lowest = max(e["y"] + e["height"] for e in scene["elements"] if e.get("frameId") == "alive")
    free = {**_element(scene, "opt-a-text"), "id": "free1", "containerId": None, "originalText": "aside"}
    scene["elements"].append({**free, "y": lowest + 40})
    _save(tmp_path, scene)
    before = _content(board.read(tmp_path))
    board.write(tmp_path, SPEC, tidy=True)
    scene = _scene(tmp_path)
    lane, a, b = _element(scene, "alive"), _element(scene, "opt-a"), _element(scene, "opt-b")
    assert a["x"] == b["x"] == lane["x"] + board.NOTE_INSET, "tidied notes line up at the lane's inset"
    assert a["y"] == lane["y"] + board.FIRST_NOTE_OFFSET and a["y"] + a["height"] < b["y"], "spec order, top down"
    assert _inside(_element(scene, "free1"), lane), "framed free text must stay inside its lane after a tidy"
    assert _content(board.read(tmp_path)) == before, "a tidy changes geometry only"


def test_tidy_puts_the_lanes_in_spec_order_after_a_switch_to_the_working_layout(tmp_path):
    frame = {"lanes": [{"id": "ideas", "name": "Ideas"}, {"id": "out", "name": "Ruled out"}],
             "notes": [{"id": "opt-a", "lane": "out", "kind": "option", "text": "A"}], "links": []}
    board.write(tmp_path, frame)
    working = {**frame, "lanes": [frame["lanes"][0], {"id": "alive", "name": "Alive"}, frame["lanes"][1]]}
    board.write(tmp_path, working)
    assert _element(_scene(tmp_path), "out")["x"] < _element(_scene(tmp_path), "alive")["x"], "a write only appends"
    board.write(tmp_path, working, tidy=True)
    scene = _scene(tmp_path)
    ideas, alive, out = (_element(scene, i) for i in ("ideas", "alive", "out"))
    assert ideas["x"] < alive["x"] < out["x"], "tidy follows the spec's lane order"
    assert _inside(_element(scene, "opt-a"), out), "a lane's notes move with it"


def test_tidy_stacks_a_user_note_after_the_spec_notes_and_read_lists_each_lane_top_to_bottom(tmp_path):
    board.write(tmp_path, SPEC)
    scene = _scene(tmp_path)
    _user_adds_note(scene, "hand1", "What about X?", _element(scene, "opt-a")["y"] + 1)
    _user_nudges(scene, "hand1", -10, 0)
    _save(tmp_path, scene)
    assert [n["id"] for n in board.read(tmp_path)["notes"]] == ["opt-a", "hand1", "opt-b"], "read follows the board"
    board.write(tmp_path, SPEC, tidy=True)
    report = board.read(tmp_path)
    assert [n["id"] for n in report["notes"]] == ["opt-a", "opt-b", "hand1"], "a user note goes below the spec's"
    assert _note(report, "hand1") == {"id": "hand1", "lane": "alive", "text": "What about X?", "owner": "user"}


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
