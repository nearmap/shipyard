#!/usr/bin/env bash
# Builds a two-commit repo: base adds an uploader, head batches it through a chunking helper that drops the tail.
set -euo pipefail
g() { git -c user.name=fixture -c user.email=fixture@example.invalid "$@"; }
git init -q -b main .
mkdir -p batch
cat > batch/upload.py <<'PY'
"""Send records to a sink one at a time."""

from collections.abc import Callable, Sequence


def upload(records: Sequence[dict], send: Callable[[list[dict]], None]) -> int:
    """Send every record and return how many were sent."""
    for record in records:
        send([record])
    return len(records)
PY
g add -A && g commit -q -m "Add record uploader"
cat > batch/chunks.py <<'PY'
"""Split a sequence into fixed-size batches."""

from collections.abc import Sequence


def chunked(items: Sequence, size: int) -> list[list]:
    """Return consecutive batches of at most `size` items, covering every item in order."""
    if size < 1:
        raise ValueError("size must be positive")
    return [list(items[i : i + size]) for i in range(0, len(items) - size + 1, size)]
PY
cat > batch/upload.py <<'PY'
"""Send records to a sink in batches."""

from collections.abc import Callable, Sequence

from .chunks import chunked

BATCH_SIZE = 50


def upload(records: Sequence[dict], send: Callable[[list[dict]], None]) -> int:
    """Send every record in batches of BATCH_SIZE and return how many were sent."""
    sent = 0
    for batch in chunked(records, BATCH_SIZE):
        send(batch)
        sent += len(batch)
    return sent
PY
g add -A && g commit -q -m "Batch uploads to cut request count"
git diff HEAD~1 HEAD > CHANGE.diff
