from __future__ import annotations

import os
import sys

import pytest

from diskuh.scanner import DirNode, scan

from conftest import disk_size_of, total_disk_size


def test_aggregate_size(tree_factory):
    root = tree_factory({"a": {"b.txt": 100, "c": {"d.txt": 50}}, "e.txt": 10})
    node = scan(root)
    assert node.size == total_disk_size(root)
    assert node.file_count == 3
    assert node.errors == []


def test_empty_dir(tree_factory):
    root = tree_factory({"empty": {}})
    node = scan(root)
    assert node.size == 0
    assert node.file_count == 0


def test_symlinks_not_followed(tree_factory):
    root = tree_factory({"real": {"f.txt": 100}})
    link = root / "link_to_real"
    link.symlink_to(root / "real", target_is_directory=True)
    node = scan(root)
    # Only the real subtree should be counted, not doubled via the symlink,
    # and the symlink itself shouldn't be traversed into.
    assert node.size == disk_size_of(root / "real" / "f.txt")
    assert node.file_count == 1


def test_symlink_cycle_does_not_hang(tree_factory):
    root = tree_factory({"a": {}})
    cycle = root / "a" / "loop"
    cycle.symlink_to(root, target_is_directory=True)
    node = scan(root)  # must return, not infinite-loop
    assert node.size == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission checks")
def test_permission_denied_recorded_not_raised(tree_factory):
    root = tree_factory({"locked": {"secret.txt": 10}, "open.txt": 5})
    locked = root / "locked"
    locked.chmod(0o000)
    try:
        node = scan(root)
    finally:
        locked.chmod(0o755)
    assert node.size == disk_size_of(root / "open.txt")  # only open.txt counted
    assert any("locked" in e or "Permission" in e for e in node.errors)
