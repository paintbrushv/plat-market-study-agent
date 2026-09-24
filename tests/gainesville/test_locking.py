from __future__ import annotations

import datetime as dt
import fcntl
import os
import threading
import time
from pathlib import Path

import pytest
from etl.gainesville.locking import LockBusy, RunLock


def test_lock_acquired_and_released(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    with RunLock(lock_path):
        assert lock_path.exists()
    assert not lock_path.exists()


def test_lock_busy_raises(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    with RunLock(lock_path):
        with pytest.raises(LockBusy):
            with RunLock(lock_path):
                pass


def test_stale_lock_auto_claimed(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    # Write a lock for a long-dead pid with old timestamp.
    stale = (
        f"99999999\n"
        f"{(dt.datetime.now(dt.UTC) - dt.timedelta(hours=12)).isoformat()}\n"
    )
    lock_path.write_text(stale)
    with RunLock(lock_path) as lock:
        assert lock.claimed_stale is True


def test_dead_pid_with_recent_timestamp_is_stale(tmp_path: Path) -> None:
    """A crashed process leaves a dead PID with a recent timestamp.

    The OS flock is released on crash, so the next caller acquires it.
    Dead PID should always be treated as stale — even with a 1-minute-old
    timestamp — so the lock is claimed immediately rather than blocking for
    up to STALE_AFTER hours.
    """
    lock_path = tmp_path / "test.lock"
    # 99999999 is virtually certain to not be a live PID; timestamp is fresh (1m ago).
    fresh_ts = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)).isoformat()
    lock_path.write_text(f"99999999\n{fresh_ts}\n")
    with RunLock(lock_path) as lock:
        assert lock.claimed_stale is True


def test_fresh_lock_with_live_pid_is_not_stolen(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    # Write the current process's pid with a fresh timestamp; no flock held.
    # Our flock acquire succeeds (no holder), we read content, see live pid +
    # fresh ts, and raise LockBusy defensively.
    fresh = f"{os.getpid()}\n{dt.datetime.now(dt.UTC).isoformat()}\n"
    lock_path.write_text(fresh)
    with pytest.raises(LockBusy):
        with RunLock(lock_path):
            pass


def test_concurrent_acquisition_only_one_wins(tmp_path: Path) -> None:
    """Two threads racing to acquire the same lock: exactly one wins, one raises LockBusy."""
    lock_path = tmp_path / "race.lock"
    barrier = threading.Barrier(2)
    results: list[str] = []  # 'acquired' or 'busy'
    errors: list[BaseException] = []

    def try_acquire() -> None:
        try:
            barrier.wait()  # maximize contention
            with RunLock(lock_path):
                results.append("acquired")
                # Hold briefly so the other thread has a chance to attempt
                # and observe the held lock; do NOT call barrier.wait() here
                # because the losing thread exits via the except branch and
                # would never reach a second barrier.
                time.sleep(0.05)
        except LockBusy:
            results.append("busy")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=try_acquire)
    t2 = threading.Thread(target=try_acquire)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, f"unexpected errors: {errors}"
    assert sorted(results) == ["acquired", "busy"], (
        f"Expected exactly one winner and one loser, got: {results}"
    )


def test_flock_exclusive_semantics(tmp_path: Path) -> None:
    """Verify that RunLock actually acquires an OS-level flock.

    Open a second file descriptor on the same path and attempt LOCK_EX | LOCK_NB —
    it must fail with BlockingIOError while our RunLock holds the lock, and succeed
    after the RunLock is released.

    Note: flock() is per-(fd, process).  In CPython a single process can open
    multiple fds to the same file; a second LOCK_EX on a different fd in the
    same process *will* block (Linux/BSD agree: each open() creates a new flock
    scope even within the same process).
    """
    lock_path = tmp_path / "flock_test.lock"
    with RunLock(lock_path):
        # RunLock holds LOCK_EX on its internal fd.
        probe_fd = os.open(lock_path, os.O_RDONLY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe_fd)

    # After RunLock.__exit__, the flock should be released.
    # Recreate the file (RunLock unlinks on exit) for the post-exit check.
    lock_path.touch()
    probe_fd2 = os.open(lock_path, os.O_RDONLY)
    try:
        # Must not raise — lock was released.
        fcntl.flock(probe_fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(probe_fd2, fcntl.LOCK_UN)
    finally:
        os.close(probe_fd2)
