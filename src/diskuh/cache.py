"""SQLite-backed scan cache.

Design
------
Scan results are cached per-directory in a SQLite database at
``~/.diskuh/cache.sqlite3``. Only directories are cached (not individual
files) -- files are cheap to `stat` once their parent directory is already
being visited, so caching them separately would bloat the database for no
benefit.

Staleness check ("is this directory's cached size still good?") uses a
**metadata fingerprint**: a hash of the immediate children's
``(name, kind, size, mtime_ns)``, computed from a single ``os.scandir()``
pass over that directory (never its full subtree). If a directory's own
``(mtime_ns, entry_count, fingerprint)`` still match what's cached, its
*entire* cached subtree aggregate is trusted and **we do not recurse into
it at all** -- this is the actual speedup: an unchanged directory costs one
`stat` + one shallow `scandir`, regardless of how large or deep the subtree
beneath it is. A mismatch means something in that directory changed
(entry added/removed/renamed/resized), so it's rescanned -- but each of
*its* children still gets an independent trust check, so one changed
directory doesn't invalidate unrelated subtrees elsewhere in the tree.

Known limitation (accepted trade-off, same class of heuristic used by tools
like ``ncdu``/``duc``/``make``): editing a file's *contents* in place (same
name, changed size/mtime) does not change its *parent* directory's own
mtime -- only the file's own entry does. Our fingerprint *does* catch this
for the file's direct parent (the file's own size/mtime is part of that
directory's immediate-children hash). But if some ancestor further up is
trusted and we stop recursing there, we never reach down far enough to see
it. So an in-place edit below an otherwise-untouched ancestor can go
unnoticed until something else in that ancestor's chain changes. Use
``--no-cache``/``--rescan`` for a guaranteed-fresh, guaranteed-accurate
scan when that matters.

Note on an earlier draft of this algorithm: a version that always recurses
into every child directory regardless of trust (to "be extra safe") was
considered, but it means every directory gets a full `scandir` on every
single scan either way -- identical total cost to an uncached scan, so it
provides no actual caching speedup at all. The short-circuit implemented
here (skip recursion entirely on a trust hit) is what makes the cache
actually pay for itself on repeat scans, at the cost of the documented
blind spot above.

Traversal is iterative (an explicit stack, post-order aggregation pass),
mirroring `scanner.scan`, so it doesn't rely on Python-level recursion and
can't blow the recursion limit on very deep trees.

A directory that can't be scanned at all (e.g. permission denied) still
gets a row stored, carrying its error message in the `error` column --
without this, the error would only ever be reported on the very first,
uncached scan: the moment its *parent* becomes a trusted cache hit, the
parent stops re-visiting it and the problem would silently disappear from
future reports even though it's still just as inaccessible. A trusted
parent checks its immediate children's stored `error` and re-surfaces it.
This only bridges one level per trusted hop, though: an error several
levels below multiple nested trusted ancestors may not resurface until
something along that chain changes (or `--no-cache` is used) -- the same
class of trade-off as the staleness blind spot above.

Writes within a single `get_or_refresh()` call are batched into one
`conn.commit()` at the end (see `_store_row`'s `commit` parameter), not one
per directory. An earlier version committed after every single row, which
turned out to be the dominant cost of a cold scan on a large tree -- a
commit is an fsync-class operation, so scanning N directories did N of them,
independent of and often exceeding the actual filesystem-walk cost. Combined
with `PRAGMA synchronous = NORMAL` (safe under WAL, see `connect()`), this is
what makes a cold `diskuh` scan competitive with plain `du` instead of
dramatically slower than it.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path
from typing import Callable

from diskuh.scanner import DirNode, Entry, FileNode, disk_size

DB_DIR = Path.home() / ".diskuh"
DB_PATH = DB_DIR / "cache.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_roots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    last_scanned REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS directories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_id INTEGER NOT NULL REFERENCES scan_roots(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    parent_path TEXT,
    dir_mtime_ns INTEGER NOT NULL,
    entry_count INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    agg_size INTEGER NOT NULL,
    agg_file_count INTEGER NOT NULL,
    last_scanned REAL NOT NULL,
    -- Set when this directory itself couldn't be scanned (e.g. permission
    -- denied). Without this, a directory that fails to scan never gets a
    -- row stored at all, so the moment its *parent* becomes a cache hit,
    -- the error silently stops being reported even though the underlying
    -- problem hasn't gone away. NULL means no error.
    error TEXT,
    UNIQUE(root_id, path)
);
CREATE INDEX IF NOT EXISTS idx_directories_root_path ON directories(root_id, path);
CREATE INDEX IF NOT EXISTS idx_directories_parent ON directories(root_id, parent_path);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    if db_path is None:
        db_path = DB_PATH  # read at call time so tests can monkeypatch it
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: the TUI runs scans on a worker thread (to
    # keep the UI responsive) while the connection is created on the main
    # thread. We never use it from two threads *concurrently* -- scans are
    # exclusive/sequenced -- so a single connection without the default's
    # same-thread restriction is safe here.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL")
    # NORMAL only fsyncs at checkpoints instead of FULL's fsync-on-every-
    # commit -- SQLite's own docs call this safe specifically in combination
    # with WAL (set just above): an app crash still can't corrupt the
    # database, and the worst an OS crash/power loss can do is lose the most
    # recent commit. An acceptable trade for a disposable, rebuildable local
    # cache, and the other half of what makes batching commits (see
    # get_or_refresh) actually pay off.
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """`CREATE TABLE IF NOT EXISTS` doesn't retrofit new columns onto a
    database file created by an older version of diskuh, so do that by
    hand for anything added after the initial schema."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(directories)")}
    if "error" not in cols:
        conn.execute("ALTER TABLE directories ADD COLUMN error TEXT")
        conn.commit()


def compute_fingerprint(children_meta: list[tuple[str, str, int, int]]) -> str:
    """Hash the sorted (name, kind, size, mtime_ns) of a directory's
    immediate children. `kind` is 'f' for file, 'd' for dir."""
    h = hashlib.blake2b(digest_size=16)
    for name, kind, size, mtime_ns in sorted(children_meta):
        h.update(name.encode("utf-8", "surrogateescape"))
        h.update(kind.encode("ascii"))
        h.update(int(size).to_bytes(8, "little", signed=False))
        h.update(int(mtime_ns).to_bytes(8, "little", signed=True))
    return h.hexdigest()


def _get_root_id(conn: sqlite3.Connection, root_path: Path) -> int:
    now = time.time()
    row = conn.execute(
        "SELECT id FROM scan_roots WHERE path = ?", (str(root_path),)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE scan_roots SET last_scanned = ? WHERE id = ?", (now, row["id"])
        )
        conn.commit()
        return row["id"]
    cur = conn.execute(
        "INSERT INTO scan_roots (path, last_scanned) VALUES (?, ?)",
        (str(root_path), now),
    )
    conn.commit()
    return cur.lastrowid


def _fetch_row(conn: sqlite3.Connection, root_id: int, path: Path) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM directories WHERE root_id = ? AND path = ?",
        (root_id, str(path)),
    ).fetchone()


def _store_row(
    conn: sqlite3.Connection,
    root_id: int,
    path: Path,
    dir_mtime_ns: int,
    entry_count: int,
    fingerprint: str,
    agg_size: int,
    agg_file_count: int,
    error: str | None = None,
    commit: bool = True,
) -> None:
    now = time.time()
    conn.execute(
        """
        INSERT INTO directories
            (root_id, path, parent_path, dir_mtime_ns, entry_count,
             fingerprint, agg_size, agg_file_count, last_scanned, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(root_id, path) DO UPDATE SET
            parent_path=excluded.parent_path,
            dir_mtime_ns=excluded.dir_mtime_ns,
            entry_count=excluded.entry_count,
            fingerprint=excluded.fingerprint,
            agg_size=excluded.agg_size,
            agg_file_count=excluded.agg_file_count,
            last_scanned=excluded.last_scanned,
            error=excluded.error
        """,
        (
            root_id,
            str(path),
            str(path.parent),
            dir_mtime_ns,
            entry_count,
            fingerprint,
            agg_size,
            agg_file_count,
            now,
            error,
        ),
    )
    if commit:
        conn.commit()


def _check_dir(
    conn: sqlite3.Connection, root_id: int, path: Path, *, force_rescan: bool
):
    """Do the one-directory work needed to decide cache trust: stat the
    directory, look up its cached row, and do a single shallow `scandir` to
    compute a fresh fingerprint. No recursion, no writes -- shared by
    `get_or_refresh`'s traversal and the standalone `is_cached` peek."""
    st = path.lstat()  # may raise OSError; caller handles it
    row = None if force_rescan else _fetch_row(conn, root_id, path)

    children_meta: list[tuple[str, str, int, int]] = []
    live_entries: list[tuple[bool, os.stat_result, Path]] = []
    errors: list[str] = []
    with os.scandir(path) as it:  # may raise OSError; caller handles it
        for entry in it:
            try:
                if entry.is_symlink():
                    continue
                cst = entry.stat(follow_symlinks=False)
            except OSError as e:
                errors.append(f"{entry.path}: {e}")
                continue
            is_dir = entry.is_dir(follow_symlinks=False)
            kind = "d" if is_dir else "f"
            children_meta.append((entry.name, kind, cst.st_size, cst.st_mtime_ns))
            live_entries.append((is_dir, cst, Path(entry.path)))

    fingerprint = compute_fingerprint(children_meta)
    entry_count = len(children_meta)
    trusted = (
        row is not None
        and row["fingerprint"] == fingerprint
        and row["dir_mtime_ns"] == st.st_mtime_ns
        and row["entry_count"] == entry_count
    )
    return st, row, live_entries, fingerprint, entry_count, trusted, errors


def is_cached(conn: sqlite3.Connection, root_path: Path, path: Path) -> bool:
    """Cheap check: would `get_or_refresh(path)` hit the cache (fast) or
    need a real (re)scan (potentially expensive)? Does one shallow
    `scandir` of `path` itself -- same cost as the real trust check -- but
    never recurses and never writes to the cache. Returns True if `path`
    doesn't exist or can't be read (nothing to scan, so nothing "expensive"
    would happen)."""
    root_path = Path(root_path).resolve()
    path = Path(path).resolve()
    root_id = _get_root_id(conn, root_path)
    try:
        *_, trusted, _errors = _check_dir(conn, root_id, path, force_rescan=False)
    except OSError:
        return True
    return trusted


def get_or_refresh(
    conn: sqlite3.Connection,
    root_path: Path,
    path: Path,
    *,
    force_rescan: bool = False,
    on_progress: Callable[[Path], None] | None = None,
) -> DirNode:
    """Return an aggregated `DirNode` for `path`, using the cache when safe.

    Directories whose fingerprint still matches the cache are returned with
    their cached aggregate size/file_count and are NOT recursed into further
    (their immediate children are still listed, as lightweight stubs for
    subdirectories, so callers can display or lazily expand one more level).
    Directories that changed (or aren't cached yet, or `force_rescan=True`)
    are fully rescanned, recursing into each child directory independently.

    `on_progress`, if given, is called with the path of each directory as
    it's visited (whether trusted or rescanned) -- useful for showing live
    progress on a scan that may touch a great many directories.
    """
    root_path = Path(root_path).resolve()
    path = Path(path).resolve()
    root_id = _get_root_id(conn, root_path)

    root_node = DirNode(path=path)
    stack: list[DirNode] = [root_node]
    # Nodes that needed a (re)scan, in pop order (parent before its own
    # children, since children are pushed after their parent is popped).
    # Reversing gives child-before-parent, which is what post-order
    # aggregation needs -- same trick as scanner.scan.
    rescanned_order: list[DirNode] = []
    fingerprints: dict[str, str] = {}

    while stack:
        node = stack.pop()
        if on_progress is not None:
            on_progress(node.path)
        try:
            st, row, live_entries, fingerprint, entry_count, trusted, errors = _check_dir(
                conn, root_id, node.path, force_rescan=force_rescan
            )
        except OSError as e:
            err_msg = f"{node.path}: {e}"
            node.errors.append(err_msg)
            # Persist this, not just report it in-memory this one time --
            # otherwise the moment this node's *parent* becomes a cache
            # hit, the error silently stops being reported even though
            # the underlying problem (e.g. permission denied) hasn't
            # gone away. Best-effort mtime: if even lstat() failed, 0 is
            # fine -- this row exists purely to carry the error forward.
            try:
                mtime_ns = node.path.lstat().st_mtime_ns
            except OSError:
                mtime_ns = 0
            node.mtime_ns = mtime_ns
            _store_row(
                conn, root_id, node.path, mtime_ns, 0, "", 0, 0, error=err_msg, commit=False
            )
            continue
        node.mtime_ns = st.st_mtime_ns
        node.entry_count = entry_count
        node.errors.extend(errors)

        if trusted:
            node.size = row["agg_size"]
            node.file_count = row["agg_file_count"]
            for is_dir, cst, full_path in live_entries:
                if is_dir:
                    # Seed the stub's size/file_count from ITS OWN cached
                    # row (a cheap indexed lookup) so it displays correctly
                    # even if a caller never expands it further -- e.g.
                    # `--depth 1` only ever sees these stubs.
                    child_row = _fetch_row(conn, root_id, full_path)
                    stub = DirNode(path=full_path, mtime_ns=cst.st_mtime_ns, expanded=False)
                    if child_row is not None:
                        stub.size = child_row["agg_size"]
                        stub.file_count = child_row["agg_file_count"]
                        if child_row["error"]:
                            # Surface a previously-recorded error even
                            # though we're not re-descending into this
                            # child (see the "error" column comment in
                            # SCHEMA). Known limitation: this only
                            # reaches one level up per trusted hop, so an
                            # error many levels below several *nested*
                            # trusted ancestors may not resurface until
                            # something along that chain changes, or
                            # --no-cache is used.
                            stub.errors.append(child_row["error"])
                            node.errors.append(child_row["error"])
                    node.children.append(stub)
                else:
                    node.children.append(
                        FileNode(path=full_path, size=disk_size(cst), mtime_ns=cst.st_mtime_ns)
                    )
            continue  # cache win: nothing below this directory is touched

        fingerprints[str(node.path)] = fingerprint
        rescanned_order.append(node)
        for is_dir, cst, full_path in live_entries:
            if is_dir:
                child = DirNode(path=full_path, mtime_ns=cst.st_mtime_ns)
                node.children.append(child)
                stack.append(child)
            else:
                fsize = disk_size(cst)
                fnode = FileNode(path=full_path, size=fsize, mtime_ns=cst.st_mtime_ns)
                node.children.append(fnode)
                node.size += fsize
                node.file_count += 1

    for node in reversed(rescanned_order):
        for child in node.children:
            if isinstance(child, DirNode):
                node.size += child.size
                node.file_count += child.file_count
                node.errors.extend(child.errors)
        _store_row(
            conn,
            root_id,
            node.path,
            node.mtime_ns,
            node.entry_count,
            fingerprints[str(node.path)],
            node.size,
            node.file_count,
            commit=False,
        )

    # Every _store_row() call above (and the OSError branch further up) ran
    # with commit=False -- this single commit covers all of them in one
    # fsync instead of one per directory, which used to dominate cold-scan
    # time on large trees (commit-per-row was the actual bottleneck behind
    # "diskuh is way slower than du" -- see the module docstring/cache
    # design notes). Unconditional, so it's a harmless no-op if this call
    # touched nothing at all (e.g. every directory was already trusted).
    conn.commit()

    return root_node


def expand(
    conn: sqlite3.Connection,
    root_path: Path,
    node: DirNode,
    *,
    force_rescan: bool = False,
    on_progress: Callable[[Path], None] | None = None,
) -> None:
    """Replace a cache-collapsed stub `node` (see `DirNode.expanded`) in
    place with its real children, one level down. A renderer walks the tree
    and calls this on any stub it needs to descend past (e.g. because
    `--depth` asks for rows below it); nodes it never descends into stay
    collapsed and cost nothing further."""
    if node.expanded:
        return
    fresh = get_or_refresh(
        conn, root_path, node.path, force_rescan=force_rescan, on_progress=on_progress
    )
    node.children = fresh.children
    node.entry_count = fresh.entry_count
    node.errors = fresh.errors
    node.size = fresh.size
    node.file_count = fresh.file_count
    node.mtime_ns = fresh.mtime_ns
    node.expanded = True


def evict_path(conn: sqlite3.Connection, root_path: Path, path: Path) -> None:
    """Drop cached rows for `path` (and everything under it) plus its
    parent, so the parent's next fingerprint check notices the missing
    entry. Call this after deleting a file/directory from the TUI."""
    root_path = Path(root_path).resolve()
    path = Path(path).resolve()
    root_row = conn.execute(
        "SELECT id FROM scan_roots WHERE path = ?", (str(root_path),)
    ).fetchone()
    if root_row is None:
        return
    root_id = root_row["id"]
    conn.execute(
        "DELETE FROM directories WHERE root_id = ? AND (path = ? OR path LIKE ? || '/%')",
        (root_id, str(path), str(path)),
    )
    conn.execute(
        "DELETE FROM directories WHERE root_id = ? AND path = ?",
        (root_id, str(path.parent)),
    )
    conn.commit()
