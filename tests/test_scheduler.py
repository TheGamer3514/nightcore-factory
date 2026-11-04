"""
The loop's decision-making, with `make_one` replaced by a stub.

What matters here is that no single bad cycle can take the daemon down, and
that the failure backoff both engages and disengages.
"""

from __future__ import annotations

import argparse

import pytest

import control
import nightcore
import scheduler
from errors import NoAudioSource, NoSongsLeft


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    control.reset_stop()
    monkeypatch.setattr(control, "sleep", lambda _s: True)
    yield
    control.reset_stop()


def make_args(**overrides) -> argparse.Namespace:
    base = dict(speed=1.2, fps=60, keep_work=False, interval=3600,
                exit_when_empty=False, no_upload=True, privacy="public")
    base.update(overrides)
    return argparse.Namespace(**base)


def stub_result():
    return nightcore.RenderResult(
        path=nightcore.OUT_DIR / "x.mp4",
        song=nightcore.Song(artist="A", title="B"),
        playlist_tag="pop", duration=180.0, image_id="1")


def run_cycles(monkeypatch, outcomes, **arg_overrides):
    """Run one cycle per outcome, then stop. Returns the stats."""
    stream = iter(outcomes)

    def fake_make_one(_args):
        try:
            outcome = next(stream)
        except StopIteration:
            control.request_stop()
            return stub_result()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(nightcore, "make_one", fake_make_one)

    stats = scheduler.LoopStats()
    args = make_args(**arg_overrides)
    for _ in range(len(outcomes)):
        if scheduler._one_cycle(args, None, None, stats) is False:
            break
    return stats


def test_a_failing_cycle_does_not_stop_the_loop(monkeypatch):
    stats = run_cycles(monkeypatch, [
        NoAudioSource("nothing on youtube"),
        stub_result(),
        RuntimeError("ffmpeg exploded"),
        stub_result(),
    ])
    assert stats.failures == 2


def test_consecutive_failures_reset_on_success(monkeypatch):
    """
    The backoff is for "something is systematically broken", not for "three
    unlucky songs since Tuesday". One success has to clear it.
    """
    stats = run_cycles(monkeypatch, [
        RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom"),
    ])
    assert stats.consecutive_failures == 3

    stats2 = run_cycles(monkeypatch, [
        RuntimeError("boom"), RuntimeError("boom"), stub_result(),
    ])
    assert stats2.consecutive_failures == 0
    assert stats2.failures == 2


def test_backoff_threshold_is_reached_after_five(monkeypatch):
    stats = run_cycles(monkeypatch, [RuntimeError("boom")] * 5)
    assert stats.consecutive_failures >= scheduler.FAILURE_BACKOFF_THRESHOLD


def test_no_songs_left_is_not_a_failure(monkeypatch):
    """
    Charts turn over daily. Treating an exhausted chart as an error would
    trip the backoff and then miss tomorrow's refresh by three hours.
    """
    stats = run_cycles(monkeypatch, [NoSongsLeft("chart used up")])
    assert stats.failures == 0
    assert stats.consecutive_failures == 0


def test_exit_when_empty_ends_the_loop(monkeypatch):
    stream = iter([NoSongsLeft("done")])
    monkeypatch.setattr(nightcore, "make_one",
                        lambda _a: (_ for _ in ()).throw(next(stream)))
    stats = scheduler.LoopStats()
    assert scheduler._one_cycle(make_args(exit_when_empty=True),
                                None, None, stats) is False


def test_cancellation_ends_the_loop(monkeypatch):
    monkeypatch.setattr(nightcore, "make_one",
                        lambda _a: (_ for _ in ()).throw(
                            __import__("errors").Cancelled("ctrl-c")))
    stats = scheduler.LoopStats()
    assert scheduler._one_cycle(make_args(), None, None, stats) is False
    assert stats.failures == 0


def test_quota_exhausted_mid_upload_is_not_our_failure(monkeypatch, tmp_path):
    """
    The video is rendered and verified; only the publish step ran out of
    budget. Counting that as a failure would eventually trip the backoff on a
    pipeline that is working perfectly.
    """
    import youtube
    from errors import QuotaExhausted

    monkeypatch.setattr(nightcore, "make_one", lambda _a: stub_result())
    monkeypatch.setattr(scheduler, "publish",
                        lambda *a, **k: (_ for _ in ()).throw(
                            QuotaExhausted("spent")))
    stats = scheduler.LoopStats()
    sentinel = object()
    scheduler._one_cycle(make_args(no_upload=False), sentinel, None, stats)
    assert stats.failures == 0
    assert stats.published == 0
