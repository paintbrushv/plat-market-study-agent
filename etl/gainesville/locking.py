"""POSIX-flock-based run lock with stale-lock recovery.

A lock file holds 'pid\\nstart_ts_iso\\n'. Mutual exclusion is enforced by
``fcntl.flock(LOCK_EX | LOCK_NB)`` — exactly one process can hold the
exclusive flock at a time.  When the holder's process exits (normally or
via crash), the kernel releases the flock on ``fd`` close, allowing the
next caller to acquire it.

Stale-lock recovery: if an existing file has dead-pid content AND the
recorded start_ts is older than STALE_AFTER, the process that successfully
acquired flock reclaims the lock (sets ``claimed_stale = True``).  Because
flock guarantees mutual exclusion, only one process can ever reach the
reclaim path for a given file.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import os
from pathlib import Path
from types import TracebackType
from typing import Self

STALE_AFTER = dt.timedelta(hours=6)


class LockBusy(RuntimeError):
    pass


class RunLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.claimed_stale = False
        self._acquired = False
        self._fd: int | None = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Open (creating if needed) with O_RDWR so we can read existing content
        # and write our payload.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Another process holds the exclusive flock.
            os.close(fd)
            raise LockBusy(f"lock held: {self._safe_read()!r}") from None
        # We now hold the OS-level flock exclusively.
        try:
            existing = os.read(fd, 4096).decode("utf-8", errors="replace")
        except OSError:
            existing = ""
        if existing.strip():
            # Content exists from a prior holder whose flock was released (process
            # died or exited).  Check if that prior holder would still be considered
            # "fresh" — if so, raise LockBusy defensively.
            if not self._content_is_stale(existing):
                # Live pid + recent ts: another process wrote content without
                # holding flock (or a race in the same process). Treat as busy.
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
                raise LockBusy(f"lock held: {existing!r}") from None
            self.claimed_stale = True
        # Truncate and write our payload.
        payload = (
            f"{os.getpid()}\n{dt.datetime.now(dt.UTC).isoformat()}\n".encode()
        )
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, payload)
        os.fsync(fd)
        self._fd = fd
        self._acquired = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
        if self._acquired and self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                pass
        self._acquired = False

    def _content_is_stale(self, content: str) -> bool:
        try:
            lines = content.strip().splitlines()
            pid = int(lines[0])
            start_ts = dt.datetime.fromisoformat(lines[1])
        except (ValueError, IndexError):
            return True
        if not _pid_alive(pid):
            return True  # dead PID is always stale, regardless of timestamp
        return dt.datetime.now(dt.UTC) - start_ts > STALE_AFTER

    def _safe_read(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except OSError:
            return "<unreadable>"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
