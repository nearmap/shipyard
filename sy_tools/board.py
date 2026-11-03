"""The `/sy:whiteboard` design board: an Excalidraw scene Claude writes and the user edits by hand.

A board is a folder of files. `board.excalidraw` is a plain Excalidraw scene that opens in the
Excalidraw VS Code extension, on excalidraw.com, or in any other Excalidraw editor, so the user can drag
notes between cells and sections, edit text and labels, add notes and draw arrows without any service.
`board.state.json` records what Claude last wrote: the revision, each note's section, cell, text and
kind, each section's name, each link and its label, and the grid that write laid out. That record is what
turns the user's edits into a diff Claude can answer. `board.written.excalidraw` is a copy of the scene
as Claude last wrote it. `revisions/r<N>.json` is what revision N wrote plus its grid, and
`revisions/seen-r<N>.json` is the last save `read` or `write` saw from an editor holding revision N; both are the
board's sections, notes and links without geometry.

Sections are Excalidraw frames, stacked top to bottom. Inside a section, notes sit on a grid of cells,
`at` = [column, row]: columns are a fixed width apart, and each row is as tall as its tallest note. A
note is a rectangle with bound text, coloured by kind. A link is an arrow bound to two notes, and its
label is a text element bound to the arrow. Element ids are the spec's ids, so the same note keeps the
same id through every save; ids Claude derives (a note's text, an arrow, a label, the revision marker)
contain ':', which no spec id can, so they never collide with one.

Every write lays the whole board out again: sections in spec order, each note at its cell, every section
sized to its grid plus one spare column and one spare row for the user to drag into, everything else
framed in a section moved with it, and every arrow touching a framed note redrawn. Elements outside every
section (the scratch area) are never moved. A note's cell is read back from where it sits: a note whose
nearest cell is still the one the last write gave it keeps that cell, and any other takes the nearest cell
no such note holds, so a drag to another cell is a user edit and a nudge inside a cell snaps back on the
next write.

The write contract is one rule, applied to a note's text, section and cell, a section's name and a
link's label: a field changes only when the board still holds what Claude last wrote there, or when the
spec already matches the board. A user edit Claude has not adopted makes the write fail, naming the
element, so the session has to read the board and answer the edit before it can overwrite it. Colour is
cosmetic: a note the user recoloured keeps the user's colour. The write never deletes an element.

Every write stamps a revision marker on the board, and every read and write records the save it saw
under that revision. An editor that opened the board before a write and saves afterwards writes back an older
revision, which would otherwise read as the user deleting everything that write added. `read` detects
that, reports the user's edits as the difference from the last save a read or write saw from that revision (or from
what that revision wrote), moves the stale save aside rather than losing it, and restores the last write.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import textwrap
import time
from typing import Any
import zlib

from .atomic import atomic_write

SCENE_NAME = "board.excalidraw"
STATE_NAME = "board.state.json"
WRITTEN_NAME = "board.written.excalidraw"
REVISIONS_NAME = "revisions"
REVISION_ID = "sy:board-revision"

KIND_COLOURS = {
    "component": ("#d0ebff", "#1c4f7a"),
    "store": ("#e5dbff", "#4b2d8f"),
    "data": ("#fff3bf", "#5c4a00"),
    "external": ("#e9ecef", "#343a40"),
    "step": ("#c3fae8", "#0b5d4b"),
    "note": ("#ffe8cc", "#7a3e00"),
}
LEFT_ALIGNED_KINDS = {"data"}

NOTE_WIDTH = 280
CELL_GAP = 140
CELL_WIDTH = NOTE_WIDTH + CELL_GAP
NOMINAL_ROW_HEIGHT = 80
ROW_GAP = 70
SECTION_INSET = 30
SECTION_TOP_INSET = 40
SECTION_GAP = 100
FONT_SIZE = 16
LINE_HEIGHT = 1.25
WRAP_CHARS = 28
TEXT_PADDING = 12

_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_REVISION_TEXT = re.compile(r"revision (\d+)")
_NOTE_SHAPES = {"rectangle", "ellipse", "diamond"}
_NEVER = object()

Element = dict[str, Any]
Live = dict[str, Element]
Cell = list[int]


class BoardConflict(ValueError):
    """The board holds a user edit the spec would overwrite; read the board and answer it first."""


def folder(root: Path, board_id: str) -> Path:
    """The board's folder under `root`; refuses an id that could step outside it."""
    if not _ID.match(board_id):
        raise ValueError(f"board id {board_id!r} must be 1-64 letters, digits, '-' or '_'")
    return root / board_id


def write(directory: Path, spec: dict[str, Any]) -> dict[str, Any]:
    """Merge `spec` into the board in `directory`, creating it on first write, lay it out, and report what changed.

    `spec` is `{"sections": [{"id", "name"}], "notes": [{"id", "section", "kind", "text", "at"}], "links":
    [{"from", "to"}, optionally "label"]}`. `at` is `[column, row]`, two ints >= 0, unique within a
    section. A note's `section` and `at` may be null only to adopt a note the user dragged out of every
    section. After merging, the whole board is laid out (see the module docstring). Raises `ValueError`
    for a malformed spec or a spec cell another note the spec does not name already holds, and
    `BoardConflict` when it would overwrite a user edit not yet adopted, when the board file is a stale
    save `read` has not yet restored, when a laid-out section would cover a scratch element, or when an element
    framed in a section sits left of or above the section's grid origin, in a margin a resize opened. Raises
    `FileNotFoundError` when the board file is missing after a first write.
    """
    sections, notes, links = _parse_spec(spec)
    scene_path, state_path = directory / SCENE_NAME, directory / STATE_NAME
    state = _load_state(state_path)
    if scene_path.is_file():
        scene = _load_scene(scene_path)
    elif state["revision"] == 0:
        scene = _empty_scene()
    else:
        raise FileNotFoundError(
            f"{scene_path} is missing: restore it from {directory / WRITTEN_NAME}, the last write, then write again"
        )
    if (stale := _stale_revision(scene, state)) is not None:
        raise BoardConflict(
            f"the board file is revision {stale}, older than the last write ({state['revision']}): an editor "
            "saved a copy it opened before that write. Call board_read, which keeps that save aside and "
            "restores the last write, then answer it with the user"
        )
    elements: list[Element] = scene["elements"]
    live = _live(scene)
    marked = _marked_revision(scene)
    seen = _projection(live, state["grid"]) if marked is not None else None
    cells, origins = _cells(live, state["grid"])
    changed: list[str] = []

    for section in sections:
        current = live.get(section["id"])
        written = state["sections"].get(section["id"])
        if current is None:
            if written is not None:
                raise BoardConflict(
                    f"section {section['id']!r} was deleted on the board; drop it from this spec; to restore it, "
                    "add it again in the next write"
                )
            frame = _frame(section["id"], section["name"])
            elements.append(frame)
            live[frame["id"]] = frame
            changed.append(f"section {section['id']} added")
            continue
        if current["type"] != "frame":
            raise ValueError(f"section {section['id']!r} names a {current['type']} on the board, not a frame")
        last = written["name"] if written else _NEVER
        if _reconcile("section", section["id"], "name", current.get("name") or "", last, section["name"]):
            current["name"] = section["name"]
            _touch(current)
            changed.append(f"section {section['id']} renamed")

    for note in notes:
        current = live.get(note["id"])
        written = state["notes"].get(note["id"])
        text_el = _bound_text(current, live) if current is not None else None
        if text_el is None:
            if written is not None:
                raise BoardConflict(
                    f"note {note['id']!r} was deleted on the board; drop it from this spec; to restore it, add a note "
                    "of your own with its text under a new id in the next write"
                )
            if current is not None and current["type"] == "text":
                raise ValueError(
                    f"note {note['id']!r} is free text on the board, not a note: add a note of your own with its "
                    "text under a new id; the user may delete the free text"
                )
            if current is not None:
                raise ValueError(
                    f"note {note['id']!r} is a {current['type']} with no text on the board, not a note: add a note of "
                    "your own with its text under a new id; the user may delete the empty shape"
                )
            _require_section(note, live)
            shape, text = _note(note)
            elements.extend([shape, text])
            live[shape["id"]], live[text["id"]] = shape, text
            changed.append(f"note {note['id']} added to {note['section']} at {note['at']}")
            continue
        assert current is not None
        text_now = text_el.get("originalText", text_el["text"])
        if _reconcile("note", note["id"], "text", text_now, _last(written, "text"), note["text"]):
            _set_text(text_el, current, note["text"])
            changed.append(f"note {note['id']} text updated")
        section_now = _section_of(current, live)
        if _reconcile("note", note["id"], "section", section_now, _last(written, "section"), note["section"]):
            if note["section"] is None:
                raise ValueError(
                    f"note {note['id']!r}: section may be null only to adopt a note the user dragged out of "
                    "every section; a write never moves a note out of its section"
                )
            _require_section(note, live)
            for element in (current, text_el):
                element["frameId"] = note["section"]
                _touch(element)
            changed.append(f"note {note['id']} moved to section {note['section']}")
        if _reconcile("note", note["id"], "at", cells.get(note["id"]), _last(written, "at"), note["at"]):
            changed.append(f"note {note['id']} moved to cell {note['at']}")
        if written is not None and written["kind"] != note["kind"]:
            if current["backgroundColor"] == KIND_COLOURS[written["kind"]][0]:
                current["backgroundColor"], current["strokeColor"] = KIND_COLOURS[note["kind"]]
                _touch(current)
            _align_text(text_el, note["kind"])
            _set_text(text_el, current, text_el.get("originalText", text_el["text"]))
            changed.append(f"note {note['id']} is now a {note['kind']}")
        elif written is None:
            _align_text(text_el, note["kind"])
            _place_text(text_el, current)
            _touch(text_el)

    named = {note["id"]: note for note in notes}
    final = {ident: cell for ident, cell in cells.items() if ident not in named}
    holders = {(_section_of(live[ident], live), tuple(cell)): ident for ident, cell in final.items()}
    for note in notes:
        if note["section"] is None:
            continue
        if (other := holders.get((note["section"], tuple(note["at"])))) is not None:
            raise ValueError(
                f"note {note['id']!r} at {note['at']} in section {note['section']!r} is the cell note {other!r} "
                "holds, and the spec does not name it: pick another cell or adopt that note in the spec"
            )
        final[note["id"]] = note["at"]

    known = {(link["from"], link["to"]): link["label"] for link in state["links"]}
    arrows = _arrows(live)
    note_ids = _note_ids(live)
    for link in links:
        for end in (link["from"], link["to"]):
            if end not in note_ids:
                raise ValueError(f"link {link['from']}->{link['to']} names {end!r}, which is not a note on the board")
        key = (link["from"], link["to"])
        name = f"{key[0]}->{key[1]}"
        if key in arrows:
            arrow = arrows[key]
            label_el = _label(arrow, live)
            label_now = label_el.get("originalText", label_el["text"]) if label_el else ""
            if key not in known and label_now == "" and link["label"]:
                raise BoardConflict(
                    f"link {name} is the user's and has no label: adopt it with label \"\" first, then label it "
                    "in the next write"
                )
            if _reconcile("link", name, "label", label_now, known.get(key, _NEVER), link["label"]):
                if not link["label"]:
                    raise ValueError(f"link {name}: a write never deletes an element, so its label cannot be cleared")
                if label_el is None:
                    label_el = _add_label(arrow, link["label"], elements, live)
                else:
                    _set_label(label_el, link["label"])
                changed.append(f"link {name} relabelled")
            continue
        if key in known:
            raise BoardConflict(
                f"link {name} was deleted on the board; drop it from this spec; to restore it, add it again in "
                "the next write"
            )
        arrow = _arrow(live[key[0]], live[key[1]], elements)
        elements.append(arrow)
        live[arrow["id"]] = arrow
        arrows[key] = arrow
        if link["label"]:
            _add_label(arrow, link["label"], elements, live)
        changed.append(f"link {name} added")

    grid = _layout(live, [section["id"] for section in sections], final, origins)

    revision = state["revision"] + 1
    _stamp_revision(elements, live, revision)
    _frames_last(elements)
    present = _note_ids(live)
    on_board = {(a, b) for a, b, _ in _links(live)}
    spec_links = {(link["from"], link["to"]): link["label"] for link in links}
    kept_links = {**known, **spec_links}
    # Answered deletions are forgotten, so a later read never reports them again.
    new_state = {
        "revision": revision,
        "sections": {
            **{k: v for k, v in state["sections"].items() if k in live},
            **{section["id"]: {"name": section["name"]} for section in sections},
        },
        "notes": {
            **{k: v for k, v in state["notes"].items() if k in present},
            **{n["id"]: {"section": n["section"], "text": n["text"], "kind": n["kind"], "at": n["at"]} for n in notes},
        },
        "links": [{"from": a, "to": b, "label": kept_links[(a, b)]} for a, b in sorted(set(kept_links) & on_board)],
        "grid": grid,
    }
    (directory / REVISIONS_NAME).mkdir(parents=True, exist_ok=True)
    if seen is not None:
        _write_json(directory / REVISIONS_NAME / f"seen-r{marked}.json", seen)
    # Written before the state names this revision, so a crash never leaves a revision with no base to diff.
    _write_json(directory / REVISIONS_NAME / f"r{revision}.json", {**_projection(live, grid), "grid": grid})
    text = json.dumps(scene, indent=2) + "\n"
    atomic_write(scene_path, text)
    atomic_write(directory / WRITTEN_NAME, text)
    _write_json(state_path, new_state)
    return {"folder": str(directory), "scene": str(scene_path), "revision": revision, "changed": changed}


def read(directory: Path) -> dict[str, Any]:
    """Summarise the board: sections, notes and links with their owner and every user change, and deletions.

    Each section, note and link carries `owner`: `claude` for one a write placed, `user` for one added by
    hand. Each note carries `kind`: the kind Claude last wrote, null for a user's shape, and `text` for free
    text, which is never a note to adopt. `user_changed` lists the fields the user changed since the last
    write. Notes are listed in board order: section by section from the top, each by row then column, then
    notes outside every section. Every read, and every write before it changes anything, records the save
    it saw in `revisions/seen-r<N>.json`, N being the revision the save carries. When the board file is a
    stale save (an older revision than the last write), the file is first moved aside and the last write
    restored, and `stale_save` names where the stale file was kept and carries `changes`: the user's edits
    in it, against the last save a read or write saw from that revision, or else against what that revision
    wrote, in the shape `_diff` returns.
    A board is assumed open in one editor tab at a time: a saved file carries no tab identity, so two stale
    tabs at one revision share a base. Raises
    `FileNotFoundError` when no board exists in `directory`, and `AssertionError` when a stale save has
    neither base to diff against.
    """
    scene_path = directory / SCENE_NAME
    scene = _load_scene(scene_path)
    state = _load_state(directory / STATE_NAME)
    marked = _marked_revision(scene)
    stale_save = None
    if (stale := _stale_revision(scene, state)) is not None:
        seen_path = directory / REVISIONS_NAME / f"seen-r{stale}.json"
        written_path = directory / REVISIONS_NAME / f"r{stale}.json"
        base_path = seen_path if seen_path.is_file() else written_path
        assert base_path.is_file(), f"revision {stale} save has no base: neither {seen_path} nor {written_path}"
        saved = _projection(_live(scene), json.loads(written_path.read_text(encoding="utf-8"))["grid"])
        changes = _diff(json.loads(base_path.read_text(encoding="utf-8")), saved)
        aside = directory / f"board.stale-r{stale}-{int(time.time())}.excalidraw"
        atomic_write(aside, scene_path.read_text(encoding="utf-8"))
        atomic_write(scene_path, (directory / WRITTEN_NAME).read_text(encoding="utf-8"))
        scene = _load_scene(scene_path)
        stale_save = {
            "kept_as": str(aside), "its_revision": stale, "restored_revision": state["revision"], "changes": changes,
        }
    else:
        saved = _projection(_live(scene), state["grid"])
    if marked is not None:
        _write_json(directory / REVISIONS_NAME / f"seen-r{marked}.json", saved)
    live = _live(scene)

    sections = []
    for frame in _frames_top_down(live):
        written = state["sections"].get(frame["id"])
        entry: dict[str, Any] = {"id": frame["id"], "name": frame.get("name") or ""}
        entry["owner"] = "claude" if written else "user"
        if written and written["name"] != entry["name"]:
            entry["user_changed"] = ["name"]
        sections.append(entry)

    notes = []
    for ident, section, at, text, free in _board_notes(live, state["grid"]):
        written = state["notes"].get(ident)
        kind = written["kind"] if written else "text" if free else None
        entry = {
            "id": ident, "section": section, "at": at, "text": text, "kind": kind,
            "owner": "claude" if written else "user",
        }
        if written:
            now = {"section": section, "at": at, "text": text}
            if user_changed := [field for field in ("section", "at", "text") if written[field] != now[field]]:
                entry["user_changed"] = user_changed
        notes.append(entry)

    known = {(link["from"], link["to"]): link["label"] for link in state["links"]}
    links = []
    for a, b, label in _links(live):
        entry = {"from": a, "to": b, "label": label, "owner": "claude" if (a, b) in known else "user"}
        if (a, b) in known and known[(a, b)] != label:
            entry["user_changed"] = ["label"]
        links.append(entry)
    present = _note_ids(live)
    on_board = {(a, b) for a, b, _ in _links(live)}
    return {
        "folder": str(directory),
        "scene": str(scene_path),
        "stale_save": stale_save,
        "sections": sections,
        "notes": notes,
        "links": links,
        "deleted_notes": sorted(k for k in state["notes"] if k not in present),
        "deleted_links": [f"{a}->{b}" for a, b in sorted(set(known) - on_board)],
    }


def _live(scene: dict[str, Any]) -> Live:
    return {e["id"]: e for e in scene["elements"] if not e.get("isDeleted")}


def _frames_top_down(live: Live) -> list[Element]:
    return sorted((e for e in live.values() if e["type"] == "frame"), key=lambda f: (f["y"], f["x"]))


def _section_of(element: Element, live: Live) -> str | None:
    frame_id = element.get("frameId")
    return frame_id if frame_id is not None and live.get(frame_id, {}).get("type") == "frame" else None


def _board_notes(live: Live, grid: dict[str, Any]) -> list[tuple[str, str | None, Cell | None, str, bool]]:
    """Every note and free text on the board as (id, section, at, text, is free text), in board order.

    Section by section from the top, each by row then column (free text in a section after its notes, top
    to bottom), then everything outside every section, top to bottom. The revision marker is no note, and a
    shape whose text is gone is none either. Free text has no cell.
    """
    rank = {frame["id"]: i for i, frame in enumerate(_frames_top_down(live))}
    cells, _ = _cells(live, grid)
    found = []
    for element in live.values():
        if element["type"] in _NOTE_SHAPES:
            text_el = _bound_text(element, live)
            if text_el is None:
                continue
            text = text_el.get("originalText", text_el["text"])
        elif element["type"] == "text" and not element.get("containerId") and element["id"] != REVISION_ID:
            text = element.get("originalText", element["text"])
        else:
            continue
        section = _section_of(element, live)
        at = cells.get(element["id"])
        order = (0, at[1], at[0]) if at is not None else (1, element["y"], element["x"])
        free = element["type"] == "text"
        found.append(((rank.get(section, math.inf), *order), (element["id"], section, at, text, free)))
    return [note for _, note in sorted(found, key=lambda pair: pair[0])]


def _row_offset(rows: list[float], row: int) -> float:
    """Row `row`'s y offset from its frame's top: recorded rows, then nominal rows beyond them."""
    if row < len(rows):
        return rows[row]
    step = NOMINAL_ROW_HEIGHT + ROW_GAP
    if not rows:
        return SECTION_TOP_INSET + row * step
    return rows[-1] + (row - len(rows) + 1) * step


def _cells(live: Live, grid: dict[str, Any]) -> tuple[dict[str, Cell], dict[str, tuple[float, float]]]:
    """Each framed note's cell, read back against its section's recorded grid, and each section's grid origin.

    Per axis, the grid keeps the recorded frame's origin when the frame's far edge or its centre is where the
    last write left it (the frame was resized from its near edge, or symmetrically about its centre, which
    does not move the notes) and takes the current origin otherwise (the frame was dragged, and the notes
    went with it). A note whose nearest cell is the
    cell the last write gave it in this section keeps it; every other note, top to bottom, takes the
    nearest cell not already held, ties by (distance, row, column).
    """
    by_frame: dict[str, list[Element]] = {}
    for element in live.values():
        section = _section_of(element, live)
        if section is not None and element["type"] in _NOTE_SHAPES and _bound_text(element, live) is not None:
            by_frame.setdefault(section, []).append(element)
    cells: dict[str, Cell] = {}
    origins: dict[str, tuple[float, float]] = {}
    recorded_grids: dict[str, dict[str, Any]] = {}
    for frame in _frames_top_down(live):
        recorded = grid.get(frame["id"], {
            "x": frame["x"], "y": frame["y"], "width": frame["width"], "height": frame["height"],
            "cols": 0, "rows": [], "cells": {},
        })
        origin = []
        for axis, size in (("x", "width"), ("y", "height")):
            far_edge_kept = math.isclose(frame[axis] + frame[size], recorded[axis] + recorded[size], abs_tol=1)
            centre_kept = math.isclose(
                2 * frame[axis] + frame[size], 2 * recorded[axis] + recorded[size], abs_tol=1
            )
            origin.append(recorded[axis] if far_edge_kept or centre_kept else frame[axis])
        recorded_grids[frame["id"]] = recorded
        origins[frame["id"]] = (origin[0], origin[1])
    for section, shapes in by_frame.items():
        recorded = recorded_grids[section]
        origin = origins[section]
        rows = recorded["rows"]
        shapes.sort(key=lambda e: (e["y"], e["x"]))
        span = len(shapes) + 2
        candidates: dict[str, list[tuple[float, int, int]]] = {}
        for shape in shapes:
            rel_x, rel_y = shape["x"] - origin[0], shape["y"] - origin[1]
            near_col = max(0, round((rel_x - SECTION_INSET) / CELL_WIDTH))
            near_row = max(0, round((rel_y - SECTION_TOP_INSET) / (NOMINAL_ROW_HEIGHT + ROW_GAP)))
            cols = range(max(near_col, recorded["cols"]) + span)
            row_range = range(max(near_row, len(rows)) + span)
            candidates[shape["id"]] = sorted(
                (math.hypot(SECTION_INSET + c * CELL_WIDTH - rel_x, _row_offset(rows, r) - rel_y), r, c)
                for c in cols for r in row_range
            )
        held: set[tuple[int, int]] = set()
        moved = []
        for shape in shapes:
            _, row, col = candidates[shape["id"]][0]
            if recorded["cells"].get(shape["id"]) == [col, row]:
                held.add((col, row))
                cells[shape["id"]] = [col, row]
            else:
                moved.append(shape)
        for shape in moved:
            _, row, col = next(c for c in candidates[shape["id"]] if (c[2], c[1]) not in held)
            held.add((col, row))
            cells[shape["id"]] = [col, row]
    return cells, origins


def _layout(
    live: Live, section_order: list[str], cells: dict[str, Cell], origins: dict[str, tuple[float, float]],
) -> dict[str, Any]:
    """Lay the board out and return the grid it laid, per section: frame box, columns, row offsets, note cells.

    Sections in `section_order`, then any other frame by current y, each at x = 0, stacked top to bottom.
    Each note in `cells` goes to its cell and its text follows; everything else framed in a section moves
    with its notes, from the origin `_cells` chose; each frame is sized to its grid (one spare column and row
    included) and to what it frames;
    every arrow between two notes, at least one framed in a section, is redrawn and its label centred on it.
    Unframed elements stay put, so an arrow between two scratch notes keeps its geometry.

    Raises `BoardConflict` when an element framed in a section sits left of or above that origin, and when a
    laid-out section would cover a scratch element.
    """
    before = {ident: _geometry(e) for ident, e in live.items()}
    frames = [live[ident] for ident in section_order] + [
        e for e in _frames_top_down(live) if e["id"] not in section_order
    ]
    notes = _note_ids(live)
    note_arrows: dict[str, tuple[str, str]] = {}
    for ident, element in live.items():
        if element["type"] == "arrow":
            a, b = _link_key(element, live)
            if a in notes and b in notes:
                note_arrows[ident] = (a, b)
    linked = {
        ident: ends for ident, ends in note_arrows.items()
        if any(_section_of(live[end], live) is not None for end in ends)
    }
    labels = {e["id"] for e in live.values() if e.get("containerId") in note_arrows}
    note_texts = {text["id"] for ident in cells if (text := _bound_text(live[ident], live)) is not None}
    grid: dict[str, Any] = {}
    y = 0.0
    for frame in frames:
        shapes = [live[ident] for ident in cells if live[ident].get("frameId") == frame["id"]]
        heights: dict[int, float] = {}
        for shape in shapes:
            row = cells[shape["id"]][1]
            heights[row] = max(heights.get(row, 0.0), shape["height"])
        cols = max((cells[s["id"]][0] for s in shapes), default=-1) + 2
        rows: list[float] = []
        offset = float(SECTION_TOP_INSET)
        for row in range(max((cells[s["id"]][1] for s in shapes), default=-1) + 2):
            rows.append(offset)
            offset += heights.get(row, NOMINAL_ROW_HEIGHT) + ROW_GAP
        ox, oy = origins.get(frame["id"], (frame["x"], frame["y"]))
        gdx, gdy = -ox, y - oy
        frame["x"], frame["y"] = 0.0, y
        laid_out = set(cells) | note_texts | set(note_arrows) | labels | {REVISION_ID}
        others = [e for e in live.values() if e.get("frameId") == frame["id"] and e["id"] not in laid_out]
        for element in others:
            left, top, _, _ = _extent(element)
            if left + gdx < 0 or top + gdy < y:
                raise BoardConflict(
                    f"element {element['id']!r} sits in the margin a resize opened on section {frame['id']!r}, left "
                    "of or above its notes: ask the user to move it level with or right of and below the section's "
                    "first cell, then write again"
                )
            element["x"] += gdx
            element["y"] += gdy
        texts = []
        for shape in shapes:
            col, row = cells[shape["id"]]
            shape["x"], shape["y"] = frame["x"] + SECTION_INSET + col * CELL_WIDTH, frame["y"] + rows[row]
            text_el = _bound_text(shape, live)
            assert text_el is not None
            _place_text(text_el, shape)
            texts.append(text_el)
        members = [*shapes, *texts, *others]
        right = max([SECTION_INSET + cols * CELL_WIDTH - CELL_GAP, *(_extent(e)[2] - frame["x"] for e in members)])
        bottom = max([offset - ROW_GAP, *(_extent(e)[3] - frame["y"] for e in members)])
        frame["width"], frame["height"] = right + SECTION_INSET, bottom + SECTION_INSET
        grid[frame["id"]] = {
            "x": frame["x"], "y": frame["y"], "width": frame["width"], "height": frame["height"],
            "cols": cols, "rows": rows, "cells": {shape["id"]: cells[shape["id"]] for shape in shapes},
        }
        y += frame["height"] + SECTION_GAP
    for element in live.values():
        if (
            element.get("frameId") is None and element["type"] not in ("frame", "arrow", "line")
            and not element.get("containerId") and element["id"] != REVISION_ID
        ):
            left, top, right, bottom = _extent(element)
            for frame in frames:
                if (
                    left < frame["x"] + frame["width"] and right > frame["x"]
                    and top < frame["y"] + frame["height"] and bottom > frame["y"]
                ):
                    raise BoardConflict(
                        f"scratch element {element['id']!r} sits where section {frame['id']!r} now reaches: ask the "
                        "user to move it clear of that section, then write again"
                    )
    for ident, (source, target) in linked.items():
        arrow = live[ident]
        (sx, sy), (tx, ty) = _facing_edges(live[source], live[target])
        arrow.update(x=sx, y=sy, width=abs(tx - sx), height=abs(ty - sy), points=[[0, 0], [tx - sx, ty - sy]])
        if (label_el := _label(arrow, live)) is not None:
            _centre_label(label_el, arrow)
    for ident, element in live.items():
        if ident in before and _geometry(element) != before[ident]:
            _touch(element)
    return grid


def _extent(element: Element) -> tuple[float, float, float, float]:
    """The element's (left, top, right, bottom) once its `angle` turns it about the centre of its unturned box.

    An arrow's or stroke's points may run left or up from x, y, so its unturned box spans its points.
    """
    x, y = element["x"], element["y"]
    if "points" in element:
        corners = [(x + point[0], y + point[1]) for point in element["points"]]
    else:
        right, bottom = x + element["width"], y + element["height"]
        corners = [(x, y), (right, y), (x, bottom), (right, bottom)]
    if element["angle"]:
        xs, ys = [c[0] for c in corners], [c[1] for c in corners]
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        cos, sin = math.cos(element["angle"]), math.sin(element["angle"])
        corners = [
            (cx + (px - cx) * cos - (py - cy) * sin, cy + (px - cx) * sin + (py - cy) * cos) for px, py in corners
        ]
    xs, ys = [c[0] for c in corners], [c[1] for c in corners]
    return min(xs), min(ys), max(xs), max(ys)


def _arrows(live: Live) -> dict[tuple[str, str], Element]:
    """Each arrow bound to two elements, keyed by the (from, to) ids it joins; the first wins a repeat."""
    found: dict[tuple[str, str], Element] = {}
    for element in live.values():
        if element["type"] == "arrow":
            a, b = _link_key(element, live)
            if a is not None and b is not None:
                found.setdefault((a, b), element)
    return found


def _label(arrow: Element, live: Live) -> Element | None:
    """The arrow's label: the text element bound to it, found by `containerId`, never by geometry."""
    return next(
        (e for e in live.values() if e["type"] == "text" and e.get("containerId") == arrow["id"]), None,
    )


def _links(live: Live) -> list[tuple[str, str, str]]:
    notes = _note_ids(live)
    found = []
    for (a, b), arrow in _arrows(live).items():
        if a in notes and b in notes:
            label_el = _label(arrow, live)
            found.append((a, b, label_el.get("originalText", label_el["text"]) if label_el else ""))
    return sorted(found)


def _projection(live: Live, grid: dict[str, Any]) -> dict[str, Any]:
    """The board without geometry: what a stale save's edits are diffed against."""
    return {
        "sections": {e["id"]: e.get("name") or "" for e in live.values() if e["type"] == "frame"},
        "notes": {
            ident: {"section": section, "at": at, "text": text}
            for ident, section, at, text, _ in _board_notes(live, grid)
        },
        "links": [[a, b, label] for a, b, label in _links(live)],
    }


def _diff(base: dict[str, Any], now: dict[str, Any]) -> dict[str, Any]:
    """What changed from projection `base` to projection `now`.

    `notes` holds `added`, `deleted`, `moved` and `edited`, each a list of `{id, section, at, text}`;
    `sections` holds `added`, `deleted` and `renamed`, each a list of `{id, name}`; `links` holds `added`,
    `deleted` and `relabelled`, each a list of `[from, to, label]`.
    """
    before, after = base["notes"], now["notes"]
    old_sections, new_sections = base["sections"], now["sections"]
    old_links = {(a, b): label for a, b, label in base["links"]}
    new_links = {(a, b): label for a, b, label in now["links"]}

    def moved(i: str, n: dict[str, Any]) -> bool:
        return (n["section"], n["at"]) != (before[i]["section"], before[i]["at"])

    return {
        "notes": {
            "added": [{"id": i, **n} for i, n in after.items() if i not in before],
            "deleted": [{"id": i, **n} for i, n in before.items() if i not in after],
            "moved": [{"id": i, **n} for i, n in after.items() if i in before and moved(i, n)],
            "edited": [{"id": i, **n} for i, n in after.items() if i in before and n["text"] != before[i]["text"]],
        },
        "sections": {
            "added": [{"id": i, "name": n} for i, n in new_sections.items() if i not in old_sections],
            "deleted": [{"id": i, "name": n} for i, n in old_sections.items() if i not in new_sections],
            "renamed": [
                {"id": i, "name": n} for i, n in new_sections.items() if i in old_sections and n != old_sections[i]
            ],
        },
        "links": {
            "added": [[a, b, label] for (a, b), label in new_links.items() if (a, b) not in old_links],
            "deleted": [[a, b, label] for (a, b), label in old_links.items() if (a, b) not in new_links],
            "relabelled": [
                [a, b, label] for (a, b), label in new_links.items()
                if (a, b) in old_links and label != old_links[(a, b)]
            ],
        },
    }


def _write_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _parse_spec(spec: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    unknown = set(spec) - {"sections", "notes", "links"}
    if unknown:
        raise ValueError(f"spec has unknown keys {sorted(unknown)}; it takes sections, notes and links")
    sections = [_fields(section, "section", ("id", "name")) for section in spec.get("sections", [])]
    notes = [
        _fields(note, "note", ("id", "section", "kind", "text", "at"), nullable=("section",), cell="at")
        for note in spec.get("notes", [])
    ]
    links = [
        _fields({"label": "", **link} if isinstance(link, dict) else link, "link", ("from", "to", "label"))
        for link in spec.get("links", [])
    ]
    pairs = [(link["from"], link["to"]) for link in links]
    if dupes := sorted({pair for pair in pairs if pairs.count(pair) > 1}):
        raise ValueError(f"links must be unique by from and to; repeated: {dupes}")
    ids = [x["id"] for x in sections + notes]
    if dupes := sorted({i for i in ids if ids.count(i) > 1}):
        raise ValueError(f"ids must be unique across sections and notes; repeated: {dupes}")
    for item in sections + notes:
        if not _ID.match(item["id"]):
            raise ValueError(f"id {item['id']!r} must be 1-64 letters, digits, '-' or '_'")
    taken: dict[tuple[str, tuple[int, ...]], str] = {}
    for note in notes:
        if note["kind"] not in KIND_COLOURS:
            raise ValueError(f"note {note['id']!r} has kind {note['kind']!r}; use one of {sorted(KIND_COLOURS)}")
        if not note["text"].strip():
            raise ValueError(f"note {note['id']!r} has no text")
        if (note["section"] is None) != (note["at"] is None):
            raise ValueError(f"note {note['id']!r}: section and at are null together or not at all")
        if note["section"] is not None:
            note["at"] = list(note["at"])
            if (other := taken.setdefault((note["section"], tuple(note["at"])), note["id"])) != note["id"]:
                raise ValueError(f"notes {other!r} and {note['id']!r} share cell {note['at']} in {note['section']!r}")
    return sections, notes, links


def _fields(
    item: Any, what: str, keys: tuple[str, ...], nullable: tuple[str, ...] = (), cell: str = "",
) -> dict[str, Any]:
    if not isinstance(item, dict) or set(item) != set(keys):
        raise ValueError(f"each {what} must be an object with exactly the fields {list(keys)}: {item!r}")
    for key in keys:
        value = item[key]
        if value is None and (key in nullable or key == cell):
            continue
        if key == cell:
            if not (
                isinstance(value, list | tuple) and len(value) == 2
                and all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in value)
            ):
                raise ValueError(f"each {what}'s {key} must be [column, row], two ints >= 0: {item!r}")
        elif not isinstance(value, str):
            raise ValueError(f"each {what}'s field {key!r} must be a string: {item!r}")
    return dict(item)


def _last(written: dict[str, Any] | None, field: str) -> Any:
    return written[field] if written is not None else _NEVER


def _reconcile(what: str, ident: str, field: str, current: Any, last: Any, wanted: Any) -> bool:
    """True when the field should be set to `wanted`; raises when that would overwrite an unadopted user edit.

    `last` is what Claude last wrote to the field, or `_NEVER` when Claude never wrote it.
    """
    if current == wanted:
        return False
    if last is not _NEVER and current == last:
        return True
    shown = "nothing" if last is _NEVER else repr(last)
    raise BoardConflict(
        f"{what} {ident!r}: the user changed {field} on the board to {current!r} (last written {shown}); "
        f"adopt it in the spec or argue it with the user before writing {wanted!r}"
    )


def _require_section(note: dict[str, Any], live: Live) -> None:
    if note["section"] is None or live.get(note["section"], {}).get("type") != "frame":
        raise ValueError(f"note {note['id']!r} names section {note['section']!r}, which is not a frame on the board")


def _load_scene(path: Path) -> dict[str, Any]:
    scene = json.loads(path.read_text(encoding="utf-8"))
    assert scene.get("type") == "excalidraw", f"{path} is not an Excalidraw scene (type={scene.get('type')!r})"
    return scene


def _load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"revision": 0, "sections": {}, "notes": {}, "links": [], "grid": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _empty_scene() -> dict[str, Any]:
    return {
        "type": "excalidraw",
        "version": 2,
        "source": "shipyard /sy:whiteboard",
        "elements": [],
        "appState": {"viewBackgroundColor": "#ffffff", "gridSize": None},
        "files": {},
    }


def _stale_revision(scene: dict[str, Any], state: dict[str, Any]) -> int | None:
    """The scene's revision when it is older than the last write; None when current or unmarked."""
    revision = _marked_revision(scene)
    return revision if revision is not None and revision < state["revision"] else None


def _marked_revision(scene: dict[str, Any]) -> int | None:
    """The revision the scene's marker names; None when the user removed the marker."""
    marker = next((e for e in scene["elements"] if e["id"] == REVISION_ID and not e.get("isDeleted")), None)
    found = _REVISION_TEXT.search(marker.get("originalText", marker.get("text", ""))) if marker else None
    return int(found.group(1)) if found else None


def _stamp_revision(elements: list[Element], live: Live, revision: int) -> None:
    label = f"Board revision {revision}, written by /sy:whiteboard"
    marker = live.get(REVISION_ID)
    if marker is None:
        marker = _base(REVISION_ID, "text", 0, -48, 0, FONT_SIZE * LINE_HEIGHT)
        marker.update(
            strokeColor="#868e96", boundElements=None, fontSize=12, fontFamily=2, textAlign="left",
            verticalAlign="top", containerId=None, lineHeight=LINE_HEIGHT, autoResize=True,
        )
        elements.append(marker)
        live[REVISION_ID] = marker
    marker.update(text=label, originalText=label, width=len(label) * 12 * 0.55, frameId=None)
    _touch(marker)


def _note_ids(live: Live) -> set[str]:
    """Ids of every shape on the board that still carries its text: a shape whose text is gone is no note."""
    return {e["id"] for e in live.values() if e["type"] in _NOTE_SHAPES and _bound_text(e, live) is not None}


def _seed(ident: str, salt: int = 0) -> int:
    return zlib.crc32(f"{ident}:{salt}".encode()) & 0x7FFFFFFF


def _base(ident: str, kind: str, x: float, y: float, w: float, h: float) -> Element:
    return {
        "id": ident, "type": kind, "x": x, "y": y, "width": w, "height": h, "angle": 0,
        "strokeColor": "#1e1e1e", "backgroundColor": "transparent", "fillStyle": "solid", "strokeWidth": 2,
        "strokeStyle": "solid", "roughness": 1, "opacity": 100, "groupIds": [], "frameId": None,
        "roundness": None, "seed": _seed(ident), "version": 1, "versionNonce": _seed(ident, 1),
        "isDeleted": False, "boundElements": [], "updated": _now(), "link": None, "locked": False,
    }


def _now() -> int:
    return int(time.time() * 1000)


def _touch(element: Element) -> None:
    element["version"] = element.get("version", 1) + 1
    element["versionNonce"] = _seed(element["id"], element["version"])
    element["updated"] = _now()


def _frame(ident: str, name: str) -> Element:
    frame = _base(ident, "frame", 0, 0, 0, 0)
    frame.update(name=name, strokeColor="#bbbbbb", boundElements=None)
    return frame


def _wrap(text: str) -> str:
    return "\n".join(
        "\n".join(textwrap.wrap(line, WRAP_CHARS, break_long_words=True)) if line.strip() else ""
        for line in text.split("\n")
    )


def _text_size(wrapped: str) -> tuple[float, float]:
    lines = wrapped.split("\n")
    return max(len(line) for line in lines) * FONT_SIZE * 0.55, len(lines) * FONT_SIZE * LINE_HEIGHT


def _note(note: dict[str, Any]) -> tuple[Element, Element]:
    """A new note in its section, at the frame's origin until the layout puts it in its cell."""
    background, stroke = KIND_COLOURS[note["kind"]]
    shape = _base(note["id"], "rectangle", 0, 0, NOTE_WIDTH, NOMINAL_ROW_HEIGHT)
    text_id = f"{note['id']}:text"
    shape.update(
        backgroundColor=background, strokeColor=stroke, roundness={"type": 3}, frameId=note["section"],
        boundElements=[{"id": text_id, "type": "text"}],
    )
    text = _base(text_id, "text", 0, 0, 0, 0)
    text.update(
        strokeColor=stroke, frameId=note["section"], boundElements=None, fontSize=FONT_SIZE, fontFamily=1,
        containerId=note["id"], lineHeight=LINE_HEIGHT, autoResize=True,
    )
    _align_text(text, note["kind"])
    _set_text(text, shape, note["text"])
    return shape, text


def _align_text(text_el: Element, kind: str) -> None:
    """Schema-style `data` text sits top-left, one field per line; every other kind's text is centred."""
    left = kind in LEFT_ALIGNED_KINDS
    text_el.update(textAlign="left" if left else "center", verticalAlign="top" if left else "middle")


def _bound_text(shape: Element, live: Live) -> Element | None:
    for bound in shape.get("boundElements") or []:
        if bound.get("type") == "text" and bound["id"] in live:
            return live[bound["id"]]
    return None


def _set_text(text_el: Element, shape: Element, text: str) -> None:
    wrapped = _wrap(text)
    tw, th = _text_size(wrapped)
    shape["height"] = max(float(NOMINAL_ROW_HEIGHT), th + 2 * TEXT_PADDING)
    text_el.update(text=wrapped, originalText=text, width=tw, height=th)
    _place_text(text_el, shape)
    _touch(text_el)
    _touch(shape)


def _place_text(text_el: Element, shape: Element) -> None:
    """Put bound text where its alignment says inside its shape: inset top-left, or centred."""
    if text_el.get("textAlign") == "left":
        text_el["x"] = shape["x"] + TEXT_PADDING
    else:
        text_el["x"] = shape["x"] + (shape["width"] - text_el["width"]) / 2
    if text_el.get("verticalAlign") == "top":
        text_el["y"] = shape["y"] + TEXT_PADDING
    else:
        text_el["y"] = shape["y"] + (shape["height"] - text_el["height"]) / 2


def _add_label(arrow: Element, text: str, elements: list[Element], live: Live) -> Element:
    """A label bound to `arrow`, with every field Excalidraw needs to keep it when the arrow next moves."""
    label = _base(_unused_id(f"{arrow['id']}:label", elements), "text", 0, 0, 0, 0)
    label.update(
        boundElements=None, fontSize=FONT_SIZE, fontFamily=1, textAlign="center", verticalAlign="middle",
        containerId=arrow["id"], lineHeight=LINE_HEIGHT, autoResize=True,
    )
    _set_label(label, text)
    arrow["boundElements"] = [*(arrow.get("boundElements") or []), {"type": "text", "id": label["id"]}]
    _touch(arrow)
    elements.append(label)
    live[label["id"]] = label
    return label


def _set_label(label: Element, text: str) -> None:
    width, height = _text_size(text)
    label.update(text=text, originalText=text, width=width, height=height)
    _touch(label)


def _centre_label(label: Element, arrow: Element) -> None:
    first, last = arrow["points"][0], arrow["points"][-1]
    mid_x, mid_y = arrow["x"] + (first[0] + last[0]) / 2, arrow["y"] + (first[1] + last[1]) / 2
    label["x"], label["y"] = mid_x - label["width"] / 2, mid_y - label["height"] / 2


def _arrow(source: Element, target: Element, elements: list[Element]) -> Element:
    """An arrow bound to both notes and to no frame, so a link between two sections is never clipped."""
    (sx, sy), (tx, ty) = _facing_edges(source, target)
    # A re-pointed arrow keeps its id, so the id this link derives may already be taken.
    ident = _unused_id(f"link:{source['id']}:{target['id']}", elements)
    arrow = _base(ident, "arrow", sx, sy, abs(tx - sx), abs(ty - sy))
    arrow.update(
        points=[[0, 0], [tx - sx, ty - sy]], lastCommittedPoint=None, startArrowhead=None, endArrowhead="arrow",
        startBinding={"elementId": source["id"], "focus": 0, "gap": 8},
        endBinding={"elementId": target["id"], "focus": 0, "gap": 8},
        roundness={"type": 2}, boundElements=None, elbowed=False,
    )
    for end in (source, target):
        end["boundElements"] = [*(end.get("boundElements") or []), {"id": ident, "type": "arrow"}]
        _touch(end)
    return arrow


def _unused_id(base: str, elements: list[Element]) -> str:
    """`base`, or `base:2`, `base:3`... when an element in the scene, deleted or not, already has it."""
    taken = {e["id"] for e in elements}
    ident, n = base, 1
    while ident in taken:
        n += 1
        ident = f"{base}:{n}"
    return ident


def _facing_edges(a: Element, b: Element) -> tuple[tuple[float, float], tuple[float, float]]:
    """Midpoints of the two edges that face each other: top/bottom when the notes are stacked, else sides."""
    acx, acy = a["x"] + a["width"] / 2, a["y"] + a["height"] / 2
    bcx, bcy = b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
    if abs(bcx - acx) < (a["width"] + b["width"]) / 2:
        if bcy >= acy:
            return (acx, a["y"] + a["height"]), (bcx, b["y"])
        return (acx, a["y"]), (bcx, b["y"] + b["height"])
    if bcx >= acx:
        return (a["x"] + a["width"], acy), (b["x"], bcy)
    return (a["x"], acy), (b["x"] + b["width"], bcy)


def _link_key(arrow: Element, live: Live) -> tuple[str | None, str | None]:
    """The (from, to) note ids an arrow joins, resolving a binding to a note's text onto the note itself."""
    ends = []
    for binding in (arrow.get("startBinding"), arrow.get("endBinding")):
        target = live.get(binding["elementId"]) if binding else None
        if target is not None and target["type"] == "text" and target.get("containerId"):
            target = live.get(target["containerId"])
        ends.append(target["id"] if target is not None else None)
    return (ends[0], ends[1]) if None not in ends else (None, None)


def _frames_last(elements: list[Element]) -> None:
    elements.sort(key=lambda e: e["type"] == "frame")


def _geometry(element: Element) -> tuple[Any, ...]:
    return element["x"], element["y"], element["width"], element["height"], element.get("points")
