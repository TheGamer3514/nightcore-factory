"""
Shared exception types.

A one-shot script can say `sys.exit("no songs left")` and be done. A daemon
cannot: the loop has to tell "this song didn't work out, try another" apart
from "the art library is empty, nothing will ever work" apart from "the
operator pressed Ctrl-C". So every giving-up path in the pipeline raises one of
these instead, and the two entrypoints — `main()` and the scheduler — decide
what each one means.
"""

from __future__ import annotations


class NightcoreError(Exception):
    """Base for every expected, diagnosable failure in the pipeline."""


class PoolEmpty(NightcoreError):
    """The local anime art library has no usable images."""


class NoSongsLeft(NightcoreError):
    """Every song source is exhausted or unreachable."""


class NoAudioSource(NightcoreError):
    """No acceptable audio upload exists for this particular song."""


class NoTagMatch(NightcoreError):
    """
    The song maps to none of the network's playlist hashtags.

    Per the member guidance a video must carry exactly one, so a song we can't
    classify is a song we don't publish.
    """


class QuotaExhausted(NightcoreError):
    """Today's YouTube API quota can't fund another upload."""


class UploadFailed(NightcoreError):
    """A non-retryable upload failure — bad credentials, rejected video, etc."""


class Cancelled(NightcoreError):
    """The operator asked us to stop. Not an error; just not a completion."""
