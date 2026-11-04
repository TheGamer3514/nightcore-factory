"""
Cancellation and the single-instance lock.

The lock test forks a real second process rather than calling `acquire_lock`
twice in one process: on POSIX, `flock` is held per open file description, so
a same-process second acquire on a *different* handle would fail, but the
thing we actually need to prevent is a second `python nightcore.py --loop`.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

import control
from errors import Cancelled

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def clean_flag():
    control.reset_stop()
    yield
    control.reset_stop()


# ── Cancellation ────────────────────────────────────────────────────────────

def test_sleep_returns_immediately_when_stopped():
    """
    An hour-long sleep between cycles must not mean an hour-long Ctrl-C.

    This is the reason the loop uses Event.wait rather than time.sleep, and
    it is invisible in any test that doesn't measure the clock.
    """
    control.request_stop()
    start = time.monotonic()
    slept_fully = control.sleep(30)
    assert time.monotonic() - start < 1.0
    assert slept_fully is False


def test_sleep_is_interrupted_mid_wait():
    timer = threading.Timer(0.1, control.request_stop)
    timer.start()
    start = time.monotonic()
    assert control.sleep(10) is False
    assert time.monotonic() - start < 3.0
    timer.cancel()


def test_check_cancelled_raises():
    control.request_stop()
    with pytest.raises(Cancelled):
        control.check_cancelled()


def test_check_cancelled_is_silent_when_running():
    control.check_cancelled()


# ── Lock ────────────────────────────────────────────────────────────────────

HOLDER = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, %r)
    import control
    from pathlib import Path
    with control.acquire_lock(Path(sys.argv[1])):
        print("held", flush=True)
        time.sleep(30)
""")


def test_second_process_cannot_take_the_lock(tmp_path):
    lock = tmp_path / "nightcore.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER % str(ROOT), str(lock)],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(control.AlreadyRunning):
            with control.acquire_lock(lock):
                pass
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_lock_is_released_on_exit(tmp_path):
    lock = tmp_path / "nightcore.lock"
    with control.acquire_lock(lock):
        pass
    with control.acquire_lock(lock):
        pass   # no exception: the first one let go


def test_lock_survives_a_stale_pid_file(tmp_path):
    """
    A hard crash leaves the PID file behind but not the lock — the kernel
    drops that. A PID-file scheme would need a staleness heuristic here and
    would strand the daemon forever after one VM reset.
    """
    lock = tmp_path / "nightcore.lock"
    control._pid_path(lock).write_text("999999\n")
    with control.acquire_lock(lock):
        pass   # acquired despite the stale PID


def test_lock_records_the_holding_pid_in_a_sibling_file(tmp_path):
    """
    The PID lives next to the lock, not inside it: msvcrt's lock is mandatory,
    so a PID stored in the locked byte is unreadable exactly while someone
    holds it.
    """
    import os
    lock = tmp_path / "nightcore.lock"
    with control.acquire_lock(lock):
        assert control._pid_path(lock).read_text().strip() == str(os.getpid())
    assert not control._pid_path(lock).exists()


def test_blocked_message_names_the_holder(tmp_path):
    lock = tmp_path / "nightcore.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER % str(ROOT), str(lock)],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(control.AlreadyRunning) as caught:
            with control.acquire_lock(lock):
                pass
        assert str(holder.pid) in str(caught.value)
    finally:
        holder.kill()
        holder.wait(timeout=10)
