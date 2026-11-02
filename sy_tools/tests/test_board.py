"""The design board's write/read contract, exercised the way a user edits the scene file by hand.

Each "user edit" below changes the `.excalidraw` JSON exactly as an Excalidraw editor saves it: a drag
rewrites the note's and its text's position (and `frameId` when it crosses into another section), a text
edit rewrites the bound text's `originalText`, a delete drops the elements.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sy_tools import board, config, server

SPEC = {
    "sections": [{"id": "api", "name": "System design"}, {"id": "model", "name": "Data model"}],
    "notes": [
        {"id": "client", "section": "api", "kind": "external", "text": "Client", "at": [0, 0]},
        {"id": "server", "section": "api", "kind": "component", "text": "API server", "at": [1, 0]},
        {"id": "users", "section": "model", "kind": "data", "text": "User\nid: int\nname: str", "at": [0, 0]},
    ],
    "links": [{"from": "client", "to": "server", "label": "calls"}, {"from": "server", "to": "users"}],
}


def _scene(directory: Path) -> dict:
    return json.loads((directory / board.SCENE_NAME).read_text(encoding="utf-8"))


def _save(directory: Path, scene: dict) -> None:
    (directory / board.SCENE_NAME).write_text(json.dumps(scene), encoding="utf-8")


def _element(scene: dict, ident: str) -> dict:
    return next(e for e in scene["elements"] if e["id"] == ident)


def _cell_xy(directory: Path, scene: dict, section: str, col: int, row: int) -> tuple[float, float]:
    rows = json.loads((directory / board.STATE_NAME).read_text(encoding="utf-8"))["grid"][section]["rows"]
    frame = _element(scene, section)
    return frame["x"] + board.SECTION_INSET + col * board.CELL_WIDTH, frame["y"] + rows[row]


def _user_moves(scene: dict, note: str, x: float, y: float, section: str | None = "keep") -> None:
    shape = _element(scene, note)
    dx, dy = x - shape["x"], y - shape["y"]
    for ident in (note, f"{note}:text"):
        element = _element(scene, ident)
        element["x"] += dx
        element["y"] += dy
        if section != "keep":
            element["frameId"] = section


def _user_drags(directory: Path, note: str, section: str, cell: tuple[int, int], scene: dict | None = None) -> None:
    """Drops the note roughly on a cell, a few pixels off its corner, the way a hand drag lands."""
    scene = scene or _scene(directory)
    x, y = _cell_xy(directory, scene, section, *cell)
    _user_moves(scene, note, x + 7, y + 9, section)
    _save(directory, scene)


def _user_edits_text(directory: Path, ident: str, text: str, scene: dict | None = None) -> None:
    scene = scene or _scene(directory)
    _element(scene, ident).update(text=text, originalText=text)
    _save(directory, scene)


def _user_adds_note(scene: dict, ident: str, text: str, x: float, y: float, section: str | None) -> None:
    label_id = f"{ident}:text"
    shape = {**_element(scene, "server"), "id": ident, "x": x, "y": y, "frameId": section,
             "boundElements": [{"id": label_id, "type": "text"}]}
    label = {**_element(scene, "server:text"), "id": label_id, "containerId": ident, "x": x + 20, "y": y + 20,
             "frameId": section, "text": text, "originalText": text}
    scene["elements"] += [shape, label]


def _free_text(scene: dict, ident: str, x: float, y: float, section: str | None) -> dict:
    free = {**_element(scene, "client:text"), "id": ident, "containerId": None, "x": x, "y": y, "frameId": section,
            "text": "aside", "originalText": "aside"}
    scene["elements"].append(free)
    return free


def _changes(**kinds: list) -> dict:
    empty = {"notes": {"added": [], "deleted": [], "moved": [], "edited": []},
             "sections": {"added": [], "deleted": [], "renamed": []},
             "links": {"added": [], "deleted": [], "relabelled": []}}
    for key, value in kinds.items():
        group, kind = key.split("_")
        empty[group][kind] = value
    return empty


def _inside(element: dict, frame: dict) -> bool:
    return (frame["x"] <= element["x"] and element["x"] + element["width"] <= frame["x"] + frame["width"]
            and frame["y"] <= element["y"] and element["y"] + element["height"] <= frame["y"] + frame["height"])


def _overlap(a: dict, b: dict) -> bool:
    return (a["x"] < b["x"] + b["width"] and b["x"] < a["x"] + a["width"]
            and a["y"] < b["y"] + b["height"] and b["y"] < a["y"] + a["height"])


def _with(note: str, **fields: object) -> dict:
    return {**SPEC, "notes": [{**n, **fields} if n["id"] == note else n for n in SPEC["notes"]]}


def _note(report: dict, ident: str) -> dict:
    return next(n for n in report["notes"] if n["id"] == ident)


def _cells(report: dict) -> list[tuple]:
    return [(n["id"], n["section"], n["at"]) for n in report["notes"]]


def test_a_first_write_lays_notes_in_their_cells_and_reads_back_unchanged(tmp_path):
    board.write(tmp_path, SPEC)
    scene = _scene(tmp_path)
    assert scene["type"] == "excalidraw", "the file must be an Excalidraw scene an editor can open"
    api, model = _element(scene, "api"), _element(scene, "model")
    assert api["x"] == model["x"] == 0 and api["y"] + api["height"] < model["y"], "sections stack top to bottom"
    for ident, frame in (("client", api), ("server", api), ("users", model)):
        shape, text = _element(scene, ident), _element(scene, f"{ident}:text")
        assert text["containerId"] == ident and shape["frameId"] == text["frameId"] == frame["id"]
        assert _inside(shape, frame), f"{ident} must sit inside its section's frame"
    step = _element(scene, "server")["x"] - _element(scene, "client")["x"]
    assert step == board.CELL_WIDTH, "column 1 is one cell right of column 0"
    assert _element(scene, "users:text")["textAlign"] == "left", "data notes read as a schema, top-left aligned"
    label = _element(scene, "link:client:server:label")
    assert label["containerId"] == "link:client:server" and label["fontSize"] and label["width"], (
        "a label is bound text on its arrow with its font and size set, or Excalidraw drops it"
    )
    assert _element(scene, "link:server:users")["frameId"] is None, "a cross-section arrow is never clipped"
    report = board.read(tmp_path)
    assert _cells(report) == [("client", "api", [0, 0]), ("server", "api", [1, 0]), ("users", "model", [0, 0])]
    assert _note(report, "users")["kind"] == "data", "a Claude note reads back with the kind it was written as"
    assert not any("user_changed" in n for n in report["notes"]), "nothing has been edited yet"
    assert [(link["from"], link["to"], link["label"], link["owner"]) for link in report["links"]] == [
        ("client", "server", "calls", "claude"), ("server", "users", "", "claude"),
    ]


@pytest.mark.parametrize(
    ("edit", "field", "adopted"),
    [
        (lambda d: _user_drags(d, "server", "api", (1, 1)), "at", {"at": [1, 1]}),
        (lambda d: _user_drags(d, "server", "model", (1, 0)), "section", {"section": "model", "at": [1, 0]}),
        (lambda d: _user_edits_text(d, "server:text", "API gateway"), "text", {"text": "API gateway"}),
    ],
    ids=["cell", "section", "text"],
)
def test_a_user_edit_is_reported_refuses_an_overwrite_and_clears_once_adopted(tmp_path, edit, field, adopted):
    board.write(tmp_path, SPEC)
    edit(tmp_path)
    note = _note(board.read(tmp_path), "server")
    assert field in note["user_changed"] and all(note[k] == v for k, v in adopted.items()), note
    with pytest.raises(board.BoardConflict, match="server"):
        board.write(tmp_path, SPEC)
    board.write(tmp_path, _with("server", **adopted))
    assert "user_changed" not in _note(board.read(tmp_path), "server"), "an adopted edit is no longer a change"


def test_a_nudge_inside_a_cell_is_no_edit_and_the_next_write_puts_the_note_back(tmp_path):
    board.write(tmp_path, SPEC)
    before = _element(_scene(tmp_path), "server")
    scene = _scene(tmp_path)
    _user_moves(scene, "server", before["x"] + 10, before["y"] - 10)
    _save(tmp_path, scene)
    assert "user_changed" not in _note(board.read(tmp_path), "server"), "a nudge is not a move"
    board.write(tmp_path, SPEC)
    after = _element(_scene(tmp_path), "server")
    assert (after["x"], after["y"]) == (before["x"], before["y"]), "a nudged note snaps back onto its cell"


def test_a_user_label_edit_is_reported_refuses_an_overwrite_and_clears_once_adopted(tmp_path):
    board.write(tmp_path, SPEC)
    _user_edits_text(tmp_path, "link:client:server:label", "polls")
    link = board.read(tmp_path)["links"][0]
    assert (link["label"], link["user_changed"]) == ("polls", ["label"]), link
    with pytest.raises(board.BoardConflict, match="client->server"):
        board.write(tmp_path, SPEC)
    adopted = {**SPEC, "links": [{"from": "client", "to": "server", "label": "polls"}, SPEC["links"][1]]}
    board.write(tmp_path, adopted)
    assert "user_changed" not in board.read(tmp_path)["links"][0], "an adopted label is no longer a change"


def test_a_taller_note_pushes_later_rows_and_sections_down_with_nothing_overlapping(tmp_path):
    queue = {"id": "queue", "section": "api", "kind": "store", "text": "Queue", "at": [0, 1]}
    spec = {**SPEC, "notes": [*SPEC["notes"], queue]}
    board.write(tmp_path, spec)
    before = {i: _element(_scene(tmp_path), i)["y"] for i in ("queue", "model")}
    tall = "Client\n" + "\n".join(f"line {i}" for i in range(12))
    board.write(tmp_path, {**spec, "notes": [{**n, "text": tall} if n["id"] == "client" else n for n in spec["notes"]]})
    scene = _scene(tmp_path)
    assert all(_element(scene, i)["y"] > before[i] for i in before), "row 1 and the next section move down"
    shapes = [_element(scene, i) for i in ("client", "server", "queue", "users")]
    assert not any(_overlap(a, b) for i, a in enumerate(shapes) for b in shapes[i + 1:]), "no two notes overlap"
    for shape in shapes:
        assert _inside(shape, _element(scene, shape["frameId"])), f"{shape['id']} must stay inside its section"
    assert not _overlap(_element(scene, "api"), _element(scene, "model")), "sections never overlap"


def test_a_stale_save_reports_a_cell_move_and_a_relabelled_link(tmp_path):
    board.write(tmp_path, SPEC)
    tab = _scene(tmp_path)
    added = {"id": "cache", "section": "api", "kind": "store", "text": "Cache", "at": [2, 0]}
    board.write(tmp_path, {**SPEC, "notes": [*SPEC["notes"], added]})
    x, y = _element(tab, "client")["x"], _element(tab, "client")["y"]
    _user_moves(tab, "client", x + 7, y + board.NOMINAL_ROW_HEIGHT + board.ROW_GAP + 9)
    _element(tab, "link:client:server:label").update(text="polls", originalText="polls")
    _save(tmp_path, tab)
    with pytest.raises(board.BoardConflict, match="board_read"):
        board.write(tmp_path, SPEC)
    report = board.read(tmp_path)
    assert report["deleted_notes"] == [] and _note(report, "cache")["owner"] == "claude", (
        "a stale save must not read as the user deleting what the last write added"
    )
    assert report["stale_save"]["changes"] == _changes(
        notes_moved=[{"id": "client", "section": "api", "at": [0, 1], "text": "Client"}],
        links_relabelled=[["client", "server", "polls"]],
    ), "only the stale copy's own edits are reported"


def test_a_drag_onto_an_occupied_cell_moves_only_the_dragged_note(tmp_path):
    board.write(tmp_path, SPEC)
    _user_drags(tmp_path, "client", "api", (1, 0))
    report = board.read(tmp_path)
    assert _note(report, "server")["at"] == [1, 0] and "user_changed" not in _note(report, "server"), (
        "the occupant keeps its cell"
    )
    assert _note(report, "client")["at"] not in ([1, 0], [0, 0]), "the dragged note takes the nearest free cell"
    board.write(tmp_path, _with("client", at=_note(report, "client")["at"]))


def test_a_spec_cell_held_by_a_note_the_spec_does_not_name_is_refused_naming_both(tmp_path):
    board.write(tmp_path, SPEC)
    scene = _scene(tmp_path)
    _user_adds_note(scene, "hand1", "What about X?", *_cell_xy(tmp_path, scene, "api", 0, 1), "api")
    _save(tmp_path, scene)
    assert _note(board.read(tmp_path), "hand1")["at"] == [0, 1]
    with pytest.raises(ValueError, match=r"server.*hand1"):
        board.write(tmp_path, _with("server", at=[0, 1]))


def test_a_drag_into_the_spare_column_reads_back_and_the_next_write_adds_a_new_spare(tmp_path):
    board.write(tmp_path, SPEC)
    width = _element(_scene(tmp_path), "api")["width"]
    _user_drags(tmp_path, "server", "api", (2, 0))
    assert _note(board.read(tmp_path), "server")["at"] == [2, 0]
    board.write(tmp_path, _with("server", at=[2, 0]))
    assert _element(_scene(tmp_path), "api")["width"] == width + board.CELL_WIDTH, "a spare column follows column 2"


def test_the_scratch_area_is_never_moved_and_reads_as_the_users(tmp_path):
    board.write(tmp_path, SPEC)
    scene = _scene(tmp_path)
    _user_adds_note(scene, "idea", "What about X?", 2000, 40, None)
    free = _free_text(scene, "scribble", 2000, 400, None)
    _save(tmp_path, scene)
    kept = {i: (_element(scene, i)["x"], _element(scene, i)["y"]) for i in ("idea", "idea:text", "scribble")}
    far = {"id": "w", "section": "api", "kind": "step", "text": "W", "at": [2, 3]}
    board.write(tmp_path, {**SPEC, "notes": [*SPEC["notes"], far]})
    scene = _scene(tmp_path)
    assert {i: (_element(scene, i)["x"], _element(scene, i)["y"]) for i in kept} == kept, "a write never moves scratch"
    report = board.read(tmp_path)
    assert _note(report, "idea") == {
        "id": "idea", "section": None, "at": None, "text": "What about X?", "kind": None, "owner": "user",
    }
    assert _note(report, "scribble")["section"] is None and free["id"] == "scribble"
    assert _note(report, "scribble")["kind"] == "text", "free text reads back as text, not as a note to adopt"
    scribble = {"id": "scribble", "section": None, "kind": "note", "text": "aside", "at": None}
    with pytest.raises(ValueError, match="free text"):
        board.write(tmp_path, {**SPEC, "notes": [*SPEC["notes"], scribble]})


def test_an_arrow_between_two_scratch_notes_keeps_its_geometry(tmp_path):
    board.write(tmp_path, SPEC)
    scene = _scene(tmp_path)
    _user_adds_note(scene, "a", "A", 2000, 40, None)
    _user_adds_note(scene, "b", "B", 2400, 300, None)
    bound = {"focus": 0, "gap": 1}
    arrow = {**next(e for e in scene["elements"] if e["type"] == "arrow"), "id": "scratch-arrow", "x": 2100.0,
             "y": 90.0, "width": 123.0, "height": 77.0, "points": [[0, 0], [123.0, 77.0]], "boundElements": [],
             "startBinding": {**bound, "elementId": "a"}, "endBinding": {**bound, "elementId": "b"}}
    scene["elements"].append(arrow)
    _save(tmp_path, scene)
    board.write(tmp_path, SPEC)
    kept = _element(_scene(tmp_path), "scratch-arrow")
    assert (kept["x"], kept["y"], kept["width"], kept["height"], kept["points"]) == (
        2100.0, 90.0, 123.0, 77.0, [[0, 0], [123.0, 77.0]]
    ), "an arrow between two scratch notes is the user's and is never redrawn"


def test_framed_free_text_moves_with_its_section_and_stays_inside_it(tmp_path):
    board.write(tmp_path, SPEC)
    scene = _scene(tmp_path)
    model = _element(scene, "model")
    _free_text(scene, "aside", model["x"] + 40, model["y"] + model["height"] - 40, "model")
    _save(tmp_path, scene)
    lower = {"id": "w", "section": "api", "kind": "step", "text": "W", "at": [0, 2]}
    board.write(tmp_path, {**SPEC, "notes": [*SPEC["notes"], lower]})
    scene = _scene(tmp_path)
    model, aside = _element(scene, "model"), _element(scene, "aside")
    assert aside["y"] > model["y"] - 1 and _inside(aside, model), "framed free text moves with its section"


@pytest.mark.parametrize("removed", [{"server", "server:text"}, {"server:text"}], ids=["whole note", "only its text"])
def test_a_deleted_note_is_reported_refuses_a_rewrite_and_is_forgotten_once_dropped(tmp_path, removed):
    board.write(tmp_path, SPEC)
    scene = _scene(tmp_path)
    scene["elements"] = [e for e in scene["elements"] if e["id"] not in removed]
    _save(tmp_path, scene)
    assert board.read(tmp_path)["deleted_notes"] == ["server"]
    with pytest.raises(board.BoardConflict, match="deleted"):
        board.write(tmp_path, SPEC)
    board.write(tmp_path, {**SPEC, "notes": [SPEC["notes"][0], SPEC["notes"][2]], "links": []})
    if removed == {"server:text"}:
        with pytest.raises(ValueError, match="new id"):
            board.write(tmp_path, SPEC)
        return
    assert board.read(tmp_path)["deleted_notes"] == [], "an answered deletion must not be reported again"
    assert board.read(tmp_path)["deleted_links"] == [], "nor its links, though a textless shape still binds them"


def _user_drags_out_of_every_section(directory: Path) -> None:
    scene = _scene(directory)
    _user_moves(scene, "server", 2000, 0, None)
    _save(directory, scene)


def _user_recolours(directory: Path) -> None:
    scene = _scene(directory)
    _element(scene, "server")["backgroundColor"] = "#ffffff"
    _save(directory, scene)


@pytest.mark.parametrize(
    ("edit", "spec"),
    [
        (_user_drags_out_of_every_section, _with("server", section=None, at=None)),
        (_user_recolours, _with("server", kind="store")),
    ],
)
def test_an_edit_out_of_the_sections_or_to_a_colour_never_wedges_the_board(tmp_path, edit, spec):
    board.write(tmp_path, SPEC)
    edit(tmp_path)
    board.write(tmp_path, spec)
    assert "user_changed" not in _note(board.read(tmp_path), "server"), "the edit must be adoptable, not a dead end"


def test_a_note_adopted_out_of_every_section_can_be_written_back_into_one(tmp_path):
    board.write(tmp_path, SPEC)
    _user_drags_out_of_every_section(tmp_path)
    board.write(tmp_path, _with("server", section=None, at=None))
    board.write(tmp_path, _with("server", section="model", at=[1, 0]))
    assert _note(board.read(tmp_path), "server")["section"] == "model", "Claude's own adopted write can be moved on"


def test_a_reversed_decision_is_not_reported_again_by_a_later_save_from_the_same_tab(tmp_path):
    board.write(tmp_path, SPEC)
    tab = _scene(tmp_path)
    _user_drags(tmp_path, "server", "api", (1, 1), tab)
    board.read(tmp_path)
    board.write(tmp_path, _with("server", at=[1, 1]))
    board.write(tmp_path, SPEC)
    _user_edits_text(tmp_path, "client:text", "Browser", tab)
    changes = board.read(tmp_path)["stale_save"]["changes"]
    assert changes == _changes(notes_edited=[{"id": "client", "section": "api", "at": [0, 0], "text": "Browser"}]), (
        "the tab's earlier move was answered and reversed; only its new edit is the user's"
    )


def test_a_stale_save_reports_a_deleted_note_and_an_added_one(tmp_path):
    board.write(tmp_path, SPEC)
    tab = _scene(tmp_path)
    added = {"id": "cache", "section": "api", "kind": "store", "text": "Cache", "at": [2, 0]}
    board.write(tmp_path, {**SPEC, "notes": [*SPEC["notes"], added]})
    tab["elements"] = [e for e in tab["elements"] if e["id"] not in {"users", "users:text", "link:server:users"}]
    _user_adds_note(tab, "hand1", "What about X?", 2000, 0, None)
    _save(tmp_path, tab)
    changes = board.read(tmp_path)["stale_save"]["changes"]
    assert changes["notes"]["deleted"] == [
        {"id": "users", "section": "model", "at": [0, 0], "text": "User\nid: int\nname: str"},
    ]
    assert changes["notes"]["added"] == [{"id": "hand1", "section": None, "at": None, "text": "What about X?"}]
    assert changes["links"]["deleted"] == [["server", "users", ""]]


def test_claude_moves_its_own_unedited_note_to_another_section_and_cell(tmp_path):
    board.write(tmp_path, SPEC)
    result = board.write(tmp_path, _with("server", section="model", at=[0, 1]))
    assert {"note server moved to section model", "note server moved to cell [0, 1]"} <= set(result["changed"])
    assert _cells(board.read(tmp_path))[-1] == ("server", "model", [0, 1])


def test_a_user_section_rename_is_reported_refuses_an_overwrite_and_clears_once_adopted(tmp_path):
    board.write(tmp_path, SPEC)
    scene = _scene(tmp_path)
    _element(scene, "model")["name"] = "Schema"
    _save(tmp_path, scene)
    section = next(s for s in board.read(tmp_path)["sections"] if s["id"] == "model")
    assert section["user_changed"] == ["name"], section
    with pytest.raises(board.BoardConflict, match="model"):
        board.write(tmp_path, SPEC)
    board.write(tmp_path, {**SPEC, "sections": [SPEC["sections"][0], {"id": "model", "name": "Schema"}]})
    assert not any("user_changed" in s for s in board.read(tmp_path)["sections"]), "an adopted rename is no change"


def test_a_note_the_user_adds_in_a_section_is_theirs_and_laid_out_like_any_other(tmp_path):
    board.write(tmp_path, SPEC)
    scene = _scene(tmp_path)
    x, y = _cell_xy(tmp_path, scene, "api", 0, 1)
    _user_adds_note(scene, "hand1", "What about X?", x + 12, y + 5, "api")
    _save(tmp_path, scene)
    assert _note(board.read(tmp_path), "hand1") == {
        "id": "hand1", "section": "api", "at": [0, 1], "text": "What about X?", "kind": None, "owner": "user",
    }
    board.write(tmp_path, SPEC)
    scene = _scene(tmp_path)
    assert (_element(scene, "hand1")["x"], _element(scene, "hand1")["y"]) == _cell_xy(tmp_path, scene, "api", 0, 1)
    assert _element(scene, "hand1:text")["originalText"] == "What about X?", "a write never edits a user note"


def test_a_user_note_adopted_as_data_takes_the_data_alignment(tmp_path):
    board.write(tmp_path, SPEC)
    scene = _scene(tmp_path)
    _user_adds_note(scene, "hand1", "What about X?", *_cell_xy(tmp_path, scene, "api", 0, 1), "api")
    _save(tmp_path, scene)
    hand1 = {"id": "hand1", "section": "api", "kind": "data", "text": "What about X?", "at": [0, 1]}
    board.write(tmp_path, {**SPEC, "notes": [*SPEC["notes"], hand1]})
    text = _element(_scene(tmp_path), "hand1:text")
    assert (text["textAlign"], text["verticalAlign"]) == ("left", "top"), text


def test_a_re_pointed_arrow_and_its_link_added_again_get_distinct_ids(tmp_path):
    board.write(tmp_path, SPEC)
    scene = _scene(tmp_path)
    _element(scene, "link:client:server")["endBinding"]["elementId"] = "users"
    _save(tmp_path, scene)
    assert board.read(tmp_path)["deleted_links"] == ["client->server"]
    adopted = {**SPEC, "links": [{"from": "client", "to": "users", "label": "calls"}, SPEC["links"][1]]}
    board.write(tmp_path, adopted)
    board.write(tmp_path, {**adopted, "links": [*adopted["links"], SPEC["links"][0]]})
    ids = [e["id"] for e in _scene(tmp_path)["elements"]]
    assert len(ids) == len(set(ids)), "every element id is unique"
    report = board.read(tmp_path)
    assert [(link["from"], link["to"]) for link in report["links"]] == [
        ("client", "server"), ("client", "users"), ("server", "users"),
    ]
    assert report["deleted_links"] == []


def test_a_frame_resized_from_its_top_left_keeps_every_cell(tmp_path):
    board.write(tmp_path, SPEC)
    scene = _scene(tmp_path)
    api = _element(scene, "api")
    api.update(x=api["x"] - 300, y=api["y"] - 200, width=api["width"] + 300, height=api["height"] + 200)
    _save(tmp_path, scene)
    assert not any("user_changed" in n for n in board.read(tmp_path)["notes"]), "a resize moves no note"
    board.write(tmp_path, SPEC)


def test_a_nudged_note_keeps_its_cell_against_a_note_dropped_beside_it(tmp_path):
    board.write(tmp_path, SPEC)
    scene = _scene(tmp_path)
    x, y = _cell_xy(tmp_path, scene, "api", 1, 0)
    _user_moves(scene, "server", x + 3, y + 3)
    _user_moves(scene, "client", x - 5, y - 5)
    _save(tmp_path, scene)
    report = board.read(tmp_path)
    assert _note(report, "server")["at"] == [1, 0] and "user_changed" not in _note(report, "server")
    assert _note(report, "client")["at"] != [1, 0], "the dropped note takes another cell"


def test_spec_ids_never_collide_with_derived_ids(tmp_path):
    spec = {
        "sections": [{"id": "sy-board-revision", "name": "S"}],
        "notes": [
            {"id": "srv", "section": "sy-board-revision", "kind": "step", "text": "A", "at": [0, 0]},
            {"id": "srv-text", "section": "sy-board-revision", "kind": "step", "text": "B", "at": [1, 0]},
        ],
        "links": [],
    }
    board.write(tmp_path, spec)
    report = board.read(tmp_path)
    assert [s["id"] for s in report["sections"]] == ["sy-board-revision"]
    assert [(n["id"], n["text"]) for n in report["notes"]] == [("srv", "A"), ("srv-text", "B")]


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (_with("client", kind="option"), "kind"),
        (_with("client", section="nowhere"), "not a frame"),
        (_with("client", at=[0, -1]), "two ints"),
        (_with("client", at=None), "null together"),
        (_with("client", at=[1, 0]), "share cell"),
        ({**SPEC, "links": [{"from": "client", "to": "api"}]}, "not a note"),
        ({**SPEC, "notes": SPEC["notes"] * 2}, "unique"),
        ({**SPEC, "lanes": []}, "unknown keys"),
    ],
)
def test_a_malformed_spec_is_refused_before_anything_is_written(tmp_path, spec, message):
    with pytest.raises(ValueError, match=message):
        board.write(tmp_path, spec)
    assert not (tmp_path / board.SCENE_NAME).exists(), "a refused spec must leave no board behind"


def test_the_tools_resolve_the_board_from_its_identifier_and_surface_a_conflict(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "repo_scratch_dir", lambda: tmp_path)
    server.board_write("pricing-idea", SPEC)
    _user_drags(tmp_path / "whiteboards" / "pricing-idea", "client", "api", (0, 1))
    assert _note(server.board_read("pricing-idea"), "client")["user_changed"] == ["at"]
    with pytest.raises(server.ToolError, match="client"):
        server.board_write("pricing-idea", SPEC)
    for tool in (server.board_read, lambda b: server.board_write(b, SPEC)):
        with pytest.raises(server.ToolError, match="board id"):
            tool("../elsewhere")
