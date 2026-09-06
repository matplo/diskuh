"""Human-readable + graphical rendering of a scanned tree.

Depth limiting lives here, not in the scanner/cache: sizes are always
computed for the full tree; `max_depth` only controls how many levels of
rows get printed (matching `du --max-depth` semantics).

Listing is hierarchical and directories-only, matching `du`/`du`-based
tools: each directory's own subdirectories are ranked and capped to
`--head`/`--tail` *independently*, then we recurse into the shown ones
before moving to the next sibling. An earlier version flattened the whole
tree (every depth, files included) into one list and ranked that globally
-- which let a handful of deeply-nested individual files crowd entire
directories out of the top N, and mixed unrelated branches together with
no sense of which subdirectory anything actually belonged to. A file's
bytes still count fully toward its parent's total; it just isn't given its
own row, the same way plain `du` never lists individual files either.
"""

from __future__ import annotations

from typing import Callable, Optional

from rich.console import Console
from rich.markup import escape as _escape_markup
from rich.table import Table
from rich.text import Text

from diskuh.scanner import DirNode, Entry

ExpandFn = Callable[[DirNode], None]

_UNITS = [("T", 1024**4), ("G", 1024**3), ("M", 1024**2), ("K", 1024)]

BAR_WIDTH = 30
BAR_MIN_WIDTH = 6
BAR_CHAR = "█"

# Default per-directory cap on how many entries to show (CLI's --head,
# TUI's 'n'), shared so both stay consistent. 0/None means unlimited.
DEFAULT_HEAD = 10


def human_size(num_bytes: int) -> str:
    """du -h style: single-letter suffix, no space, e.g. '4.2G', '128K'."""
    if num_bytes < 0:
        return f"-{human_size(-num_bytes)}"
    for suffix, factor in _UNITS:
        if num_bytes >= factor:
            value = num_bytes / factor
            return f"{value:.1f}{suffix}" if value < 10 else f"{value:.0f}{suffix}"
    return f"{num_bytes}B"


def _bar_color(size: int, max_size: int) -> str:
    ratio = size / max_size if max_size else 0
    if ratio > 0.66:
        return "red"
    if ratio > 0.33:
        return "yellow"
    return "green"


def render_bar(size: int, max_size: int, width: int = BAR_WIDTH) -> Text:
    filled = round((size / max_size) * width) if max_size > 0 else 0
    filled = max(0, min(width, filled))
    color = _bar_color(size, max_size)
    return Text(BAR_CHAR * filled, style=color) + Text(" " * (width - filled), style="dim")


def _pick_bar_width(console_width: int, size_col_width: int, min_name_room: int = 24) -> int:
    """Shrink the bar so the Name column always has room, instead of Rich
    silently truncating filenames with an ellipsis to make everything fit.
    `min_name_room` is a floor on how much space we insist on leaving for
    names before shrinking the bar further; below `BAR_MIN_WIDTH` the Name
    column's own fold-on-overflow takes over as the last resort."""
    overhead = size_col_width + 4  # inter-column padding, ~2 chars x 2 gaps
    available = console_width - overhead - min_name_room
    return max(BAR_MIN_WIDTH, min(BAR_WIDTH, available))


def _apply_head_tail(entries_sorted: list[Entry], head: int | None, tail: int | None) -> list[Entry]:
    """`entries_sorted` must already be sorted largest-first. `head` keeps
    the N largest; `tail` keeps the N smallest (still returned largest-first,
    matching the rest of the listing)."""
    if head is not None:
        return entries_sorted[:head]
    if tail is not None:
        return entries_sorted[-tail:] if tail < len(entries_sorted) else entries_sorted
    return entries_sorted


_TREE_BRANCH = "├── "
_TREE_LAST = "└── "
_TREE_BAR = "│   "
_TREE_BLANK = "    "


def collect_entries(
    node: DirNode,
    max_depth: int | None,
    expand: Optional[ExpandFn] = None,
    head: int | None = None,
    tail: int | None = None,
    include_files: bool = False,
    sort_key: Optional[Callable[[Entry], object]] = None,
) -> tuple[list[tuple[int, Entry, str]], int]:
    """Depth-first hierarchical listing (see module docstring): each
    directory's own children are ranked and capped to `head`/`tail`
    independently, then we recurse into the shown subdirectories (up to
    `max_depth`) before moving on to the next sibling. This is the part
    that can still trigger real scanning (`expand`), so callers showing a
    progress display during scanning should call this *inside* it, then
    render the result (via `render_tree`/`render_terse`'s `entries=`
    param) only after tearing the progress display down, so the two don't
    race for the terminal.

    `include_files` controls whether plain files are ranked/listed
    alongside subdirectories at each level (as non-recursable leaves) or
    excluded entirely (matching `du`, the CLI's default -- see module
    docstring). `sort_key` overrides the default largest-first ranking
    (e.g. for a name-sorted view); it still determines what "head"/"tail"
    keep.

    Returns `(rows, hidden_count)`: `rows` is the depth-first list of
    `(depth, entry, tree_prefix)` -- `tree_prefix` is a `├── `/`└── `
    connector plus one `│   `/`    ` continuation segment per ancestor
    level (like the `tree` command), so the nesting is traceable at a
    glance instead of relying on indentation alone. `hidden_count` is how
    many entries were left out this way, summed across every directory
    visited.
    """
    rows: list[tuple[int, Entry, str]] = []
    hidden = 0
    key = sort_key or (lambda c: -c.size)

    def walk(n: DirNode, depth: int, ancestors_last: list[bool]) -> None:
        nonlocal hidden
        children = list(n.children) if include_files else [
            c for c in n.children if isinstance(c, DirNode)
        ]
        if not children:
            return
        children_sorted = sorted(children, key=key)
        shown = _apply_head_tail(children_sorted, head, tail)
        hidden += len(children_sorted) - len(shown)
        last_index = len(shown) - 1
        for i, child in enumerate(shown):
            is_last = i == last_index
            prefix = "".join(_TREE_BLANK if b else _TREE_BAR for b in ancestors_last)
            prefix += _TREE_LAST if is_last else _TREE_BRANCH
            rows.append((depth, child, prefix))
            if isinstance(child, DirNode) and (max_depth is None or depth < max_depth - 1):
                if not child.expanded and expand is not None:
                    expand(child)
                walk(child, depth + 1, ancestors_last + [is_last])

    walk(node, 0, [])
    return rows, hidden


def render_tree(
    console: Console,
    node: DirNode,
    *,
    max_depth: int | None = None,
    expand: Optional[ExpandFn] = None,
    head: int | None = None,
    tail: int | None = None,
    entries: list[tuple[int, Entry, str]] | None = None,
    hidden: int = 0,
) -> None:
    """Pure rendering -- no scanning happens here once `entries` is given
    (see `collect_entries`). If `entries` is omitted, it's computed on the
    spot from `max_depth`/`expand`/`head`/`tail`, for callers that don't
    care about the scan/render timing split. `entries` is already in the
    desired hierarchical order and must not be re-sorted/re-capped here --
    that would destroy the structure `collect_entries` just built."""
    if entries is None:
        entries, hidden = collect_entries(node, max_depth, expand=expand, head=head, tail=tail)
    if not entries:
        console.print(f"{human_size(node.size)}\t{node.path}")
        return
    sizes = [entry.size for _, entry, _ in entries]
    max_size = max(sizes, default=1) or 1

    size_col_width = max((len(human_size(s)) for s in sizes), default=4)
    bar_width = _pick_bar_width(console.size.width, size_col_width)

    table = Table(box=None, show_header=False, padding=(0, 1))
    table.add_column(no_wrap=True)
    table.add_column(no_wrap=True)
    # Fold rather than truncate: a name is never silently cut short just
    # because the bar+size+indent left little room -- see _pick_bar_width.
    table.add_column(overflow="fold")
    for _depth, entry, prefix in entries:
        # Escape the real filename: it's untrusted as far as Rich markup
        # goes -- a name containing literal "[...]" (e.g. "archive[1].zip",
        # or something crafted like "[bold]notes.txt") would otherwise be
        # silently misinterpreted as style tags instead of shown as-is.
        safe_name = _escape_markup(str(entry.path.name or entry.path))
        name = f"[dim]{prefix}[/dim]{safe_name}"
        table.add_row(human_size(entry.size), render_bar(entry.size, max_size, width=bar_width), name)
    console.print(table)
    if hidden > 0:
        console.print(f"[dim]... {hidden} more subdirectories not shown[/dim]")
    console.print(f"[bold]{human_size(node.size)}[/bold]\ttotal\t{node.path}")


def render_terse(
    node: DirNode,
    *,
    max_depth: int | None = None,
    expand: Optional[ExpandFn] = None,
    head: int | None = None,
    tail: int | None = None,
    entries: list[tuple[int, Entry, str]] | None = None,
    hidden: int = 0,
) -> None:
    if entries is None:
        entries, hidden = collect_entries(node, max_depth, expand=expand, head=head, tail=tail)
    for _, entry, _prefix in entries:
        print(f"{human_size(entry.size)}\t{entry.path}")
    print(f"{human_size(node.size)}\t{node.path}")
