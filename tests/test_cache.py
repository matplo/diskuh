from __future__ import annotations

import os
import sys
import time

import pytest

from diskuh import cache

from conftest import disk_size_of, total_disk_size

# Use a size delta large enough that on-disk block counts are guaranteed to
# differ regardless of the filesystem's block size or small-file inlining
# quirks (e.g. APFS may store a handful of bytes without allocating a data
# extent at all).
SMALL = 10
LARGE = 5_000_000


def _connect(tmp_path):
    # Keep the cache DB outside the scanned tree (tree_factory scans
    # tmp_path itself), otherwise the DB's own files would be counted.
    db_dir = tmp_path.parent / (tmp_path.name + "-cachedb")
    conn = cache.connect(db_dir / "cache.sqlite3")
    cache.init_schema(conn)
    return conn


def test_cache_hit_skips_rescan_of_unchanged_subtree(tree_factory, tmp_path, monkeypatch):
    root = tree_factory({"a": {"b.txt": 100, "c": {"d.txt": 50}}, "e.txt": 10})
    conn = _connect(tmp_path)

    node1 = cache.get_or_refresh(conn, root, root)
    assert node1.size == total_disk_size(root)

    scandir_calls = []
    real_scandir = os.scandir

    def counting_scandir(path):
        scandir_calls.append(path)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", counting_scandir)

    node2 = cache.get_or_refresh(conn, root, root)
    assert node2.size == node1.size

    # Every directory still gets one shallow scandir (needed to verify its
    # own fingerprint), but the unchanged "a/c" subtree must NOT have been
    # recursed into a second time beyond that -- i.e. we shouldn't see more
    # scandir calls than there are directories in the tree.
    dir_count = sum(1 for _ in os.walk(root)) + 1  # root + subdirs
    assert len(scandir_calls) <= dir_count


def test_cache_detects_edit_in_direct_child(tree_factory, tmp_path):
    # A file directly inside the scanned directory is caught: it's part of
    # that directory's own immediate-children fingerprint.
    root = tree_factory({"a": {"f.txt": SMALL}})
    conn = _connect(tmp_path)

    node1 = cache.get_or_refresh(conn, root, root / "a")
    f = root / "a" / "f.txt"
    assert node1.size == disk_size_of(f)

    future = time.time() + 2
    f.write_bytes(b"x" * LARGE)
    os.utime(f, (future, future))
    size_after = disk_size_of(f)
    assert size_after != node1.size  # sanity: the edit actually changed disk usage

    node2 = cache.get_or_refresh(conn, root, root / "a")
    assert node2.size == size_after


def test_cache_documented_blind_spot_for_deep_in_place_edit(tree_factory, tmp_path):
    # Documented trade-off (see cache.py module docstring): editing a file
    # two levels below an otherwise-untouched ancestor does NOT change that
    # ancestor's own immediate-children fingerprint ("b"'s own mtime as an
    # entry of "a" doesn't change just because something inside "b"
    # changed), so a scan of the root alone misses it and keeps serving the
    # stale cached size.
    root = tree_factory({"a": {"b": {"deep.txt": SMALL}}})
    conn = _connect(tmp_path)

    node1 = cache.get_or_refresh(conn, root, root)
    deep_file = root / "a" / "b" / "deep.txt"
    size_before = node1.size
    assert size_before == disk_size_of(deep_file)

    future = time.time() + 2
    deep_file.write_bytes(b"x" * LARGE)
    os.utime(deep_file, (future, future))
    size_after = disk_size_of(deep_file)
    assert size_after != size_before  # sanity: the edit actually changed disk usage

    node2 = cache.get_or_refresh(conn, root, root)
    assert node2.size == size_before  # stale: the blind spot in action

    # The escape hatch: --no-cache/--rescan (force_rescan=True) always sees
    # the truth.
    node3 = cache.get_or_refresh(conn, root, root, force_rescan=True)
    assert node3.size == size_after

    # Directly re-visiting the changed file's own parent also sees it,
    # without needing a full-tree rescan.
    node4 = cache.get_or_refresh(conn, root, root / "a" / "b")
    assert node4.size == size_after


def test_trusted_stub_children_have_correct_size(tree_factory, tmp_path):
    # Regression: a cache-collapsed child DirNode stub must carry its own
    # correct aggregate size/file_count (from its own cached row), not the
    # DirNode default of 0 -- otherwise a trusted parent renders every
    # subdirectory as 0B until something happens to expand it.
    root = tree_factory({"a": {"b.txt": 100, "c.txt": 23}, "e.txt": 10})
    conn = _connect(tmp_path)
    expected_a_size = total_disk_size(root / "a")

    cache.get_or_refresh(conn, root, root)  # cold: populates the cache
    node = cache.get_or_refresh(conn, root, root)  # warm: root is trusted

    stub = next(c for c in node.children if c.path.name == "a")
    assert stub.expanded is False
    assert stub.size == expected_a_size
    assert stub.file_count == 2

    cache.expand(conn, root, stub)
    assert stub.expanded is True
    assert stub.size == expected_a_size
    assert stub.file_count == 2
    assert {c.path.name for c in stub.children} == {"b.txt", "c.txt"}


def test_evict_path_removes_cached_rows(tree_factory, tmp_path):
    root = tree_factory({"a": {"b.txt": 10}})
    conn = _connect(tmp_path)
    cache.get_or_refresh(conn, root, root)

    row = conn.execute("SELECT COUNT(*) AS n FROM directories").fetchone()
    assert row["n"] >= 1

    cache.evict_path(conn, root, root / "a")
    remaining = conn.execute(
        "SELECT path FROM directories WHERE path LIKE ?", (f"%{os.sep}a",)
    ).fetchall()
    assert remaining == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission checks")
def test_scan_error_persists_across_cache_hits(tree_factory, tmp_path):
    # Regression: a directory that fails to scan (e.g. permission denied)
    # never got a row stored at all, so the moment its *parent* became a
    # trusted cache hit, the error silently stopped being reported even
    # though the underlying problem hadn't gone away.
    root = tree_factory({"locked": {"secret.txt": SMALL}, "open.txt": SMALL})
    locked = root / "locked"
    locked.chmod(0o000)
    conn = _connect(tmp_path)
    try:
        node1 = cache.get_or_refresh(conn, root, root)
        assert any("locked" in e for e in node1.errors)

        # Nothing on disk changed, so root is now a trusted cache hit --
        # but the error must still resurface.
        node2 = cache.get_or_refresh(conn, root, root)
        assert any("locked" in e for e in node2.errors)

        stub = next(c for c in node2.children if c.path.name == "locked")
        assert stub.errors  # the stub itself carries the error too
    finally:
        locked.chmod(0o755)


def test_is_cached(tree_factory, tmp_path):
    root = tree_factory({"a": {"b.txt": SMALL}})
    conn = _connect(tmp_path)

    # Never scanned yet: not a quick op.
    assert cache.is_cached(conn, root, root / "a") is False

    cache.get_or_refresh(conn, root, root / "a")
    assert cache.is_cached(conn, root, root / "a") is True

    # Editing a direct child invalidates it again.
    f = root / "a" / "b.txt"
    future = time.time() + 2
    f.write_bytes(b"x" * LARGE)
    os.utime(f, (future, future))
    assert cache.is_cached(conn, root, root / "a") is False

    # A nonexistent path is reported as "cached" (nothing expensive would
    # happen if you tried) rather than raising.
    assert cache.is_cached(conn, root, root / "does-not-exist") is True
