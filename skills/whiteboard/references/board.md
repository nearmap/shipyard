# The design board

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

- **Sections** are frames, stacked top to bottom in spec order.
- **Notes** sit on a grid inside their section. `at` is `[column, row]`, from `[0, 0]` at the top left, unique within the section. Columns are a fixed width apart; each row is as tall as its tallest note.
- **Kinds** set the colour: `component`, `store`, `data`, `external`, `step`, `note`. A `data` note is a schema: a title line, then one field per line, aligned top-left.
- **Links** are arrows between two notes, in or across sections, with an optional `label`: a verb ("reads"), or the rough size on a data-flow arrow ("2 GB/day", "50 req/s"). Arrows are straight, so put linked notes in neighbouring cells; an arrow between two notes with a third between them crosses it.

Keep notes short: a title line, then at most three short lines (a `data` note may list more fields). A note's id never changes once written, because the id is how a user's move is traced back.

The spec lists every section and note Claude wants as it stands, rebuilt from `notes.md` each write.

## Layout

Every write lays the whole board out: sections in spec order, each note at its cell, and each section sized to its grid plus one spare column and one spare row, so the user always has empty cells to drag into. Free text and other shapes in a section move with it. Arrows touching a note in a section are redrawn, with their labels centred on them.

- **A drag to another cell** is read back by `board_read` as the note's new `at` (or `section` too, across sections), marked `user_changed`. Adopt it in the next spec, or argue it.

## The scratch area

Everything outside every section, to the right of the sections, is the scratch area. `board_read` lists scratch notes with `section` and `at` null. To adopt one, first write it as it is, with `section` and `at` null; then give it a section and cell in the next write.

## Opening the board

Tell the user once, at the first write, with the `board_url` and `forward` from `page_serve` and the path `board_write` returned:

> On a remote machine, forward the port first, the same way as for the page (one forward covers both; use `localhost` if `127.0.0.1` does not open). Then open the board at `<board_url>` in any browser. It saves by itself and loads my changes without a reload. It needs internet for Excalidraw's code. If you have unsaved edits when I change the board, it asks before it replaces them.
>
> Or open `<path>/board.excalidraw` in VS Code with the Excalidraw extension (`pomdtr.excalidraw-editor`), or on excalidraw.com in a Chromium browser (Open, then Save writes back to the file). These two do not reload the file, so close and reopen the board after each of my turns.
>
> Drag notes to other cells or sections, edit their text or an arrow's label, add notes or arrows, and save; I read your changes at the start of every turn. Put notes you are not sure about to the right of the sections; I never move those. To zoom: Ctrl/Cmd + scroll or pinch, Shift+1 to fit the whole board, Shift+2 to zoom to the selected notes.

The browser editor runs pinned Excalidraw code from esm.sh with the token, so that code can read and save any board in this repo's whiteboards folder.

The board needs Excalidraw 0.18 or later; older editors move a section's notes differently on a resize, and the board is not tested on them.

Known limit: resizing a section's left or top edge and then, before the next write, resizing its opposite edge or dragging the section reads as a drag, and framed free text and shapes shift by as far as that edge moved: under half a cell in a section with notes unless the resize cropped them, by any amount in a section without notes.

On excalidraw.com, a browser other than Chromium has no File System Access API, so Save downloads a copy instead of writing the file back. excalidraw.com keeps the scene in the browser unless the user starts a live collaboration or a share link; say so when the problem is sensitive.

## A stale save

The small grey "Board revision N" line above the sections is how a stale save is detected. Leave it on the board.
