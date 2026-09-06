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
diskuh --tui --depth 5 [PATH] # launch the TUI showing 5 levels deep at once (default: 3)
diskuh-tui [PATH]             # same TUI, dedicated entry point
diskuh-tui --depth 0 [PATH]   # same TUI, unlimited depth from the start

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
Modified/Created date columns for every visible entry. Created works via
`st_birthtime` on macOS/BSD, and via the `statx()` syscall on Linux
(kernel 4.11+/glibc or musl since ~2018) — shows `—` only where neither is
available (very old Linux, or a filesystem that doesn't track a birth time
at all, e.g. some network filesystems). Press `n` to set how many entries
to show per directory level, and `l` to set how many levels deep to show
at once — both accept blank for unlimited. `--depth`'s short flag is `-l`
(for "depth-**l**evel"), not `-d`, to avoid any mnemonic overlap with the
TUI's `d` (delete) binding.

## Development

```bash
henv --name duh-dev -x pytest -v
```

## Releasing

Published to PyPI via GitHub Actions using
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (OIDC) — no
API tokens involved. To cut a release:

1. Bump `version` in **both** `pyproject.toml` and `src/diskuh/__init__.py`.
2. Commit, then tag and push:
   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z: <summary>"
   git push origin vX.Y.Z
   ```
3. `.github/workflows/publish.yml` builds, validates with `twine check`, and
   publishes automatically. Watch it at
   `gh run watch --repo matplo/diskuh` or the Actions tab.

PyPI's installable index can lag its own JSON API by up to ~30s after a
publish — a `pip install` that briefly shows the previous version doesn't
mean the release failed; retry after a short wait.
