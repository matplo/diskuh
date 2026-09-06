from __future__ import annotations

from pathlib import Path

import pytest

from diskuh.tui import BrowserApp


@pytest.fixture
def app_root(tree_factory, tmp_path, monkeypatch):
    # Keep the cache DB out of both the scanned tree and the user's real
    # ~/.diskuh -- tree_factory scans tmp_path itself.
    db_dir = tmp_path.parent / (tmp_path.name + "-cachedb")
    monkeypatch.setattr("diskuh.cache.DB_PATH", db_dir / "cache.sqlite3")
    monkeypatch.setattr("diskuh.cache.DB_DIR", db_dir)
    return tree_factory({"keep": {"a.txt": 20}, "delete_me": {"b.txt": 15}})


async def _wait_loaded(app, pilot, timeout: float = 5.0) -> None:
    """Scans now run on a worker thread (see tui.py); wait for the table's
    loading indicator to clear instead of assuming a bare pilot.pause() is
    enough time for the background thread to finish and call back."""
    table = app.query_one("#entries")
    elapsed = 0.0
    step = 0.02
    while table.loading and elapsed < timeout:
        await pilot.pause(step)
        elapsed += step
    assert not table.loading, "scan did not finish within timeout"


@pytest.mark.asyncio
async def test_multi_level_tree_shows_nested_entries_with_connectors(tmp_path, monkeypatch):
    db_dir = tmp_path.parent / (tmp_path.name + "-cachedb")
    monkeypatch.setattr("diskuh.cache.DB_PATH", db_dir / "cache.sqlite3")
    monkeypatch.setattr("diskuh.cache.DB_DIR", db_dir)

    from conftest import make_tree

    make_tree(tmp_path, {"outer": {"inner": {"deep.txt": 500}}})
    root = tmp_path

    app = BrowserApp(root)
    async with app.run_test() as pilot:
        await _wait_loaded(app, pilot)
        table = app.query_one("#entries")
        # outer, inner, deep.txt -- all three levels visible at once.
        assert table.row_count == 3
        cols = list(table.columns.keys())
        names = [table.get_cell(row, cols[2]) for row in table.rows.keys()]
        assert any("outer" in n for n in names)
        assert any("inner" in n for n in names)
        assert any("deep.txt" in n for n in names)
        # tree connectors are present, distinguishing nesting from flat
        # indentation.
        assert any("├──" in n or "└──" in n for n in names)


@pytest.mark.asyncio
async def test_drill_down_and_up(app_root):
    app = BrowserApp(app_root)
    async with app.run_test() as pilot:
        await _wait_loaded(app, pilot)
        assert app.current_path == app_root

        await pilot.press("enter")  # row-selected message -> drill_down
        await _wait_loaded(app, pilot)
        assert app.current_path in (app_root / "keep", app_root / "delete_me")

        await pilot.press("backspace")
        await _wait_loaded(app, pilot)
        assert app.current_path == app_root


@pytest.mark.asyncio
async def test_go_up_can_leave_the_launch_directory(app_root):
    # Navigating above the directory the TUI was launched on is allowed --
    # a confirmation dialog (tested separately below) is what guards
    # against an unbounded/unwarned scan now, not a hard stop at root.
    app = BrowserApp(app_root)
    async with app.run_test() as pilot:
        await _wait_loaded(app, pilot)
        await pilot.press("backspace")
        await pilot.pause(0.2)
        # app_root's parent was never scanned -> confirm first.
        assert len(app.screen_stack) == 2
        await pilot.click("#scan")
        await _wait_loaded(app, pilot)
        assert app.current_path == app_root.parent


@pytest.mark.asyncio
async def test_go_up_stops_only_at_filesystem_root(app_root):
    # The one real boundary: "/" has no parent to go to.
    app = BrowserApp(app_root)
    async with app.run_test() as pilot:
        await _wait_loaded(app, pilot)
        app.current_path = Path(app_root.anchor)  # e.g. "/"
        await pilot.press("backspace")
        await pilot.pause(0.2)
        assert len(app.screen_stack) == 1  # no confirm dialog, no-op
        assert app.current_path == Path(app_root.anchor)


@pytest.mark.asyncio
async def test_sort_toggle(app_root):
    app = BrowserApp(app_root)
    async with app.run_test() as pilot:
        await _wait_loaded(app, pilot)
        assert app.sort_by_size is True
        await pilot.press("s")
        await _wait_loaded(app, pilot)
        assert app.sort_by_size is False


@pytest.mark.asyncio
async def test_delete_confirmed_removes_target(app_root):
    app = BrowserApp(app_root)
    async with app.run_test() as pilot:
        await _wait_loaded(app, pilot)
        table = app.query_one("#entries")
        while app._selected_path().name != "delete_me":
            table.action_cursor_down()

        await pilot.press("d")
        await pilot.pause()
        assert len(app.screen_stack) == 2  # confirmation modal is up

        await pilot.click("#delete")
        await _wait_loaded(app, pilot)

        assert not (app_root / "delete_me").exists()
        assert (app_root / "keep").exists()


@pytest.mark.asyncio
async def test_delete_cancelled_keeps_target(app_root):
    app = BrowserApp(app_root)
    async with app.run_test() as pilot:
        await _wait_loaded(app, pilot)
        await pilot.press("d")
        await pilot.pause()
        await pilot.click("#cancel")
        await pilot.pause(0.2)

        assert (app_root / "keep").exists()
        assert (app_root / "delete_me").exists()


@pytest.mark.asyncio
async def test_loading_indicator_during_scan(app_root, monkeypatch):
    # Regression: get_or_refresh() used to run inline on the main thread
    # and block the whole UI until it finished. It must now run on a
    # worker, with the table's loading indicator up for the duration.
    # Artificially slow the scan so the mid-flight state is deterministic
    # rather than racing a real (near-instant) tiny-tree scan.
    import time

    from diskuh import cache as cache_module

    real_get_or_refresh = cache_module.get_or_refresh

    def slow_get_or_refresh(*args, **kwargs):
        time.sleep(0.2)
        return real_get_or_refresh(*args, **kwargs)

    monkeypatch.setattr("diskuh.tui.cache.get_or_refresh", slow_get_or_refresh)

    app = BrowserApp(app_root)
    async with app.run_test() as pilot:
        table = app.query_one("#entries")
        assert table.loading is True  # scan kicked off immediately on mount
        await pilot.pause(0.05)
        assert table.loading is True  # still running: UI hasn't frozen waiting on it
        await _wait_loaded(app, pilot)
        assert table.loading is False
        # 2 top-level dirs + their one file each, now that the TUI shows a
        # multi-level tree (see BrowserApp.tree_depth) instead of just the
        # current directory's immediate children.
        assert table.row_count == 4


@pytest.mark.asyncio
async def test_go_up_confirms_when_parent_not_cached(app_root):
    app = BrowserApp(app_root)
    async with app.run_test() as pilot:
        await _wait_loaded(app, pilot)
        await pilot.press("enter")
        await _wait_loaded(app, pilot)
        assert app.current_path != app_root

        # Simulate the root's cached row having gone stale/missing (e.g. an
        # eviction elsewhere), so going back up is no longer a guaranteed
        # cache hit.
        app.conn.execute("DELETE FROM directories WHERE path = ?", (str(app_root),))
        app.conn.commit()

        await pilot.press("backspace")
        await pilot.pause(0.2)
        assert len(app.screen_stack) == 2  # confirmation modal is up
        assert app.current_path != app_root  # didn't navigate yet

        await pilot.click("#scan")
        await _wait_loaded(app, pilot)
        assert app.current_path == app_root


@pytest.mark.asyncio
async def test_go_up_confirm_cancelled_stays_put(app_root):
    app = BrowserApp(app_root)
    async with app.run_test() as pilot:
        await _wait_loaded(app, pilot)
        await pilot.press("enter")
        await _wait_loaded(app, pilot)
        drilled_into = app.current_path

        app.conn.execute("DELETE FROM directories WHERE path = ?", (str(app_root),))
        app.conn.commit()

        await pilot.press("backspace")
        await pilot.pause(0.2)
        await pilot.click("#cancel")
        await pilot.pause(0.2)

        assert app.current_path == drilled_into


@pytest.mark.asyncio
async def test_go_up_no_confirm_when_parent_cached(app_root):
    # The common case: the parent was already visited (and thus cached) on
    # the way down, so going back up should be instant with no prompt.
    app = BrowserApp(app_root)
    async with app.run_test() as pilot:
        await _wait_loaded(app, pilot)
        await pilot.press("enter")
        await _wait_loaded(app, pilot)

        await pilot.press("backspace")
        await _wait_loaded(app, pilot)
        assert len(app.screen_stack) == 1  # no confirmation modal
        assert app.current_path == app_root
