"""The `/sy:whiteboard` board: an Excalidraw scene Claude writes and the user edits by hand.

A board is two files in one scratch directory. `board.excalidraw` is a plain Excalidraw scene that
opens in the Excalidraw VS Code extension, on excalidraw.com, or in any other Excalidraw editor, so the
user can drag notes between lanes, edit text, add notes and draw arrows without any service.
`board.state.json` records what Claude last wrote: each note's lane and text, each lane's name, and
each link. That record is what turns the user's edits into a diff Claude can answer.

The write contract is one rule, applied to every field Claude owns: a field changes only when the
board still holds what Claude last wrote there, or when the spec already matches the board. A user
edit Claude has not adopted makes the write fail, naming the note, so the session has to read the
board and answer the edit before it can overwrite it. The write never deletes or repositions an
element that already exists; ruling a note out means moving it to another lane, not removing it.

Lanes are Excalidraw frames. A note is a rectangle with bound text. A link is an arrow bound to two
notes. Element ids are the spec's ids, so the same note keeps the same id through every save.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import textwrap
import time
from typing import Any
import zlib

from .atomic import atomic_write

SCENE_NAME = "board.excalidraw"
STATE_NAME = "board.state.json"

KIND_COLOURS = {
    "status": ("#e9ecef", "#343a40"),
    "option": ("#fff3bf", "#5c4a00"),
    "decision": ("#d0ebff", "#1c4f7a"),
    "hypothesis": ("#e5dbff", "#4b2d8f"),
    "probe": ("#c3fae8", "#0b5d4b"),
    "objection": ("#ffc9c9", "#7a1c1c"),
}

LANE_WIDTH = 340
LANE_HEIGHT = 360
LANE_GAP = 60
NOTE_WIDTH = 280
NOTE_INSET = 30
NOTE_GAP = 44
FIRST_NOTE_OFFSET = 50
FONT_SIZE = 16
LINE_HEIGHT = 1.25
WRAP_CHARS = 28
TEXT_PADDING = 12

_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_NOTE_SHAPES = {"rectangle", "ellipse", "diamond"}

Element = dict[str, Any]
Live = dict[str, Element]


class BoardConflict(ValueError):
    """The board holds a user edit the spec would overwrite; read the board and answer it first."""


def write(directory: Path, spec: dict[str, Any]) -> dict[str, Any]:
    """Merge `spec` into the board in `directory`, creating it on first write, and report what changed.

    `spec` is `{"lanes": [{"id", "name"}], "notes": [{"id", "lane", "kind", "text"}], "links": [{"from",
    "to"}]}`. Raises `ValueError` for a malformed spec and `BoardConflict` when it would overwrite a user
    edit not yet adopted.
    """
    lanes, notes, links = _parse_spec(spec)
    scene_path, state_path = directory / SCENE_NAME, directory / STATE_NAME
    scene = _load_scene(scene_path) if scene_path.is_file() else _empty_scene()
    state = _load_state(state_path)
    live = {e["id"]: e for e in scene["elements"] if not e.get("isDeleted")}
    elements: list[Element] = scene["elements"]
    changed: list[str] = []

    for lane in lanes:
        current = live.get(lane["id"])
        written = state["lanes"].get(lane["id"])
        if current is None:
            if written is not None:
                raise BoardConflict(f"lane {lane['id']!r} was deleted on the board; drop it from the spec or argue it")
            frame = _frame(lane["id"], lane["name"], _next_lane_x(live), 0)
            elements.append(frame)
            live[frame["id"]] = frame
            changed.append(f"lane {lane['id']} added")
            continue
        if current["type"] != "frame":
            raise ValueError(f"lane {lane['id']!r} names a {current['type']} on the board, not a frame")
        if _reconcile("lane", lane["id"], "name", current.get("name"), written and written["name"], lane["name"]):
            current["name"] = lane["name"]
            _touch(current)
            changed.append(f"lane {lane['id']} renamed")

    for note in notes:
        current = live.get(note["id"])
        written = state["notes"].get(note["id"])
        if note["lane"] not in live or live[note["lane"]]["type"] != "frame":
            raise ValueError(f"note {note['id']!r} names lane {note['lane']!r}, which is not a frame on the board")
        if current is None:
            if written is not None:
                raise BoardConflict(f"note {note['id']!r} was deleted on the board; drop it from the spec or argue it")
            shape, text = _note(note, live[note["lane"]], live)
            elements.extend([shape, text])
            live[shape["id"]], live[text["id"]] = shape, text
            _fit_lane(live[note["lane"]], shape)
            changed.append(f"note {note['id']} added to {note['lane']}")
            continue
        text_el = _bound_text(current, live)
        if text_el is None:
            raise ValueError(f"note {note['id']!r} is a {current['type']} with no text on the board, not a note")
        text_now = text_el.get("originalText", text_el["text"])
        if _reconcile("note", note["id"], "text", text_now, written and written["text"], note["text"]):
            _set_text(text_el, current, note["text"])
            changed.append(f"note {note['id']} text updated")
        if _reconcile("note", note["id"], "lane", current.get("frameId"), written and written["lane"], note["lane"]):
            _move_to_lane(current, text_el, live[note["lane"]], live)
            changed.append(f"note {note['id']} moved to {note['lane']}")
        # Only a note Claude wrote is recoloured: a hand-made note keeps whatever colour the user gave it.
        if written is not None:
            written_colour = KIND_COLOURS[written["kind"]][0]
            wanted_colour, wanted_stroke = KIND_COLOURS[note["kind"]]
            if _reconcile("note", note["id"], "colour", current["backgroundColor"], written_colour, wanted_colour):
                current["backgroundColor"], current["strokeColor"] = wanted_colour, wanted_stroke
                _touch(current)
                changed.append(f"note {note['id']} is now a {note['kind']}")

    known_links = {(link["from"], link["to"]) for link in state["links"]}
    arrows = {_link_key(e, live) for e in live.values() if e["type"] == "arrow"}
    for link in links:
        for end in (link["from"], link["to"]):
            if live.get(end, {}).get("type") not in _NOTE_SHAPES:
                raise ValueError(f"link {link['from']}->{link['to']} names {end!r}, which is not a note on the board")
        key = (link["from"], link["to"])
        if key in arrows:
            continue
        if key in known_links:
            raise BoardConflict(f"link {key[0]}->{key[1]} was deleted on the board; drop it from the spec or argue it")
        arrow = _arrow(live[key[0]], live[key[1]])
        elements.append(arrow)
        live[arrow["id"]] = arrow
        arrows.add(key)
        changed.append(f"link {key[0]}->{key[1]} added")

    _frames_last(elements)
    # Anything Claude wrote that the user has since deleted, and the spec no longer names, is forgotten here:
    # the deletion has been answered, so a later read must not report it again.
    new_state = {
        "lanes": {
            **{k: v for k, v in state["lanes"].items() if k in live},
            **{lane["id"]: {"name": lane["name"]} for lane in lanes},
        },
        "notes": {
            **{k: v for k, v in state["notes"].items() if k in live},
            **{n["id"]: {"lane": n["lane"], "text": n["text"], "kind": n["kind"]} for n in notes},
        },
        "links": [
            {"from": a, "to": b}
            for a, b in sorted((known_links | {(link["from"], link["to"]) for link in links}) & arrows)
        ],
    }
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write(scene_path, json.dumps(scene, indent=2) + "\n")
    atomic_write(state_path, json.dumps(new_state, indent=2, sort_keys=True) + "\n")
    return {"scene": str(scene_path), "changed": changed}


def read(directory: Path) -> dict[str, Any]:
    """Summarise the board for the session: lanes, notes with every user change, links, deletions.

    Each note carries `owner` (`claude` for a note Claude wrote, `user` for one added by hand) and
    `user_changed`, the fields the user changed since Claude last wrote them. Raises `FileNotFoundError`
    when no board exists in `directory`.
    """
    scene_path = directory / SCENE_NAME
    scene = _load_scene(scene_path)
    state = _load_state(directory / STATE_NAME)
    live = {e["id"]: e for e in scene["elements"] if not e.get("isDeleted")}
    frames = {e["id"]: e for e in live.values() if e["type"] == "frame"}

    lanes = []
    for frame in sorted(frames.values(), key=lambda f: (f["x"], f["y"])):
        written = state["lanes"].get(frame["id"])
        entry: dict[str, Any] = {"id": frame["id"], "name": frame.get("name") or ""}
        entry["owner"] = "claude" if written else "user"
        if written and written["name"] != entry["name"]:
            entry["user_changed"] = ["name"]
        lanes.append(entry)

    notes = []
    for element in sorted(live.values(), key=lambda e: (e["x"], e["y"])):
        if element["type"] in _NOTE_SHAPES:
            text_el = _bound_text(element, live)
            if text_el is None:
                continue
            text = text_el.get("originalText", text_el["text"])
        elif element["type"] == "text" and not element.get("containerId"):
            text = element.get("originalText", element["text"])
        else:
            continue
        lane = element.get("frameId")
        written = state["notes"].get(element["id"])
        entry = {"id": element["id"], "lane": lane, "text": text, "owner": "claude" if written else "user"}
        if written:
            user_changed = [field for field, now in (("lane", lane), ("text", text)) if written[field] != now]
            if user_changed:
                entry["user_changed"] = user_changed
        notes.append(entry)

    links = sorted(
        {_link_key(e, live) for e in live.values() if e["type"] == "arrow"} - {(None, None)},
        key=lambda k: (str(k[0]), str(k[1])),
    )
    deleted = sorted(k for k in state["notes"] if k not in live)
    deleted_links = sorted(
        f"{link['from']}->{link['to']}" for link in state["links"] if (link["from"], link["to"]) not in set(links)
    )
    return {
        "scene": str(scene_path),
        "lanes": lanes,
        "notes": notes,
        "links": [{"from": a, "to": b} for a, b in links],
        "deleted_notes": deleted,
        "deleted_links": deleted_links,
    }


def _parse_spec(spec: dict[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    unknown = set(spec) - {"lanes", "notes", "links"}
    if unknown:
        raise ValueError(f"spec has unknown keys {sorted(unknown)}; it takes lanes, notes and links")
    lanes = [_fields(lane, "lane", ("id", "name")) for lane in spec.get("lanes", [])]
    notes = [_fields(note, "note", ("id", "lane", "kind", "text")) for note in spec.get("notes", [])]
    links = [_fields(link, "link", ("from", "to")) for link in spec.get("links", [])]
    ids = [x["id"] for x in lanes + notes]
    if dupes := sorted({i for i in ids if ids.count(i) > 1}):
        raise ValueError(f"ids must be unique across lanes and notes; repeated: {dupes}")
    for item in lanes + notes:
        if not _ID.match(item["id"]):
            raise ValueError(f"id {item['id']!r} must be 1-64 letters, digits, '-' or '_'")
    for note in notes:
        if note["kind"] not in KIND_COLOURS:
            raise ValueError(f"note {note['id']!r} has kind {note['kind']!r}; use one of {sorted(KIND_COLOURS)}")
        if not note["text"].strip():
            raise ValueError(f"note {note['id']!r} has no text")
    return lanes, notes, links


def _fields(item: Any, what: str, keys: tuple[str, ...]) -> dict[str, str]:
    if not isinstance(item, dict) or set(item) != set(keys) or not all(isinstance(item[k], str) for k in keys):
        raise ValueError(f"each {what} must be an object with exactly the string fields {list(keys)}: {item!r}")
    return item


def _reconcile(what: str, ident: str, field: str, current: Any, last: Any, wanted: Any) -> bool:
    """True when the field should be set to `wanted`; raises when that would overwrite an unadopted user edit.

    `last` is what Claude last wrote to the field, or None when Claude never wrote it.
    """
    if current == wanted:
        return False
    if last is not None and current == last:
        return True
    raise BoardConflict(
        f"{what} {ident!r}: the user changed {field} on the board to {current!r} (last written {last!r}); "
        f"adopt it in the spec or argue it with the user before writing {wanted!r}"
    )


def _load_scene(path: Path) -> Element:
    scene = json.loads(path.read_text(encoding="utf-8"))
    assert scene.get("type") == "excalidraw", f"{path} is not an Excalidraw scene (type={scene.get('type')!r})"
    return scene


def _load_state(path: Path) -> Element:
    if not path.is_file():
        return {"lanes": {}, "notes": {}, "links": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _empty_scene() -> Element:
    return {
        "type": "excalidraw",
        "version": 2,
        "source": "shipyard /sy:whiteboard",
        "elements": [],
        "appState": {"viewBackgroundColor": "#ffffff", "gridSize": None},
        "files": {},
    }


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


def _frame(ident: str, name: str, x: float, y: float) -> Element:
    frame = _base(ident, "frame", x, y, LANE_WIDTH, LANE_HEIGHT)
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


def _note(note: dict[str, str], lane: Element, live: Live) -> tuple[Element, Element]:
    wrapped = _wrap(note["text"])
    tw, th = _text_size(wrapped)
    height = max(80.0, th + 2 * TEXT_PADDING)
    x, y = lane["x"] + NOTE_INSET, _next_note_y(lane, live)
    background, stroke = KIND_COLOURS[note["kind"]]
    shape = _base(note["id"], "rectangle", x, y, NOTE_WIDTH, height)
    text_id = f"{note['id']}-text"
    shape.update(
        backgroundColor=background, strokeColor=stroke, roundness={"type": 3}, frameId=lane["id"],
        boundElements=[{"id": text_id, "type": "text"}],
    )
    text = _base(text_id, "text", x + (NOTE_WIDTH - tw) / 2, y + (height - th) / 2, tw, th)
    text.update(
        strokeColor=stroke, frameId=lane["id"], boundElements=None, text=wrapped, originalText=note["text"],
        fontSize=FONT_SIZE, fontFamily=1, textAlign="center", verticalAlign="middle", containerId=note["id"],
        lineHeight=LINE_HEIGHT, autoResize=True,
    )
    return shape, text


def _bound_text(shape: Element, live: Live) -> Element | None:
    for bound in shape.get("boundElements") or []:
        if bound.get("type") == "text" and bound["id"] in live:
            return live[bound["id"]]
    return None


def _set_text(text_el: Element, shape: Element, text: str) -> None:
    wrapped = _wrap(text)
    tw, th = _text_size(wrapped)
    shape["height"] = max(shape["height"], th + 2 * TEXT_PADDING)
    text_el.update(
        text=wrapped, originalText=text, width=tw, height=th,
        x=shape["x"] + (shape["width"] - tw) / 2, y=shape["y"] + (shape["height"] - th) / 2,
    )
    _touch(text_el)
    _touch(shape)


def _move_to_lane(shape: Element, text_el: Element, lane: Element, live: Live) -> None:
    dx = lane["x"] + NOTE_INSET - shape["x"]
    dy = _next_note_y(lane, live) - shape["y"]
    for element in (shape, text_el):
        element["x"] += dx
        element["y"] += dy
        element["frameId"] = lane["id"]
        _touch(element)
    _fit_lane(lane, shape)


def _next_note_y(lane: Element, live: Live) -> float:
    bottoms = [e["y"] + e["height"] for e in live.values() if e.get("frameId") == lane["id"] and e["type"] != "text"]
    return max(bottoms) + NOTE_GAP if bottoms else lane["y"] + FIRST_NOTE_OFFSET


def _fit_lane(lane: Element, shape: Element) -> None:
    bottom = shape["y"] + shape["height"] + NOTE_GAP
    if bottom > lane["y"] + lane["height"]:
        lane["height"] = bottom - lane["y"]
        _touch(lane)


def _next_lane_x(live: Live) -> float:
    rights = [e["x"] + e["width"] for e in live.values() if e["type"] == "frame"]
    return max(rights) + LANE_GAP if rights else 0


def _arrow(source: Element, target: Element) -> Element:
    """An arrow bound to both notes, leaving and entering through the sides that face each other."""
    (sx, sy), (tx, ty) = _facing_edges(source, target)
    ident = f"link-{source['id']}-{target['id']}"
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
