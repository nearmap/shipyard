"""The shared durable-write primitive every rewriting module depends on."""
from __future__ import annotations

import os
from typing import Any

import pytest

from sy_tools.atomic import atomic_write


def test_atomic_write_replaces_the_destination_with_the_new_text(tmp_path):
    path = tmp_path / "state.yaml"
    path.write_text("old", encoding="utf-8")
    atomic_write(path, "new")
    assert path.read_text(encoding="utf-8") == "new"
    assert not path.with_suffix(path.suffix + ".tmp").exists(), "the temp file must not survive a successful write"


def test_atomic_write_cleans_up_the_temp_file_when_the_replace_fails(tmp_path, monkeypatch):
    path = tmp_path / "state.yaml"
    path.write_text("old", encoding="utf-8")

    def crash(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("interrupted before the rename")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError, match="interrupted"):
        atomic_write(path, "new")
    assert path.read_text(encoding="utf-8") == "old", "a failed replace must leave the destination untouched"
    tmp = path.with_suffix(path.suffix + ".tmp")
    assert not tmp.exists(), "a failed replace must not leave the temp file behind"
