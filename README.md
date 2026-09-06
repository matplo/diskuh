# diskuh

`du`, but with attractive human-readable output — numeric sizes plus a
proportional graphical bar per entry — and an optional full-screen TUI.

## Install (dev)

```bash
henv --name duh-dev -x python -m pip install -e ".[dev]"
```

## Usage

```bash
diskuh [PATH]                 # human-readable sizes + bars, sorted by size desc
                               # (top-level entries only by default: depth 1)
diskuh --depth 2 [PATH]       # go deeper (sizes are always accurate regardless of depth)
diskuh --depth 0 [PATH]       # unlimited depth: every directory, recursively (the old default)
diskuh --head 10 [PATH]       # show only the 10 largest entries
diskuh --tail 10 [PATH]       # show only the 10 smallest entries
diskuh --terse [PATH]         # plain "size<TAB>path" output, script/pipe friendly
diskuh --no-cache [PATH]      # bypass the cache; force a fresh full scan
diskuh --tui [PATH]           # launch the full-screen TUI
diskuh-tui [PATH]             # same TUI, dedicated entry point

dhx [PATH]                    # short alias for diskuh (identical, same flags)
dhx-tui [PATH]                # short alias for diskuh-tui
```

Scan results are cached in a SQLite database at `~/.diskuh/cache.sqlite3`,
keyed on a per-directory metadata fingerprint (name/type/size/mtime of each
directory's immediate children) so repeat scans skip re-reading unchanged
subtrees. See `src/diskuh/cache.py` for the exact staleness algorithm and its
documented limitations.

In the TUI, press `d` on a selected entry to delete it (after confirmation).
Deletion is **permanent** — there is no trash/undo. Press `i` to toggle
Modified/Created date columns for every visible entry (Created is a
macOS/BSD stat extension and shows as `—` where the platform doesn't
support it).

## Development

```bash
henv --name duh-dev -x pytest -v
```
