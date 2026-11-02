# The design board

The board is where the design is drawn, once the user opens it. The record stays in `notes.md`; the board is the picture of the design part of it.

## Sections, notes and links

`board_write` takes the whole board as Claude wants it:

```json
{
  "sections": [{"id": "flow-a", "name": "Data flow: A"}],
  "notes": [
    {"id": "ingest", "section": "flow-a", "kind": "step", "text": "Ingest\n1k files/day", "at": [0, 0]},
    {"id": "store", "section": "flow-a", "kind": "store", "text": "Object store", "at": [1, 0]}
  ],
  "links": [{"from": "ingest", "to": "store", "label": "2 GB/day"}]
}
```

- **Sections** are frames, stacked top to bottom in spec order. One per view the user picked at Readiness: Data model, Data flow, System design, Algorithm, or one per approach and view ("Data flow: A").
- **Notes** sit on a grid inside their section. `at` is `[column, row]`, from `[0, 0]` at the top left, unique within the section. Columns are a fixed width apart; each row is as tall as its tallest note.
- **Kinds** set the colour: `component`, `store`, `data`, `external`, `step`, `note`. A `data` note is a schema: a title line, then one field per line, aligned top-left. A note the user recolours keeps their colour.
- **Links** are arrows between two notes, in or across sections, with an optional `label`: a verb ("reads"), or the rough size on a data-flow arrow ("2 GB/day", "50 req/s"). Arrows are straight, so put linked notes in neighbouring cells; an arrow between two notes with a third between them crosses it. A user's unlabelled arrow is adopted in two writes: first with label `""`, then with its label.

Keep notes short: a title line, then at most three short lines (a `data` note may list more fields). A note's id never changes once written, because the id is how a user's move is traced back.

The spec lists every section and note Claude wants as it stands, rebuilt from `notes.md` each write. A spec that drops a note leaves it on the board untouched; a write never deletes anything. A link label, once written, can be changed but not cleared. Restoring a note or link the user deleted takes two writes: drop it from the spec, then add it again in the next one (a note whose text alone was cleared needs a note of Claude's own under a new id).

## Layout

Every write lays the whole board out: sections in spec order, each note at its cell, and each section sized to its grid plus one spare column and one spare row, so the user always has empty cells to drag into. Free text and other shapes in a section move with it. Arrows between notes are redrawn, with their labels centred on them.

- **A drag to another cell** is read back by `board_read` as the note's new `at` (or `section` too, across sections), marked `user_changed`. Adopt it in the next spec, or argue it.
- **A nudge inside a cell** is not a move: the next write puts the note back exactly on its cell.
- **A drag onto an occupied cell** moves the dragged note to the nearest free cell; the note already there keeps its cell.
- A spec cell that holds a note the spec does not name is refused; pick another cell, or adopt that note first.

## The scratch area

Everything outside every section, to the right of the sections, is the scratch area. A write never moves anything there. `board_read` lists scratch notes with `section` and `at` null. Ask where each new scratch note belongs (Scratch note choice point). To adopt one, first write it as it is, with `section` and `at` null; then give it a section and cell in the next write. Free text, in scratch or in a section, reads back with `kind: "text"` and `at` null, and is never adopted: to keep it, add a note of Claude's own with its text under a new id; the user may then delete the free text.

## Opening the board

Tell the user once, at the first write, with the path `board_write` returned:

> The board is `<path>/board.excalidraw`. Open it in VS Code with the Excalidraw extension (`pomdtr.excalidraw-editor`), or on excalidraw.com in a Chromium browser (Open, then Save writes back to the file). Neither reloads the file, so close and reopen the board after each of my turns. Drag notes to other cells or sections, edit their text or an arrow's label, add notes or arrows, and save; I read your changes at the start of every turn. Put notes you are not sure about to the right of the sections; I never move those. To zoom: Ctrl/Cmd + scroll or pinch, Shift+1 to fit the whole board, Shift+2 to zoom to the selected notes.

The extension's 3.9.x releases bundle Excalidraw 0.18.1. On excalidraw.com, a browser other than Chromium has no File System Access API, so Save downloads a copy instead of writing the file back. excalidraw.com keeps the scene in the browser unless the user starts a live collaboration or a share link; say so when the problem is sensitive.

## A stale save

A save from a tab not reopened since Claude's last write is caught by `board_read` as a stale save: the copy is kept aside, the last write is restored, and the copy's edits come back in `stale_save.changes`, cell moves and relabelled links included. Answer them per the skill. The small grey "Board revision N" line above the sections is how a stale save is detected. Leave it on the board. A saved file carries no tab identity, so the board assumes one editor tab at a time: two stale tabs at one revision share a base.
