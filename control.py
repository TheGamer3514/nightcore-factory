"""
Cancellation and the single-instance lock.

Two separate concerns, but both are "how the process relates to the outside
world", and both are needed by `nightcore` *and* `scheduler`. Keeping them here
is what stops those two importing each other.

The cancellation flag is a module-level Event rather than something passed
around because the thing that most needs to see it — the subprocess runner
deep inside the render — is five call frames below anything that would have a
handle on it.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from errors import Cancelled

# ─────────────────────────────────────────────────────────────────────────────
# Cancellation
# ─────────────────────────────────────────────────────────────────────────────

_STOP = threading.Event()
_hard_exit_armed = False


def stopping() -> bool:
    return _STOP.is_set()


def request_stop() -> None:
    _STOP.set()


def reset_stop() -> None:
    """Clear the flag. Exists for tests; nothing in production un-cancels."""
    global _hard_exit_armed
    _STOP.clear()
    _hard_exit_armed = False


def check_cancelled() -> None:
    if _STOP.is_set():
        raise Cancelled("cancelled by operator")


def sleep(seconds: float) -> bool:
    """
    Interruptible sleep. Returns True if it slept the whole time.

    `Event.wait` rather than `time.sleep` so that Ctrl-C during the hour
    between cycles takes effect now instead of in fifty minutes.
    """
    return not _STOP.wait(max(0.0, seconds))


def install_signal_handlers() -> None:
    """
    First signal asks for a graceful stop; a second one exits on the spot.

    The graceful path can still take a few seconds — an ffmpeg child has to be
    terminated and a partial file removed — and an operator who is done waiting
    deserves a way out that doesn't involve the task manager.
    """

    def handle(signum, _frame):  # noqa: ANN001
        global _hard_exit_armed
        if _hard_exit_armed:
            print("\n[stop] second signal — exiting immediately", flush=True)
            os._exit(130)
        _hard_exit_armed = True
        print(f"\n[stop] signal {signum} — finishing up, press again to force",
              flush=True)
        _STOP.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle)
        except (ValueError, OSError):
            # Not the main thread, or the platform doesn't have it. Neither is
            # fatal: the flag can still be set programmatically.
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Single-instance lock
# ─────────────────────────────────────────────────────────────────────────────

class AlreadyRunning(RuntimeError):
    """Another process holds the lock."""


@contextmanager
def acquire_lock(path: Path) -> Iterator[None]:
    """
    Hold an exclusive, non-blocking lock on `path` for the duration of the
    block, or raise AlreadyRunning.

    Non-blocking on purpose. Waiting would just queue up a second render behind
    the first, on a box that can barely afford the first one.

    The lock is an OS-level file lock, not a PID file, so it is released by the
    kernel even if we are killed with SIGKILL or the VM is reset. A PID file
    alone strands the lock on a hard crash and needs staleness heuristics that
    are wrong exactly when you need them.

    The PID goes in a *sibling* file rather than in the lock itself, because
    `msvcrt.locking` is a mandatory lock: on Windows the locked byte cannot be
    read by anyone, the holder included, so a PID stored there would be
    unreadable precisely while someone is holding it — which is the only time
    you'd ever want to look.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        _lock_file(handle)
    except AlreadyRunning:
        handle.close()
        raise AlreadyRunning(_holder_message(path)) from None

    pid_path = _pid_path(path)
    try:
        pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
        yield
    finally:
        try:
            _unlock_file(handle)
        finally:
            handle.close()
            pid_path.unlink(missing_ok=True)


if sys.platform == "win32":
    import msvcrt

    def _lock_file(handle) -> None:
        # Seek first: msvcrt locks a byte range *from the current position*,
        # and "a+" opens at EOF. Without this, two processes would lock two
        # different bytes of the same file and both think they won.
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise AlreadyRunning() from None

    def _unlock_file(handle) -> None:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:
    import fcntl

    def _lock_file(handle) -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise AlreadyRunning() from None

    def _unlock_file(handle) -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


def _pid_path(lock_path: Path) -> Path:
    return lock_path.with_suffix(lock_path.suffix + ".pid")


def _holder_message(lock_path: Path) -> str:
    try:
        pid = _pid_path(lock_path).read_text(encoding="utf-8").strip()
    except OSError:
        pid = ""
    return (f"another instance is already running (pid {pid or 'unknown'}); "
            f"lock held on {lock_path}")
