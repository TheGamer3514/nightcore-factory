"""
The continuous loop: one video an hour, unattended, until told to stop.

Design notes that are easy to get wrong and expensive to debug at 3am:

  * The interval is measured from the *start* of a cycle. Sleeping a fixed
    hour after each one would let a 70-minute render push every subsequent
    upload later and later until the schedule has drifted into the night.
  * Quota is checked before the render, not after. There is no point spending
    twenty minutes of a slow box's evening on a video that cannot be published
    for another nine hours — and a song consumed by a render we then throw
    away is a song burned for nothing.
  * Nothing reaches the ledger until the video has rendered, verified and
    (when publishing) uploaded. Every abort path is therefore a no-op: the
    song stays eligible and the next cycle simply tries again.
"""

from __future__ import annotations

import argparse
import time
import traceback
from dataclasses import dataclass

import control
import nightcore
from errors import (Cancelled, NightcoreError, NoSongsLeft,
                    QuotaExhausted)

# After this many failures in a row, something is broken in a way that another
# attempt in an hour will not fix — a stale yt-dlp, a revoked token, a full
# disk. Back right off rather than grinding the box all night.
FAILURE_BACKOFF_THRESHOLD = 5
FAILURE_BACKOFF_SECONDS = 4 * 60 * 60


@dataclass
class LoopStats:
    cycles: int = 0
    published: int = 0
    failures: int = 0
    consecutive_failures: int = 0


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(message: str) -> None:
    print(f"[{_stamp()}] {message}", flush=True)


def run_loop(args: argparse.Namespace) -> int:
    """
    Drive `make_one` (and the upload) forever. Returns a process exit code.

    Zero means "the operator stopped us" or "we ran out of work", both of
    which are normal endings. Non-zero is reserved for never having managed
    to start.
    """
    control.install_signal_handlers()

    # Take the lock before anything else. A second instance should discover
    # it is redundant in milliseconds, not after an OAuth round-trip.
    try:
        lock = control.acquire_lock(nightcore.STATE_DIR / "nightcore.lock")
        lock.__enter__()
    except control.AlreadyRunning as exc:
        log(f"not starting: {exc}")
        return 1

    try:
        return _run_locked(args)
    finally:
        lock.__exit__(None, None, None)


def _run_locked(args: argparse.Namespace) -> int:
    quota = None
    uploader = None
    if not args.no_upload:
        # Imported lazily: a --no-upload run should not need the Google
        # libraries installed at all.
        import youtube
        quota = youtube.QuotaLedger.load(nightcore.STATE_DIR / "quota.json")
        uploader = youtube.Uploader(
            client_secret=nightcore.STATE_DIR / "client_secret.json",
            token_path=nightcore.STATE_DIR / "yt_token.json",
            privacy=args.privacy,
        )
        # Fail fast on a missing token: better to say so now than at the end
        # of the first render, an hour from now, with a song already spent.
        uploader.authorise()
        log(f"youtube: signed in, {quota.remaining} quota units left today "
            f"({quota.remaining // quota.cost} uploads)")

    stats = LoopStats()
    log(f"loop started — every {args.interval}s, "
        f"{'publishing' if uploader else 'render only'}. Ctrl-C to stop.")

    while not control.stopping():
        cycle_start = time.monotonic()
        stats.cycles += 1

        if quota is not None and not quota.can_upload():
            wait = quota.seconds_until_reset()
            log(f"quota spent ({quota.units_spent}/{quota.budget}); "
                f"sleeping {wait / 3600:.1f}h until the Pacific reset")
            control.sleep(wait)
            continue

        if _one_cycle(args, uploader, quota, stats) is False:
            break

        elapsed = time.monotonic() - cycle_start
        backing_off = stats.consecutive_failures >= FAILURE_BACKOFF_THRESHOLD
        interval = FAILURE_BACKOFF_SECONDS if backing_off else args.interval
        if backing_off:
            log(f"{stats.consecutive_failures} failures in a row — "
                f"backing off to {interval / 3600:.0f}h")

        wait = max(0.0, interval - elapsed)
        if wait and not control.stopping():
            log(f"cycle took {elapsed / 60:.1f}m; next in {wait / 60:.0f}m")
            control.sleep(wait)

    log(f"stopped after {stats.cycles} cycles "
        f"({stats.published} published, {stats.failures} failed)")
    return 0


def _one_cycle(args: argparse.Namespace, uploader, quota,
               stats: LoopStats) -> bool | None:
    """
    Run one render-and-publish. Returns False to end the loop entirely.

    Every exception is caught here rather than in `run_loop`, so that a single
    bad song can never take the daemon down — the whole point of the thing is
    that it is still running tomorrow.
    """
    try:
        result = nightcore.make_one(args)
    except Cancelled:
        log("cancelled mid-render; nothing committed")
        return False
    except NoSongsLeft as exc:
        log(f"no songs available: {exc}")
        if args.exit_when_empty:
            return False
        return None                     # charts turn over; try again later
    except NightcoreError as exc:
        stats.failures += 1
        stats.consecutive_failures += 1
        log(f"cycle failed: {exc}")
        return None
    except Exception:
        stats.failures += 1
        stats.consecutive_failures += 1
        log("cycle failed unexpectedly:\n" + traceback.format_exc())
        return None

    if uploader is None:
        stats.consecutive_failures = 0
        return None

    try:
        publish(result, uploader, quota)
    except Cancelled:
        log("cancelled mid-upload; the rendered file is still in ./out")
        return False
    except QuotaExhausted as exc:
        # Not a failure of ours. The video is rendered and on disk; the loop
        # will sleep to the reset on its next pass through.
        log(f"{exc} — {result.path.name} left in ./out, upload it later")
        return None
    except Exception as exc:
        stats.failures += 1
        stats.consecutive_failures += 1
        log(f"upload failed: {exc}")
        return None

    stats.published += 1
    stats.consecutive_failures = 0
    return None


def publish(result: "nightcore.RenderResult", uploader, quota) -> str:
    """Upload one finished render and record the resulting video id."""
    import youtube

    metadata = youtube.VideoMetadata.build(
        artist=result.song.artist,
        title=result.song.title,
        playlist_tag=result.playlist_tag,
    )
    video_id = uploader.upload(result.path, metadata, quota=quota)

    # Write the id back into the ledger entry the render already created, so
    # `--list-used` can show what went where.
    ledger = nightcore.Ledger.load(nightcore.LEDGER_PATH)
    entry = ledger.songs.get(result.song.key)
    if entry is not None:
        entry["youtube_id"] = video_id
        if result.playlist_tag:
            entry["playlist_tag"] = result.playlist_tag
        ledger.save()
    return video_id
