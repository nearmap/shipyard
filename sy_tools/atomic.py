"""The one durable-write primitive, shared by every module that rewrites a file readers depend on."""
from __future__ import annotations

import os
from pathlib import Path


def atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path` by renaming over the destination, so a reader never sees a half-written file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
