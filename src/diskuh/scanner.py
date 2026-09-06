"""Directory tree scanning.

`scan()` walks a directory tree and returns an aggregated `DirNode`. The walk
is iterative (an explicit stack, no Python-level recursion) so it can't blow
the recursion limit on very deep trees.

Symlinks are never followed — a symlinked file or directory is skipped
entirely, which avoids both double-counting and cycles (e.g. a symlink
pointing back at an ancestor).

Permission errors (an unreadable directory, or an unreadable individual
entry) are caught and recorded on the node's `errors` list rather than
aborting the whole scan, mirroring how `du` reports "cannot read directory"
lines but keeps going.

Depth limiting is deliberately *not* handled here: `scan()` always computes
full, accurate aggregate sizes for the whole tree. Which levels get *printed*
is a rendering concern, handled in `format.py`, exactly like `du
--max-depth` still sums everything but only prints some of it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Union


@dataclass
class FileNode:
    path: Path
    size: int
    mtime_ns: int
    is_dir: bool = False


@dataclass
class DirNode:
    path: Path
    size: int = 0
    is_dir: bool = True
    entry_count: int = 0
    mtime_ns: int = 0
    children: list["Entry"] = field(default_factory=list)
    file_count: int = 0
    errors: list[str] = field(default_factory=list)
    # False for a cache-collapsed stub (size/file_count are accurate, but
    # `children` isn't populated below this point) -- see diskuh.cache.
    # scanner.scan() always produces fully-expanded nodes, so True is the
    # right default for that use.
    expanded: bool = True


Entry = Union[DirNode, FileNode]


def disk_size(st: os.stat_result) -> int:
    """Actual on-disk size of a file, matching `du`'s default behavior
    (block-rounded, not the file's apparent/logical byte size). A small
    file still consumes at least one filesystem block, so this is what
    "how much space is this using" actually means -- and what `du -h`
    reports without `--apparent-size`. Falls back to `st_size` on
    platforms without `st_blocks` (e.g. Windows)."""
    blocks = getattr(st, "st_blocks", None)
    if blocks is None:
        return st.st_size
    return blocks * 512


def scan(root: Path) -> DirNode:
    """Walk `root` and return a fully aggregated `DirNode` tree."""
    root = Path(root).resolve()
    try:
        root_stat = root.lstat()
    except OSError as e:
        node = DirNode(path=root)
        node.errors.append(f"{root}: {e}")
        return node

    root_node = DirNode(path=root, mtime_ns=root_stat.st_mtime_ns)

    stack: list[DirNode] = [root_node]
    order: list[DirNode] = []

    while stack:
        node = stack.pop()
        order.append(node)
        _populate_children(node, stack)

    # Post-order aggregation: `order` is parent-before-children in the order
    # nodes were popped from the stack, so a plain reverse gives a valid
    # children-before-parents pass for summing.
    for node in reversed(order):
        for child in node.children:
            if isinstance(child, DirNode):
                node.size += child.size
                node.file_count += child.file_count
                node.errors.extend(child.errors)

    return root_node


def _populate_children(node: DirNode, stack: list[DirNode]) -> None:
    try:
        with os.scandir(node.path) as it:
            for entry in it:
                try:
                    if entry.is_symlink():
                        continue
                    st = entry.stat(follow_symlinks=False)
                except OSError as e:
                    node.errors.append(f"{entry.path}: {e}")
                    continue

                node.entry_count += 1
                if entry.is_dir(follow_symlinks=False):
                    child = DirNode(path=Path(entry.path), mtime_ns=st.st_mtime_ns)
                    node.children.append(child)
                    stack.append(child)
                else:
                    fsize = disk_size(st)
                    fnode = FileNode(
                        path=Path(entry.path), size=fsize, mtime_ns=st.st_mtime_ns
                    )
                    node.children.append(fnode)
                    node.size += fsize
                    node.file_count += 1
    except OSError as e:
        node.errors.append(f"{node.path}: {e}")


def iter_errors(root_node: DirNode) -> Iterator[str]:
    yield from root_node.errors
