from __future__ import annotations

from pathlib import Path

from rich.console import Console

from diskuh import format as fmt
from diskuh.format import BAR_WIDTH, human_size, render_bar
from diskuh.scanner import DirNode


def test_human_size_bytes():
    assert human_size(0) == "0B"
    assert human_size(900) == "900B"


def test_human_size_units():
    assert human_size(1536) == "1.5K"
    assert human_size(1024) == "1.0K"
    assert human_size(1024**2) == "1.0M"
    assert human_size(5 * 1024**3) == "5.0G"
    assert human_size(50 * 1024**3) == "50G"


def test_render_bar_within_width():
    for size in (0, 1, 500, 1000, 999999):
        bar = render_bar(size, 1000)
        assert len(bar.plain) == BAR_WIDTH


def test_render_bar_zero_max_size():
    bar = render_bar(0, 0)
    assert len(bar.plain) == BAR_WIDTH


def test_long_name_never_truncated_at_narrow_width():
    # Regression: Rich's Table used to silently ellipsis-truncate the Name
    # column (e.g. "long_name_that_will…") when the fixed-width bar left no
    # room for it at a normal/narrow terminal width. Full names must always
    # be recoverable from the rendered output -- wrapped is fine, cut off
    # is not.
    root = DirNode(path=Path("/scan/root"))
    long_name = "a_very_long_subdirectory_name_that_will_not_fit_in_a_narrow_terminal"
    child = DirNode(path=Path("/scan/root") / long_name, size=500)
    root.children.append(child)
    root.size = 500

    for width in (20, 40, 80, 120):
        console = Console(width=width, force_terminal=False, record=True)
        fmt.render_tree(console, root, max_depth=None)
        rendered = console.export_text()
        assert "…" not in rendered
        # The name may be wrapped across lines; reassembled without
        # whitespace it must still contain the full name intact.
        assert long_name in rendered.replace("\n", "").replace(" ", "")


def test_pick_bar_width_shrinks_for_narrow_console():
    wide = fmt._pick_bar_width(200, size_col_width=6)
    narrow = fmt._pick_bar_width(30, size_col_width=6)
    assert wide == BAR_WIDTH
    assert fmt.BAR_MIN_WIDTH <= narrow < BAR_WIDTH


def test_head_and_tail_select_expected_entries():
    root = DirNode(path=Path("/root"))
    for i, size in enumerate([100, 50, 10, 5, 1]):
        root.children.append(DirNode(path=Path(f"/root/d{i}"), size=size))
    root.size = sum(c.size for c in root.children)

    console = Console(width=100, force_terminal=False, record=True)
    fmt.render_tree(console, root, head=2)
    out = console.export_text()
    assert "d0" in out and "d1" in out
    assert "d3" not in out and "d4" not in out
    assert "3 more subdirectories not shown" in out

    console2 = Console(width=100, force_terminal=False, record=True)
    fmt.render_tree(console2, root, tail=2)
    out2 = console2.export_text()
    assert "d3" in out2 and "d4" in out2
    assert "d0" not in out2
