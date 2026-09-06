from __future__ import annotations

import ctypes
import platform
import time
from pathlib import Path

import pytest

from diskuh import _statx


def _deref(buf_ptr: ctypes.c_void_p) -> "_statx._Statx":
    """The fake `statx()` doubles below receive whatever `ctypes.byref(buf)`
    produces, which -- unlike a real ctypes-declared function's argtypes
    marshaling -- is a plain `CArgObject` with no `.contents` here, since
    these fakes are plain Python callables, not real C functions. Cast it
    back to a typed pointer to get a mutable view of the same buffer the
    caller passed in."""
    return ctypes.cast(buf_ptr, ctypes.POINTER(_statx._Statx)).contents


def test_get_birthtime_prefers_st_birthtime_when_present(monkeypatch):
    # macOS/BSD path: st_birthtime is directly on the stat_result, so the
    # Linux statx() fallback must never even be attempted.
    class FakeStat:
        st_birthtime = 12345.0

    called = []
    monkeypatch.setattr(_statx, "_linux_statx_birthtime", lambda p: called.append(p) or 0.0)

    result = _statx.get_birthtime(Path("/some/path"), FakeStat())
    assert result == 12345.0
    assert called == []


def test_get_birthtime_falls_back_to_linux_statx(monkeypatch):
    class FakeStat:
        pass  # no st_birthtime attribute at all, like real Linux os.stat()

    monkeypatch.setattr(_statx, "_IS_LINUX", True)
    monkeypatch.setattr(_statx, "_linux_statx_birthtime", lambda p: 99.0)

    assert _statx.get_birthtime(Path("/some/path"), FakeStat()) == 99.0


def test_get_birthtime_returns_none_off_linux_without_st_birthtime(monkeypatch):
    class FakeStat:
        pass

    monkeypatch.setattr(_statx, "_IS_LINUX", False)
    called = []
    monkeypatch.setattr(_statx, "_linux_statx_birthtime", lambda p: called.append(p) or 1.0)

    assert _statx.get_birthtime(Path("/some/path"), FakeStat()) is None
    assert called == []  # never attempted off Linux


def test_linux_statx_birthtime_returns_none_when_libc_unavailable(monkeypatch):
    monkeypatch.setattr(_statx, "_get_libc", lambda: None)
    assert _statx._linux_statx_birthtime(Path("/some/path")) is None


def test_linux_statx_birthtime_returns_none_on_nonzero_return_code(monkeypatch):
    class FakeLibc:
        def statx(self, *args, **kwargs):
            return -1  # syscall failed

    monkeypatch.setattr(_statx, "_get_libc", lambda: FakeLibc())
    assert _statx._linux_statx_birthtime(Path("/some/path")) is None


def test_linux_statx_birthtime_returns_none_when_mask_lacks_btime(monkeypatch):
    # statx() succeeded, but this filesystem doesn't actually track a birth
    # time, so the kernel doesn't set the STATX_BTIME bit in the response.
    class FakeLibc:
        def statx(self, dirfd, path, flags, mask, buf_ptr):
            _deref(buf_ptr).stx_mask = 0  # STATX_BTIME bit not set
            return 0

    monkeypatch.setattr(_statx, "_get_libc", lambda: FakeLibc())
    assert _statx._linux_statx_birthtime(Path("/some/path")) is None


def test_linux_statx_birthtime_extracts_seconds_and_nanoseconds(monkeypatch):
    class FakeLibc:
        def statx(self, dirfd, path, flags, mask, buf_ptr):
            buf = _deref(buf_ptr)
            buf.stx_mask = _statx._STATX_BTIME
            buf.stx_btime.tv_sec = 1_700_000_000
            buf.stx_btime.tv_nsec = 500_000_000
            return 0

    monkeypatch.setattr(_statx, "_get_libc", lambda: FakeLibc())
    result = _statx._linux_statx_birthtime(Path("/some/path"))
    assert result == pytest.approx(1_700_000_000.5)


@pytest.mark.skipif(platform.system() != "Linux", reason="exercises the real statx() syscall")
def test_get_birthtime_real_syscall_on_linux(tmp_path):
    # End-to-end on an actual Linux box (verified separately via Docker
    # during development on macOS -- this just locks it in for whenever
    # tests do run on real Linux, e.g. CI).
    f = tmp_path / "f.txt"
    before = time.time() - 1
    f.write_text("hello")
    st = f.stat()
    assert not hasattr(st, "st_birthtime")  # confirms this is the real gap being fixed

    birthtime = _statx.get_birthtime(f, st)
    assert birthtime is not None
    assert birthtime >= before
