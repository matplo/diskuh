from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path

import click
from rich.console import Console

from diskuh.format import DEFAULT_HEAD

# How often the progress display's *content* actually redraws. It's called
# once per directory visited -- possibly tens of thousands of times on a
# big tree -- so redrawing on every single call would make the progress
# display itself a meaningful chunk of the scan's cost. The count is still
# tracked exactly; only the (relatively expensive) repaint is throttled.
_PROGRESS_UPDATE_INTERVAL = 1.0

# Rich's own spinner animation redraws independently of our content
# throttling above -- by default at ~12.5Hz, for the entire scan duration,
# regardless of whether the text changed. On a huge tree (hundreds of
# thousands of directories, possibly minutes of scanning) that's a lot of
# terminal writes competing for CPU with the actual scan. Match it to our
# own update cadence instead.
_SPINNER_REFRESH_PER_SECOND = 1 / _PROGRESS_UPDATE_INTERVAL


@contextmanager
def _scan_progress(console: Console):
    """Show a live "Scanning..." spinner (directory count + current path)
    on `console` while the body runs, so a slow scan doesn't look like the
    tool has just hung. Silent when `console` isn't an interactive terminal
    (piped/redirected), matching how du/curl/pip etc. behave -- and always
    on stderr, so it never mixes into piped stdout output."""
    if not console.is_terminal:
        yield None
        return

    count = 0
    last_update = 0.0
    with console.status("Scanning...", refresh_per_second=_SPINNER_REFRESH_PER_SECOND) as status:

        def on_progress(path):
            nonlocal count, last_update
            count += 1
            now = time.monotonic()
            if now - last_update >= _PROGRESS_UPDATE_INTERVAL:
                last_update = now
                status.update(f"Scanning... {count} directories  [dim]{path}[/dim]")

        yield on_progress


def _validate_depth(depth: int | None) -> None:
    if depth is not None and depth < 0:
        raise click.UsageError("--depth must be 0 (unlimited) or a positive integer.")


def _tui_kwargs_for_depth(depth: int | None) -> dict:
    """Resolve --depth into run_tui()'s initial_depth kwarg. Shared between
    `main()`'s --tui branch and `tui_main()` so the three-state handling
    (not given / explicitly 0 / explicit N) isn't duplicated: unspecified
    means "let the TUI use its own default" (an empty dict, so run_tui's
    own Python-level default parameter value applies), explicit 0 means
    unlimited, and N means exactly N levels."""
    if depth is None:
        return {}
    return {"initial_depth": None if depth == 0 else depth}


def _report_errors(errors: list[str], *, show_errors: bool) -> None:
    """By default, a scan hitting hundreds of permission-denied
    subdirectories (common under ~/Library on macOS, say) would otherwise
    dump one line per error -- easily swamping the actual report. Print a
    one-line summary instead unless the caller asked to see them all."""
    if not errors:
        return
    if show_errors:
        for err in errors:
            click.echo(f"diskuh: {err}", err=True)
    else:
        click.echo(
            f"diskuh: {len(errors)} error(s) while scanning (e.g. permission denied); "
            "rerun with --show-errors to see them.",
            err=True,
        )


@click.command()
@click.argument(
    "path",
    type=click.Path(exists=True, file_okay=True, dir_okay=True, path_type=Path),
    default=".",
)
@click.option(
    "--depth",
    "-l",
    "depth",
    type=int,
    default=None,
    help="Limit reported directory depth (like du --max-depth); 0 = unlimited. "
    "Default: 1 for the report, 3 for --tui. Sizes are always accurate "
    "regardless of depth.",
)
@click.option(
    "--terse",
    is_flag=True,
    default=False,
    help="Plain script-friendly output: size<TAB>path, no colors/bars.",
)
@click.option(
    "--no-cache",
    "--rescan",
    "no_cache",
    is_flag=True,
    default=False,
    help="Bypass the cache; force a fresh full scan.",
)
@click.option(
    "--tui",
    is_flag=True,
    default=False,
    help="Launch the full-screen TUI instead of printing to stdout.",
)
@click.option(
    "--head",
    "head",
    type=int,
    default=None,
    metavar="N",
    help=f"Show only the N largest entries (default: {DEFAULT_HEAD}); 0 = unlimited.",
)
@click.option(
    "--tail",
    "tail",
    type=int,
    default=None,
    metavar="N",
    help="Show only the N smallest entries. Overrides the default --head.",
)
@click.option(
    "--show-errors",
    is_flag=True,
    default=False,
    help="Print every scan error individually instead of a one-line summary.",
)
def main(
    path: Path,
    depth: int | None,
    terse: bool,
    no_cache: bool,
    tui: bool,
    head: int | None,
    tail: int | None,
    show_errors: bool,
) -> None:
    """du, but with attractive human-readable output and an optional TUI."""
    resolved = path.resolve()

    _validate_depth(depth)
    if head is not None and head < 0:
        raise click.UsageError("--head must be 0 (unlimited) or a positive integer.")
    if tail is not None and tail <= 0:
        raise click.UsageError("--tail must be a positive integer.")
    if head is not None and head > 0 and tail is not None:
        raise click.UsageError("--head and --tail are mutually exclusive.")

    if head is None and tail is None:
        head = DEFAULT_HEAD  # show only the biggest entries unless told otherwise
    elif head == 0:
        head = None  # explicit opt-out of the default limit

    if tui:
        from diskuh.tui import run_tui

        run_tui(resolved, **_tui_kwargs_for_depth(depth))
        return

    # Plain report: --depth unspecified defaults to 1 (unlike --tui, which
    # defaults to run_tui()'s own, deeper default -- see
    # _tui_kwargs_for_depth).
    report_depth = 1 if depth is None else depth
    max_depth = None if report_depth == 0 else report_depth

    from diskuh import cache, format as fmt

    conn = cache.connect()
    cache.init_schema(conn)

    progress_console = Console(stderr=True)
    with _scan_progress(progress_console) as on_progress:
        root_node = cache.get_or_refresh(
            conn, resolved, resolved, force_rescan=no_cache, on_progress=on_progress
        )

        def _expand(node):
            cache.expand(conn, resolved, node, force_rescan=no_cache, on_progress=on_progress)

        # Collecting entries can still trigger real scanning (expanding a
        # cache-collapsed stub), so it happens *inside* the progress
        # context. Rendering happens only after that context has fully
        # torn down its live display below -- doing it while the spinner
        # was still active (a separate Console, on stderr) used to garble
        # the very first line of output where the two writes interleaved.
        entries, hidden = fmt.collect_entries(
            root_node, max_depth, expand=_expand, head=head, tail=tail
        )

    if terse:
        fmt.render_terse(root_node, entries=entries, hidden=hidden)
    else:
        fmt.render_tree(Console(), root_node, entries=entries, hidden=hidden)

    _report_errors(root_node.errors, show_errors=show_errors)


@click.command()
@click.argument(
    "path",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=".",
)
@click.option(
    "--depth",
    "-l",
    "depth",
    type=int,
    default=None,
    help="How many levels deep to show at once (default: 3); 0 = unlimited.",
)
def tui_main(path: Path, depth: int | None) -> None:
    """Launch the diskuh TUI directly."""
    _validate_depth(depth)
    from diskuh.tui import run_tui

    run_tui(path.resolve(), **_tui_kwargs_for_depth(depth))


if __name__ == "__main__":
    main()
