from __future__ import annotations

import os
from pathlib import Path

import pytest


def make_tree(base: Path, spec: dict) -> None:
    """Build a nested file/dir tree under `base` from a nested dict spec.

    A leaf value that's an int is a file of that many bytes; a dict value
    is a subdirectory. Example:
        {"a": {"b.txt": 100, "c": {"d.txt": 50}}, "e.txt": 10}
    """
    for name, value in spec.items():
        path = base / name
        if isinstance(value, dict):
            path.mkdir(parents=True, exist_ok=True)
            make_tree(path, value)
        else:
            path.write_bytes(b"x" * int(value))


@pytest.fixture
def tree_factory(tmp_path):
    def _factory(spec: dict) -> Path:
        make_tree(tmp_path, spec)
        return tmp_path

    return _factory


def disk_size_of(path: Path) -> int:
    """Real on-disk size of one file, via the same block-rounding logic the
    app uses (`du`'s default, not apparent/logical byte size) -- block size
    is filesystem-dependent, so tests compute expected totals this way
    rather than assuming raw byte counts."""
    from diskuh.scanner import disk_size

    return disk_size(path.stat())


def total_disk_size(root: Path) -> int:
    """Sum of disk_size_of() for every regular file under `root`."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            total += disk_size_of(Path(dirpath) / name)
    return total
