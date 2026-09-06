"""Best-effort file creation ("birth") time, across platforms.

`os.stat()`'s `st_birthtime` is a macOS/BSD stat() extension: it's always
present on those platforms (part of the OS's `struct stat`, a compile-time
fact about the platform, not a per-file/per-filesystem runtime one), so a
plain `os.stat()`/`Path.stat()` call is enough there.

On Linux, the classic `stat()`/`stat64` syscall that `os.stat()` uses has
never exposed a creation time at all -- not because filesystems don't track
one (ext4, XFS, and Btrfs all do), but because that syscall's `struct stat`
has no field for it. Getting it on Linux requires the newer `statx()`
syscall (kernel 4.11+, glibc/musl wrapper since ~2018) with the
`STATX_BTIME` mask bit, which Python's stdlib still doesn't wrap as of this
writing. We call it directly via `ctypes` -- no new dependency.

Even with `statx()`, plenty of cases still can't produce a birth time: an
old kernel/libc without the syscall, a filesystem that doesn't track one at
all (tmpfs, many network filesystems), or the file simply not being on
Linux. All of those come back as `None`, same as "unknown" -- callers
should not treat `None` as an error.
"""

from __future__ import annotations

import ctypes
import os
import platform
from pathlib import Path

_IS_LINUX = platform.system() == "Linux"


def get_birthtime(path: Path, st: os.stat_result) -> float | None:
    """Best-effort creation time in seconds since the epoch, from an
    already-taken `st` (`path.stat()`), or `None` if this platform/
    filesystem doesn't track one (or on any error)."""
    birthtime = getattr(st, "st_birthtime", None)
    if birthtime is not None:
        return birthtime
    if _IS_LINUX:
        return _linux_statx_birthtime(path)
    return None


class _StatxTimestamp(ctypes.Structure):
    _fields_ = [
        ("tv_sec", ctypes.c_int64),
        ("tv_nsec", ctypes.c_uint32),
        ("__reserved", ctypes.c_int32),
    ]


class _Statx(ctypes.Structure):
    # Matches Linux's <linux/stat.h> `struct statx` layout. Only the fields
    # up to and including stx_btime matter to us; the rest just need to
    # exist with the right sizes so the struct's total layout (and thus
    # padding-derived offsets) matches what the kernel writes.
    _fields_ = [
        ("stx_mask", ctypes.c_uint32),
        ("stx_blksize", ctypes.c_uint32),
        ("stx_attributes", ctypes.c_uint64),
        ("stx_nlink", ctypes.c_uint32),
        ("stx_uid", ctypes.c_uint32),
        ("stx_gid", ctypes.c_uint32),
        ("stx_mode", ctypes.c_uint16),
        ("__spare0", ctypes.c_uint16 * 1),
        ("stx_ino", ctypes.c_uint64),
        ("stx_size", ctypes.c_uint64),
        ("stx_blocks", ctypes.c_uint64),
        ("stx_attributes_mask", ctypes.c_uint64),
        ("stx_atime", _StatxTimestamp),
        ("stx_btime", _StatxTimestamp),
        ("stx_ctime", _StatxTimestamp),
        ("stx_mtime", _StatxTimestamp),
        ("stx_rdev_major", ctypes.c_uint32),
        ("stx_rdev_minor", ctypes.c_uint32),
        ("stx_dev_major", ctypes.c_uint32),
        ("stx_dev_minor", ctypes.c_uint32),
        ("stx_mnt_id", ctypes.c_uint64),
        ("stx_dio_mem_align", ctypes.c_uint32),
        ("stx_dio_offset_align", ctypes.c_uint32),
        ("__spare3", ctypes.c_uint64 * 12),
    ]


_AT_FDCWD = -100
_AT_SYMLINK_NOFOLLOW = 0x100
_STATX_BTIME = 0x800

_libc: ctypes.CDLL | None = None
_libc_load_attempted = False


def _get_libc() -> ctypes.CDLL | None:
    """Lazily resolve and cache the statx() symbol. `ctypes.CDLL(None)`
    (dlopen(NULL)) gives access to symbols already loaded into this
    process -- i.e. whatever libc Python itself is dynamically linked
    against (glibc or musl, both of which have wrapped statx() since
    ~2018) -- without needing to guess a specific .so filename that varies
    by distro. Returns None (cached) if statx isn't available at all: a
    fully static Python build, or a libc old enough to predate it."""
    global _libc, _libc_load_attempted
    if not _libc_load_attempted:
        _libc_load_attempted = True
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            libc.statx.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_uint,
                ctypes.POINTER(_Statx),
            ]
            libc.statx.restype = ctypes.c_int
            _libc = libc
        except (OSError, AttributeError):
            _libc = None
    return _libc


def _linux_statx_birthtime(path: Path) -> float | None:
    libc = _get_libc()
    if libc is None:
        return None
    buf = _Statx()
    try:
        ret = libc.statx(
            _AT_FDCWD,
            os.fsencode(str(path)),
            _AT_SYMLINK_NOFOLLOW,
            _STATX_BTIME,
            ctypes.byref(buf),
        )
    except OSError:
        return None
    if ret != 0:
        return None
    if not (buf.stx_mask & _STATX_BTIME):
        return None  # this filesystem doesn't track a birth time at all
    return buf.stx_btime.tv_sec + buf.stx_btime.tv_nsec / 1e9
