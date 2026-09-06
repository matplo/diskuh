from __future__ import annotations

import importlib.metadata
import os
import re
import sys

import pytest
from click.testing import CliRunner

from diskuh.cli import main


def test_dhx_aliases_are_registered():
    # dhx/dhx-tui are short aliases for diskuh/diskuh-tui -- same
    # functions, not a separate implementation (see pyproject.toml).
    entry_points = {
        ep.name: ep.value
        for ep in importlib.metadata.entry_points(group="console_scripts")
        if ep.name in ("diskuh", "diskuh-tui", "dhx", "dhx-tui")
    }
    assert entry_points.get("dhx") == entry_points.get("diskuh") == "diskuh.cli:main"
    assert (
        entry_points.get("dhx-tui")
        == entry_points.get("diskuh-tui")
        == "diskuh.cli:tui_main"
    )


def _isolate_cache_db(monkeypatch, tmp_path):
    # Keep the cache DB outside the scanned tree (tree_factory scans
    # tmp_path itself), otherwise the DB's own files would be counted.
    db_dir = tmp_path.parent / (tmp_path.name + "-cachedb")
    monkeypatch.setattr("diskuh.cache.DB_PATH", db_dir / "cache.sqlite3")
    monkeypatch.setattr("diskuh.cache.DB_DIR", db_dir)


def test_default_output_smoke(tree_factory, tmp_path, monkeypatch):
    # Only directories get their own row (matching du semantics); a bare
    # file's bytes still count toward its parent's total, but it isn't
    # listed itself -- see format.py's module docstring.
    root = tree_factory({"a": {"b.txt": 100}, "c": {"d.txt": 10}})
    _isolate_cache_db(monkeypatch, tmp_path)

    runner = CliRunner()
    result = runner.invoke(main, [str(root)])

    assert result.exit_code == 0
    assert "a" in result.output
    assert "c" in result.output


def test_default_depth_is_one(tree_factory, tmp_path, monkeypatch):
    # With no --depth, only top-level directories are reported (nested's
    # own row is suppressed, though it's still counted in outer's total).
    root = tree_factory({"outer": {"nested": {"c.txt": 100}}, "sibling": {"f.txt": 10}})
    _isolate_cache_db(monkeypatch, tmp_path)

    runner = CliRunner()
    result = runner.invoke(main, [str(root)])

    assert result.exit_code == 0
    assert "outer" in result.output
    assert "sibling" in result.output
    assert "nested" not in result.output

    result_deep = runner.invoke(main, ["--depth", "2", str(root)])
    assert "nested" in result_deep.output


def test_depth_zero_means_unlimited(tree_factory, tmp_path, monkeypatch):
    root = tree_factory(
        {"outer": {"middle": {"innermost": {"deep.txt": 100}}}, "sibling": {"f.txt": 10}}
    )
    _isolate_cache_db(monkeypatch, tmp_path)

    runner = CliRunner()
    result = runner.invoke(main, ["--depth", "0", str(root)])

    assert result.exit_code == 0
    assert "innermost" in result.output  # the deepest directory is reached


def test_depth_short_flag_is_l_not_d(tree_factory, tmp_path, monkeypatch):
    # -d used to be --depth's short flag; changed to -l ("depth-Level") to
    # avoid any mnemonic overlap with the TUI's 'd' (delete) binding, even
    # though they're different input surfaces. -d must no longer work.
    root = tree_factory({"outer": {"nested": {"c.txt": 100}}})
    _isolate_cache_db(monkeypatch, tmp_path)

    runner = CliRunner()
    result_l = runner.invoke(main, ["-l", "2", str(root)])
    assert result_l.exit_code == 0
    assert "nested" in result_l.output

    result_d = runner.invoke(main, ["-d", "2", str(root)])
    assert result_d.exit_code != 0
    assert "No such option" in result_d.output


def test_negative_depth_is_rejected(tree_factory, tmp_path, monkeypatch):
    root = tree_factory({"e.txt": 10})
    _isolate_cache_db(monkeypatch, tmp_path)

    runner = CliRunner()
    result = runner.invoke(main, ["--depth", "-1", str(root)])

    assert result.exit_code != 0
    assert "--depth" in result.output


def _many_entries_tree(n: int) -> dict:
    # Sizes spread far enough apart (tens of KB, well-separated) that disk
    # block rounding can't collapse two of them into a tie -- a tiny
    # byte-count difference (e.g. "10 vs 11 bytes") can easily round to the
    # exact same number of blocks and make the sort order arbitrary.
    return {f"d{i:02d}": {"f.txt": (n - i) * 20_000 + 1000} for i in range(n)}


def test_default_head_limits_to_ten(tree_factory, tmp_path, monkeypatch):
    root = tree_factory(_many_entries_tree(15))
    _isolate_cache_db(monkeypatch, tmp_path)

    runner = CliRunner()
    result = runner.invoke(main, ["--depth", "1", str(root)])

    assert result.exit_code == 0
    for i in range(10):
        assert f"d{i:02d}" in result.output
    for i in range(10, 15):
        assert f"d{i:02d}" not in result.output
    assert "5 more subdirectories not shown" in result.output


def test_head_zero_disables_default_limit(tree_factory, tmp_path, monkeypatch):
    root = tree_factory(_many_entries_tree(15))
    _isolate_cache_db(monkeypatch, tmp_path)

    runner = CliRunner()
    result = runner.invoke(main, ["--depth", "1", "--head", "0", str(root)])

    assert result.exit_code == 0
    for i in range(15):
        assert f"d{i:02d}" in result.output
    assert "more subdirectories not shown" not in result.output


def test_tail_alone_does_not_trigger_default_head(tree_factory, tmp_path, monkeypatch):
    root = tree_factory(_many_entries_tree(15))
    _isolate_cache_db(monkeypatch, tmp_path)

    runner = CliRunner()
    result = runner.invoke(main, ["--depth", "1", "--tail", "3", str(root)])

    assert result.exit_code == 0
    # tail=3 keeps the 3 smallest -- the highest-numbered dirs, given
    # _many_entries_tree's descending sizes.
    for i in (12, 13, 14):
        assert f"d{i:02d}" in result.output
    assert "d00" not in result.output


def test_head_and_tail_both_positive_rejected(tree_factory, tmp_path, monkeypatch):
    root = tree_factory({"e.txt": 10})
    _isolate_cache_db(monkeypatch, tmp_path)

    runner = CliRunner()
    result = runner.invoke(main, ["--head", "3", "--tail", "3", str(root)])

    assert result.exit_code != 0


def test_head_zero_with_tail_is_allowed(tree_factory, tmp_path, monkeypatch):
    root = tree_factory(_many_entries_tree(5))
    _isolate_cache_db(monkeypatch, tmp_path)

    runner = CliRunner()
    result = runner.invoke(main, ["--depth", "1", "--head", "0", "--tail", "2", str(root)])

    assert result.exit_code == 0
    assert "d03" in result.output and "d04" in result.output
    assert "d00" not in result.output


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission checks")
def test_errors_summarized_by_default_and_not_duplicated(tree_factory, tmp_path, monkeypatch):
    root = tree_factory({"locked": {"secret.txt": 10}, "open.txt": 5})
    locked = root / "locked"
    locked.chmod(0o000)
    _isolate_cache_db(monkeypatch, tmp_path)
    try:
        runner = CliRunner()
        result = runner.invoke(main, [str(root)])
        assert result.exit_code == 0
        assert "error(s) while scanning" in result.output
        assert "--show-errors" in result.output
        assert result.output.count("Permission denied") == 0  # only the summary line, not raw errors

        result_detailed = runner.invoke(main, ["--show-errors", str(root)])
        assert "locked" in result_detailed.output
        # Exactly one *line* mentions it -- regression: this error used to
        # be printed both by format.py and cli.py. (A single line
        # legitimately contains the path twice: our own prefix, plus
        # OSError's own str() embeds the filename again -- so we count
        # matching lines, not substring occurrences.)
        matching_lines = [
            line for line in result_detailed.output.splitlines() if str(locked) in line
        ]
        assert len(matching_lines) == 1
    finally:
        locked.chmod(0o755)


def test_terse_output_is_plain(tree_factory, tmp_path, monkeypatch):
    root = tree_factory({"e.txt": 10})
    _isolate_cache_db(monkeypatch, tmp_path)

    runner = CliRunner()
    result = runner.invoke(main, ["--terse", str(root)])

    assert result.exit_code == 0
    ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
    assert ansi_escape.search(result.output) is None
    lines = [line for line in result.output.splitlines() if line]
    assert any("\t" in line for line in lines)


def test_tui_kwargs_for_depth():
    from diskuh.cli import _tui_kwargs_for_depth

    assert _tui_kwargs_for_depth(None) == {}  # unspecified: let run_tui pick its own default
    assert _tui_kwargs_for_depth(0) == {"initial_depth": None}  # explicit 0 = unlimited
    assert _tui_kwargs_for_depth(5) == {"initial_depth": 5}


def test_main_tui_flag_passes_depth_through(tree_factory, tmp_path, monkeypatch):
    root = tree_factory({"a": {"b.txt": 10}})
    _isolate_cache_db(monkeypatch, tmp_path)

    calls = []
    monkeypatch.setattr("diskuh.tui.run_tui", lambda *a, **kw: calls.append((a, kw)))

    runner = CliRunner()

    # --depth unspecified -> nothing passed, run_tui uses its own default.
    result = runner.invoke(main, ["--tui", str(root)])
    assert result.exit_code == 0
    assert calls[-1][1] == {}

    # explicit --depth N (via -l) -> forwarded as initial_depth.
    result = runner.invoke(main, ["--tui", "-l", "5", str(root)])
    assert result.exit_code == 0
    assert calls[-1][1] == {"initial_depth": 5}

    # explicit --depth 0 -> unlimited.
    result = runner.invoke(main, ["--tui", "--depth", "0", str(root)])
    assert result.exit_code == 0
    assert calls[-1][1] == {"initial_depth": None}


def test_tui_main_passes_depth_through(tree_factory, tmp_path, monkeypatch):
    root = tree_factory({"a": {"b.txt": 10}})
    _isolate_cache_db(monkeypatch, tmp_path)

    calls = []
    monkeypatch.setattr("diskuh.tui.run_tui", lambda *a, **kw: calls.append((a, kw)))

    from diskuh.cli import tui_main

    runner = CliRunner()
    result = runner.invoke(tui_main, ["-l", "7", str(root)])
    assert result.exit_code == 0
    assert calls[-1][1] == {"initial_depth": 7}

    result = runner.invoke(tui_main, [str(root)])
    assert result.exit_code == 0
    assert calls[-1][1] == {}
