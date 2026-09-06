"""Textual TUI: browse a directory tree with size + bar per entry, delete
entries with confirmation. Both `diskuh --tui` and the `diskuh-tui` console
script call `run_tui()` -- there is exactly one TUI implementation."""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import Optional

from rich.markup import escape as _escape_markup
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Static

# How many levels of subdirectories to show at once, like `--depth` on the
# CLI (see format.collect_entries) -- not yet user-adjustable from within
# the TUI. Kept finite rather than unlimited so a single navigation can't
# balloon into rendering an enormous number of rows for a very deep tree;
# in practice showing more levels costs little extra scanning; the
# directory being displayed was already fully resolved (to compute its own
# accurate total) by the time it's shown, so expanding a few more of its
# already-cached descendants for display is normally close to free.
_DEFAULT_TREE_DEPTH = 3

# How often the scanning status line actually redraws. on_progress fires
# once per directory visited -- possibly tens of thousands of times on a
# big tree -- and each redraw here means call_from_thread() hopping back
# to the main thread, so redrawing on every single call would make the
# status display itself a meaningful chunk of the scan's cost.
_PROGRESS_UPDATE_INTERVAL = 1.0

from diskuh import cache, format as fmt


class ConfirmDeleteScreen(ModalScreen[bool]):
    """Modal: confirm before a permanent delete."""

    DEFAULT_CSS = """
    ConfirmDeleteScreen {
        align: center middle;
    }
    #confirm-dialog {
        width: 60%;
        max-width: 80;
        height: auto;
        border: thick $error;
        background: $surface;
        padding: 1 2;
    }
    #confirm-buttons {
        height: auto;
        align: right middle;
        padding-top: 1;
    }
    #confirm-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, target: Path, is_dir: bool) -> None:
        super().__init__()
        self.target = target
        self.is_dir = is_dir

    def compose(self) -> ComposeResult:
        kind = "directory (recursively)" if self.is_dir else "file"
        with Vertical(id="confirm-dialog"):
            yield Static(
                f"Permanently delete this {kind}?\n\n{self.target}\n\n"
                "This cannot be undone."
            )
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Delete", id="delete", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "delete")


class ExpensiveScanConfirmScreen(ModalScreen[bool]):
    """Modal: warn before navigating to a directory that isn't already
    cached and up to date -- sizing it means a real scan rather than an
    instant cache hit, which can be slow for a large, never-visited tree."""

    DEFAULT_CSS = """
    ExpensiveScanConfirmScreen {
        align: center middle;
    }
    #scan-confirm-dialog {
        width: 60%;
        max-width: 80;
        height: auto;
        border: thick $warning;
        background: $surface;
        padding: 1 2;
    }
    #scan-confirm-buttons {
        height: auto;
        align: right middle;
        padding-top: 1;
    }
    #scan-confirm-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, target: Path) -> None:
        super().__init__()
        self.target = target

    def compose(self) -> ComposeResult:
        with Vertical(id="scan-confirm-dialog"):
            yield Static(
                f"{self.target}\n\n"
                "This directory hasn't been scanned yet (or has changed on "
                "disk since), so this may take a while for a large tree.\n\n"
                "Continue?"
            )
            with Horizontal(id="scan-confirm-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Scan", id="scan", variant="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "scan")


class LimitInputScreen(ModalScreen[Optional[int]]):
    """Modal: type how many entries to show per directory level (like the
    CLI's --head). Blank clears the limit (unlimited); Escape cancels
    unchanged."""

    DEFAULT_CSS = """
    LimitInputScreen {
        align: center middle;
    }
    #limit-dialog {
        width: 44;
        height: auto;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, current: int | None) -> None:
        super().__init__()
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="limit-dialog"):
            yield Static("Show how many entries per directory? (blank = unlimited)")
            yield Input(
                value=str(self._current) if self._current else "",
                placeholder="unlimited",
                id="limit-input",
            )

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(self._current)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            self.dismiss(None)
            return
        try:
            n = int(text)
        except ValueError:
            self.notify("Enter a whole number, or leave blank for auto.", severity="error")
            return
        if n <= 0:
            self.notify("Must be a positive number.", severity="error")
            return
        self.dismiss(n)


class BrowserApp(App):
    """Browse a directory: a multi-level tree (down to `_DEFAULT_TREE_DEPTH`
    levels) rooted at the current directory, with `tree`-style connectors
    (matching the CLI's rendering) and a human size + proportional bar per
    row. Enter drills down (rooting the view at whatever row is selected,
    at any depth shown), backspace goes up, 'd' deletes (with
    confirmation), 's' toggles sort order, 'n' sets how many entries to
    show per directory level (default: unlimited)."""

    TITLE = "diskuh"

    CSS = """
    DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("enter", "drill_down", "Open"),
        Binding("backspace", "go_up", "Up"),
        Binding("d", "delete_selected", "Delete"),
        Binding("s", "cycle_sort", "Sort"),
        Binding("n", "set_limit", "Limit"),
        Binding("r", "rescan", "Rescan"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self.current_path = root
        self.sort_by_size = True
        # How many entries to show per directory level (like the CLI's
        # --head); None = unlimited. Set by 'n'. Same default as the CLI.
        self.limit: int | None = fmt.DEFAULT_HEAD
        self.tree_depth = _DEFAULT_TREE_DEPTH
        self.conn = cache.connect()
        cache.init_schema(self.conn)
        # `exclusive=True` on _scan_worker stops tracking a superseded scan,
        # but the OS thread itself keeps running to completion regardless
        # (Python threads can't be force-cancelled) -- so a slow, abandoned
        # scan could still call back and overwrite a newer one's results.
        # Each _load() bumps this and stamps its worker with the new value;
        # a callback whose stamp no longer matches is stale and is dropped.
        self._scan_generation = 0

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="entries", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self._load(self.current_path)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # DataTable binds "enter" itself (to post this message) before it
        # would ever bubble up to the App-level "enter" binding below, so
        # drill-down has to be wired here rather than relying on that
        # binding actually firing while the table has focus.
        self.action_drill_down()

    def _load(self, path: Path, *, force_rescan: bool = False) -> None:
        # Navigation itself is instant: update position and show a loading
        # state right away. The actual scan (which can be slow) runs on a
        # worker thread so the UI stays responsive and repaints normally
        # while it's in flight -- it used to run inline here and block the
        # whole app until get_or_refresh() returned.
        self._scan_generation += 1
        generation = self._scan_generation
        self.current_path = path
        self.sub_title = f"Scanning {path} ..."
        table = self.query_one("#entries", DataTable)
        table.loading = True
        self._scan_worker(path, force_rescan, generation)

    @work(thread=True, exclusive=True, group="scan")
    def _scan_worker(self, path: Path, force_rescan: bool, generation: int) -> None:
        last_update = 0.0

        def on_progress(p: Path) -> None:
            nonlocal last_update
            now = time.monotonic()
            if now - last_update >= _PROGRESS_UPDATE_INTERVAL:
                last_update = now
                self.call_from_thread(self._set_scan_status, p, generation)

        node = cache.get_or_refresh(
            self.conn, self.root, path, force_rescan=force_rescan, on_progress=on_progress
        )

        def _expand(n):
            cache.expand(self.conn, self.root, n, force_rescan=force_rescan, on_progress=on_progress)

        # Multi-level, tree-connector listing -- the same hierarchical,
        # per-directory-capped algorithm the CLI uses (see format.py's
        # module docstring), just with files included as leaves too (the
        # CLI excludes them to match `du`; the TUI still needs them
        # browsable/selectable/deletable).
        sort_key = None if self.sort_by_size else (lambda c: c.path.name.lower())
        entries, hidden = fmt.collect_entries(
            node,
            self.tree_depth,
            expand=_expand,
            head=self.limit,
            include_files=True,
            sort_key=sort_key,
        )
        self.call_from_thread(self._populate_table, path, node, entries, hidden, generation)

    def _set_scan_status(self, current: Path, generation: int) -> None:
        if generation != self._scan_generation:
            return  # superseded by a newer navigation; discard
        self.sub_title = f"Scanning {self.current_path} ... [{current}]"

    def _populate_table(self, path: Path, node, entries, hidden: int, generation: int) -> None:
        if generation != self._scan_generation:
            return  # superseded by a newer navigation; discard
        table = self.query_one("#entries", DataTable)
        table.clear(columns=True)
        table.add_columns("Size", "", "Name")

        sizes = [entry.size for _, entry, _ in entries]
        max_size = max(sizes, default=1) or 1
        for _depth, entry, prefix in entries:
            # Escape the real filename -- see format.py's matching comment:
            # a name containing literal "[...]" would otherwise be
            # misinterpreted as Rich style tags instead of shown as-is.
            safe_name = _escape_markup(entry.path.name) + ("/" if entry.is_dir else "")
            name = f"[dim]{prefix}[/dim]{safe_name}"
            table.add_row(
                fmt.human_size(entry.size),
                fmt.render_bar(entry.size, max_size),
                name,
                key=str(entry.path),
            )
        table.loading = False

        limit_note = f", head {self.limit}" if self.limit is not None else ""
        hidden_note = f"  [{hidden} more not shown]" if hidden > 0 else ""
        self.sub_title = f"{path}  ({fmt.human_size(node.size)}{limit_note}){hidden_note}"
        for err in node.errors:
            self.notify(err, severity="warning", timeout=5)

    def _selected_path(self) -> Path | None:
        table = self.query_one("#entries", DataTable)
        if table.row_count == 0:
            return None
        coord = table.cursor_coordinate
        row_key, _ = table.coordinate_to_cell_key(coord)
        if row_key is None or row_key.value is None:
            return None
        return Path(row_key.value)

    def action_drill_down(self) -> None:
        target = self._selected_path()
        if target is not None and target.is_dir():
            self._load(target)

    @work(exclusive=True, group="go-up")
    async def action_go_up(self) -> None:
        # Only the true filesystem root has no parent to go to. Navigating
        # above the directory the TUI was launched on is allowed -- it used
        # to hard-stop there, but that's redundant now: the is_cached check
        # + confirmation dialog right below is what actually protects
        # against an unbounded, unwarned scan of a huge/uncached tree
        # (previously that check didn't exist, so the hard stop was the
        # only thing preventing Backspace from walking out to "/" and
        # triggering a scan of the entire filesystem).
        if self.current_path == self.current_path.parent:
            return
        parent = self.current_path.parent

        # The parent was necessarily visited on the way down, so this is
        # normally an instant cache hit -- but a rescan ('r'), a delete
        # that evicted it, or a change on disk since can make it genuinely
        # expensive. Do the cheap one-directory check off the main thread
        # (it's a real, if shallow, scandir) and only bother the user if
        # it says a real (re)scan is actually needed.
        quick = await asyncio.to_thread(cache.is_cached, self.conn, self.root, parent)
        if not quick:
            confirmed = await self.push_screen_wait(ExpensiveScanConfirmScreen(parent))
            if not confirmed:
                return
        self._load(parent)

    def action_cycle_sort(self) -> None:
        self.sort_by_size = not self.sort_by_size
        self._load(self.current_path)

    def action_rescan(self) -> None:
        self._load(self.current_path, force_rescan=True)

    @work
    async def action_set_limit(self) -> None:
        result = await self.push_screen_wait(LimitInputScreen(self.limit))
        self.limit = result
        self._load(self.current_path)

    @work
    async def action_delete_selected(self) -> None:
        target = self._selected_path()
        if target is None:
            return
        is_dir = target.is_dir()
        confirmed = await self.push_screen_wait(ConfirmDeleteScreen(target, is_dir))
        if not confirmed:
            return
        try:
            if is_dir:
                shutil.rmtree(target)
            else:
                os.remove(target)
        except OSError as e:
            self.notify(f"Delete failed: {e}", severity="error", timeout=8)
            return
        cache.evict_path(self.conn, self.root, target)
        self._load(self.current_path)


def run_tui(root: Path) -> None:
    BrowserApp(root).run()
