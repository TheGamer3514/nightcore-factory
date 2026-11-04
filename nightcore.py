#!/usr/bin/env python3
"""
nightcore.py — one-shot nightcore video factory.

Pipeline
--------
  1. pick a charting song that hasn't been used before    (SongCatalogue + Ledger)
  2. fetch clean audio for it — never a music video       (AudioFetcher / yt-dlp)
  3. nightcore it: +20% speed AND pitch, then loudness-normalise  (Nightcorifier)
  4. draw a random image from the local vetted art pool   (ArtPool)
  5. render a 1080p60 video with a beat-reactive visualiser (VideoRenderer / ffmpeg)

Everything it produces lands in ./out, the anime art library lives in ./assets,
and everything it remembers lives in ./state.

Requires: ffmpeg, ffprobe, yt-dlp on PATH. No API keys needed — see SONG SOURCES.

Usage:    python nightcore.py                 # make one video
          python nightcore.py --build-pool    # (re)stock the anime art library
          python nightcore.py --help          # all the knobs
"""

from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import shutil
import subprocess
import sys
import textwrap
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent import futures
from dataclasses import dataclass, field
from pathlib import Path

import envfile  # noqa: F401  (must precede the modules that read os.environ)
import artcheck
import control
import tagging
from sources import (Song, SongCatalogue, SpotifyPlaylist,
                     seasonal_playlist)
from errors import (Cancelled, NightcoreError, NoAudioSource, NoSongsLeft,
                    NoTagMatch, PoolEmpty)

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "out"
WORK_DIR = ROOT / "work"
STATE_DIR = ROOT / "state"
ART_DIR = ROOT / "assets" / "anime"
LEDGER_PATH = STATE_DIR / "ledger.json"
ART_MANIFEST = STATE_DIR / "art_pool.json"

USER_AGENT = "nightcore-tool/1.0 (+personal use)"

# Burned into a corner of every frame. Set NIGHTCORE_WATERMARK to your own
# channel handle, or to an empty string for none.
WATERMARK = os.environ.get("NIGHTCORE_WATERMARK", "@sillynightcore")

# Follow the Nightcore Network's rules (nightcore.com): every song must resolve
# to a playlist tag, and the description carries the network blurb and tag.
# NIGHTCORE_NETWORK=0 drops all of that — no tag lookup, no song rejected for
# lacking a tag, no seasonal playlists, no #playlist_ hashtag, no network link.
NETWORK_ENABLED = envfile.flag("NIGHTCORE_NETWORK", True)

# Size of the local anime art library. Stocking it once means a render never
# waits on the booru. The per-image ceiling is ART_MAX_EDGE, which artcheck
# derives from the zoom geometry rather than picking by feel — capping too low
# makes it arithmetically impossible for wide art to avoid upscaling.
ART_POOL_SIZE = 100
ART_MAX_EDGE = artcheck.MAX_EDGE

# How many images to ingest at once. Each ingest is a download followed by an
# ffmpeg re-encode and a pixel check, so it alternates between waiting on the
# network and waiting on a core; overlapping them is most of the win. Above
# roughly 2x the core count the CPU stages just queue up behind each other.
POOL_JOBS = int(os.environ.get("NIGHTCORE_POOL_JOBS") or 6)

# Nightcore is a *tape-speed* effect: pitch and tempo move together.
SPEED = 1.20

# Output spec. The wiki requires "at least 1080p"; the rest are our own
# over-delivery on the things it only describes qualitatively.
#
# The frame geometry lives in artcheck because that module has to reason about
# it to decide whether a picture survives the crop. Importing it here keeps one
# definition rather than two that drift.
WIDTH, HEIGHT = artcheck.WIDTH, artcheck.HEIGHT
FPS = 60
# Tuned for the deployment target: 4 virtual cores of a Xeon E5-2670 v2
# (Ivy Bridge — AVX but no AVX2). `medium` on that silicon runs several times
# slower than realtime; on this content — a near-static plate plus bars —
# veryfast/CRF 20 is visually indistinguishable at roughly 4x the throughput.
CRF = 20
X264_PRESET = "veryfast"
ENCODE_THREADS = int(os.environ.get("NIGHTCORE_THREADS") or 4)

# Quality of the throwaway 24-second loop clip (see VideoRenderer). High
# enough that the final encode cannot see it, cheap enough to be nearly free.
LOOP_CRF = 12
LOOP_PRESET = "ultrafast"
AUDIO_BITRATE = "320k"
SAMPLE_RATE = 48000

# Composition is built oversized, then zoompan crops down to 1080p. This keeps
# the slow "breathing" zoom permanently *down*-sampling, so it never softens.
COMP_W, COMP_H = artcheck.COMP_W, artcheck.COMP_H
ZOOM_BASE = artcheck.ZOOM_BASE          # 1.25 → 1:1 pixel mapping at rest
ZOOM_AMPLITUDE = artcheck.ZOOM_AMPLITUDE   # subtle, per "stable animations"
ZOOM_PERIOD_S = artcheck.ZOOM_PERIOD_S     # whole cycle → seamless loop

VIS_HEIGHT = artcheck.VIS_HEIGHT    # visualiser band at the bottom

# Text scrim. It ramps in *above* the text and then holds at full strength, so
# every glyph sits on uniform darkness. Ramping all the way to the bottom edge
# instead would leave the title floating at ~0 alpha, i.e. no contrast at all.
SCRIM_TOP = 400                     # y where the darkening begins
SCRIM_RAMP = 300                    # px to reach full strength (ends above title)
SCRIM_ALPHA = 0.66                  # peak opacity

# Fonts. Resolved from candidates rather than hardcoded, because the render
# box is Linux and the machine this gets developed on is Windows — a single
# hardcoded path means one of the two fails at the loop pass with an opaque
# drawtext error. Override with NIGHTCORE_FONT_BOLD / NIGHTCORE_FONT_REG.
def _first_font(*candidates: str) -> str:
    for path in candidates:
        if path and Path(path).exists():
            return path
    return candidates[-1]      # let ffmpeg produce the error, with a real path


FONT_BOLD = _first_font(
    os.environ.get("NIGHTCORE_FONT_BOLD", ""),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
)
FONT_REG = _first_font(
    os.environ.get("NIGHTCORE_FONT_REG", ""),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/seguisb.ttf",
)

# Accepted song length *before* the speed-up. Filters out radio edits that are
# actually 20s previews, hour-long "1 hour version" uploads, and live streams.
MIN_SRC_SECONDS = 100
MAX_SRC_SECONDS = 420

# ─────────────────────────────────────────────────────────────────────────────
# Song sources
# ─────────────────────────────────────────────────────────────────────────────
#
# Four of them, pooled rather than ranked — see sources.py for why, and for
# the credentials that unlock the full Spotify playlist instead of the first
# hundred tracks.

# How many different songs to try before giving up on a run.
SONG_ATTEMPTS = 6


# ─────────────────────────────────────────────────────────────────────────────
# Ledger — the "no duplicates" memory
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Ledger:
    """Persistent record of what we've already used, so nothing repeats."""

    path: Path
    songs: dict[str, dict] = field(default_factory=dict)
    skipped: dict[str, dict] = field(default_factory=dict)
    images: set[str] = field(default_factory=set)
    videos: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, path: Path) -> "Ledger":
        if not path.exists():
            return cls(path=path)
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            path=path,
            songs=data.get("songs", {}),
            skipped=data.get("skipped", {}),
            images=set(data.get("images", [])),
            videos=set(data.get("videos", [])),
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "songs": self.songs,
                    "skipped": self.skipped,
                    "images": sorted(self.images),
                    "videos": sorted(self.videos),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    # Songs are keyed twice: by "artist - title" and by the source video id, so
    # the same track can't sneak back in under a different upload.
    #
    # A permanently-skipped song counts as used. Its unsuitability is a fact
    # about the song, not about today, so reconsidering it every hour would
    # just burn two API calls an hour forever.
    def song_used(self, song: Song) -> bool:
        return song.key in self.songs or song.key in self.skipped

    def seasonal_used_today(self, tag: str) -> bool:
        today = time.strftime("%Y-%m-%d")
        return any(
            entry.get("playlist_tag") == tag
            and entry.get("used_at", "").startswith(today)
            for entry in self.songs.values()
        )

    def record_skip(self, song: Song, reason: str) -> None:
        self.skipped[song.key] = {
            "artist": song.artist,
            "title": song.title,
            "reason": reason,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def source_used(self, video_id: str) -> bool:
        return any(v.get("video_id") == video_id for v in self.songs.values())

    def record_song(self, song: Song, video_id: str, source_title: str,
                    playlist_tag: str | None = None) -> None:
        entry = {
            "artist": song.artist,
            "title": song.title,
            "video_id": video_id,
            "source_title": source_title,
            "used_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if playlist_tag is not None:
            entry["playlist_tag"] = playlist_tag
        self.songs[song.key] = entry

    def image_used(self, image_id: str) -> bool:
        return image_id in self.images


# ─────────────────────────────────────────────────────────────────────────────
# Small shared helpers
# ─────────────────────────────────────────────────────────────────────────────

# How long a terminated child gets to exit before we stop being polite.
KILL_GRACE_SECONDS = 5.0


def _terminate(proc: subprocess.Popen) -> None:
    """Ask a child to stop, then insist."""
    proc.terminate()
    try:
        proc.communicate(timeout=KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass


def run(cmd: list[str], *, capture: bool = False, quiet: bool = False) -> str:
    """
    Run a subprocess, raising with useful context on failure.

    Popen-and-poll rather than `subprocess.run`, purely so that Ctrl-C works.
    A render is a single ffmpeg call that owns the CPU for minutes; under a
    blocking `run()` the cancellation flag would go unread until it finished,
    which is precisely the wait the operator was trying to cut short.

    The polling is done with `communicate(timeout=...)` rather than `poll()`
    so the pipes keep draining: ffmpeg with `-stats` is chatty enough to fill
    a stderr buffer and deadlock against a parent that is only checking
    returncode.
    """
    control.check_cancelled()
    if not quiet:
        print(f"  $ {' '.join(cmd[:6])}{' ...' if len(cmd) > 6 else ''}")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    stdout = stderr = ""
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if control.stopping():
                _terminate(proc)
                raise Cancelled(f"cancelled during: {cmd[0]}")

    if proc.returncode != 0:
        tail = (stderr or "")[-1500:]
        raise RuntimeError(f"command failed ({proc.returncode}): {cmd[0]}\n{tail}")
    return stdout or ""


def http_text(url: str, *, headers: dict[str, str] | None = None,
              timeout: int = 30) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def http_json(url: str, *, headers: dict[str, str] | None = None,
              timeout: int = 30) -> object:
    return json.loads(http_text(url, headers=headers, timeout=timeout))


def http_download(url: str, dest: Path, *, timeout: int = 60) -> Path:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp, dest.open("wb") as fh:
        shutil.copyfileobj(resp, fh)
    return dest


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[\s_-]+", "-", text) or "track"


def require_tools(*tools: str) -> None:
    missing = [t for t in tools if shutil.which(t) is None]
    if missing:
        sys.exit(f"Missing required tool(s) on PATH: {', '.join(missing)}")


# ─────────────────────────────────────────────────────────────────────────────
# 1 + 2. Song selection and audio fetching
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FetchedAudio:
    path: Path
    song: Song
    video_id: str
    source_title: str
    duration: float


class AudioFetcher:
    """Finds and downloads the best available audio stream for a Song."""

    def __init__(self, ledger: Ledger, work_dir: Path):
        self.ledger = ledger
        self.work_dir = work_dir

    # Anything whose title matches these is not the clean studio master we
    # want. Music videos are the big one: they open on dialogue, sound design
    # or a cold intro and fade out on credits, which is exactly the "different
    # intros/outros" the guidance forbids — the audio genuinely differs from
    # the released track. Karaoke, covers and pre-sped edits are equally out.
    REJECT_TITLE = re.compile(
        r"""
        official\s*(music\s*)?video | music\s*video | \bm[/.]?v\b
      | \bvideo\s*oficial\b | videoclip | \bvisuali[sz]er\b
      | karaoke | instrumental | \bcover\b | \bremix\b | \bmashup\b
      | \blive\b | \bconcert\b | \btour\b | \bperformance\b | \bacoustic\b
      | \bsession\b | \bunplugged\b | \bdemo\b | \bsnippet\b
      | nightcore | sped\s*up | slowed | reverb | \b8d\b | bass\s*boost
      | \breaction\b | \breview\b | \btrailer\b | \bteaser\b
      | behind\s*the\s*scenes | \bmaking\s*of\b
      | \d+\s*hours? | \bloop(ed)?\b | \bmix\b | \bmedley\b | \bmegamix\b
      | director'?s\s*cut          # a music-video recut, not the master
      | \d{3}\s*hz                 # "432hz" reuploads are pitch-shifted
        """,
        re.I | re.X,
    )

    def _search(self, song: Song, count: int = 12) -> list[dict]:
        raw = run(
            [
                "yt-dlp", "-J", "--flat-playlist", "--no-warnings",
                f"ytsearch{count}:{song.search_query}",
            ],
            capture=True,
        )
        return json.loads(raw).get("entries", []) or []

    @staticmethod
    def _flatten(text: str) -> str:
        """Lowercase, strip accents, reduce punctuation to single spaces."""
        text = unicodedata.normalize("NFKD", text)
        text = text.encode("ascii", "ignore").decode()
        return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()

    # Words that decorate an upload's title without changing which recording
    # it is. Stripping these is what lets "As It Was (Official Audio)" match
    # while "Ordinary Life" still doesn't match "Ordinary".
    QUALIFIERS = {
        "official", "audio", "video", "lyric", "lyrics", "hd", "hq", "4k",
        "full", "song", "music", "remastered", "remaster", "explicit",
        "visualizer", "visualiser", "new", "original", "version", "track",
    }

    @classmethod
    def _core_title(cls, text: str, artist: str = "") -> str:
        """
        Reduce an upload title to just the song name.

        Drops bracketed decorations, the artist's own name, any "feat." tail,
        and trailing marketing words — leaving something that can be compared
        for equality. Equality is the point: a substring test says "Ordinary
        Life" contains "Ordinary" and happily grabs the wrong track.
        """
        text = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", text)
        flat = cls._flatten(text)
        flat = re.split(r"\b(?:feat|ft|featuring|with|prod)\b", flat)[0]

        artist_flat = cls._flatten(artist)
        if artist_flat:
            flat = flat.replace(artist_flat, " ")

        tokens = flat.split()
        while tokens and tokens[0] in cls.QUALIFIERS:
            tokens.pop(0)
        while tokens and tokens[-1] in cls.QUALIFIERS:
            tokens.pop()
        return " ".join(tokens)

    def _is_same_song(self, entry: dict, song: Song) -> bool:
        """
        Does this result actually correspond to the song we asked for?

        Search engines answer "Harry Styles As It Was audio" with the whole
        artist catalogue, and the scoring below happily crowns "Adore You
        (Official Audio)" from Harry's own channel — right artist, right
        channel, wrong song. Nothing downstream would ever notice, so the
        title has to be checked explicitly.
        """
        wanted = self._core_title(song.title)
        if not wanted:
            return False
        if self._core_title(entry.get("title") or "",
                            song.primary_artist) != wanted:
            return False

        # The lead artist must show up too — plenty of different artists have
        # a song called "Alone".
        artist = self._flatten(song.primary_artist)
        haystack = (f"{self._flatten(entry.get('title') or '')} "
                    f"{self._flatten(entry.get('channel') or '')}")
        return not artist or artist in haystack

    def _acceptable(self, entry: dict, song: Song) -> bool:
        duration = entry.get("duration")
        if not duration or not (MIN_SRC_SECONDS <= duration <= MAX_SRC_SECONDS):
            return False
        if entry.get("live_status") in {"is_live", "is_upcoming"}:
            return False
        if self.REJECT_TITLE.search(entry.get("title") or ""):
            return False
        if not self._is_same_song(entry, song):
            return False
        return not self.ledger.source_used(entry.get("id", ""))

    @staticmethod
    def _score(entry: dict, song: Song) -> int:
        """
        Rank survivors by how likely they are to be the untouched master.

        WHO uploaded it outweighs what it's called. A random reupload titled
        "(Audio)" is a re-encode of unknown provenance, while a plainly-titled
        track on the artist's own channel is the real master — so the channel
        terms here are worth several times the title terms. Ranking on title
        keywords alone puts channels named "aa" and "full album channel" above
        the artist themselves, which is how you end up with lossy junk.
        """
        def norm(s: str) -> str:
            return re.sub(r"[^a-z0-9]", "", s.lower())

        title = (entry.get("title") or "").lower()
        channel = entry.get("channel") or entry.get("uploader") or ""
        chan, artist = norm(channel), norm(song.primary_artist)

        score = 0
        if artist and chan == artist + "topic":
            # YouTube's own auto-generated art track, ingested straight from
            # the distributor. The cleanest source that exists — though normal
            # search rarely surfaces these, so don't count on it.
            score += 120
        elif artist and chan == artist:
            score += 80                       # the artist's own channel
        elif artist and artist in chan:
            score += 70 if "vevo" in chan else 50

        if "official audio" in title:
            score += 25
        elif "(audio)" in title or "[audio]" in title:
            score += 20
        if "lyric" in title:
            score += 8       # lyric videos normally carry the plain master
        return score

    def fetch(self, song: Song) -> FetchedAudio:
        print(f"[song] searching for: {song.label}")
        candidates = [e for e in self._search(song) if self._acceptable(e, song)]
        if not candidates:
            raise NoAudioSource(f"no acceptable source found for {song.label}")
        candidates.sort(key=lambda e: self._score(e, song), reverse=True)

        # Individual uploads go region-locked, age-gated or extractor-broken all
        # the time, so walk the ranked list rather than betting on the top hit.
        last_error: Exception | None = None
        for entry in candidates:
            video_id = entry["id"]
            print(f"[song] trying: {entry.get('title')} "
                  f"[{entry.get('channel') or entry.get('uploader')}] "
                  f"({entry.get('duration')}s, score {self._score(entry, song)})")
            try:
                stdout = run(
                    [
                        "yt-dlp",
                        # Prefer a genuinely high-bitrate stream, but degrade
                        # rather than fail if nothing that good is offered.
                        "-f", "bestaudio[abr>=160]/bestaudio/best",
                        "--no-playlist", "--no-warnings",
                        # The box is slow but the link is not; pulling
                        # fragments in parallel is free wall-clock.
                        "--concurrent-fragments", "4",
                        "--no-simulate", "--print", "after_move:filepath",
                        "-o", str(self.work_dir / f"{video_id}.%(ext)s"),
                        f"https://www.youtube.com/watch?v={video_id}",
                    ],
                    capture=True,
                )
            except RuntimeError as exc:
                print(f"[song] source unavailable, trying the next one")
                last_error = exc
                continue

            path = Path(stdout.strip().splitlines()[-1])
            if not path.exists():
                last_error = RuntimeError(f"yt-dlp reported {path}, not on disk")
                continue

            return FetchedAudio(
                path=path,
                song=song,
                video_id=video_id,
                source_title=entry.get("title") or song.label,
                duration=float(entry.get("duration") or 0),
            )

        raise NoAudioSource(f"every source for {song.label} failed") from last_error


# ─────────────────────────────────────────────────────────────────────────────
# 3. The nightcore transform
# ─────────────────────────────────────────────────────────────────────────────

class Nightcorifier:
    """Speeds audio up by SPEED, raising pitch with it, then normalises it."""

    def __init__(self, speed: float = SPEED):
        self.speed = speed

    def build_filter(self) -> str:
        return ",".join([
            # Land on a known rate first so the asetrate maths is exact.
            f"aresample={SAMPLE_RATE}",
            # THE nightcore step. asetrate relabels the stream as faster without
            # touching samples; the following aresample plays it back at the real
            # rate, so tempo and pitch both rise by `speed` — the tape effect.
            # (atempo alone would speed it up at the original pitch: not nightcore.)
            f"asetrate={int(SAMPLE_RATE * self.speed)}",
            f"aresample={SAMPLE_RATE}:resampler=soxr:precision=20",
            # "Consistent volume levels" — one-pass EBU R128 normalisation.
            "loudnorm=I=-14:TP=-1.5:LRA=11",
            # "Clear sound, free from distortion" — catch any inter-sample peaks.
            "alimiter=limit=0.97:level=disabled",
            f"aformat=sample_fmts=s16:sample_rates={SAMPLE_RATE}:channel_layouts=stereo",
        ])

    def process(self, src: Path, dest: Path) -> Path:
        print(f"[audio] nightcore {self.speed:.2f}x (pitch + tempo)")
        run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src),
            "-af", self.build_filter(),
            "-c:a", "pcm_s16le", "-ar", str(SAMPLE_RATE), "-ac", "2",
            str(dest),
        ])
        return dest


# ─────────────────────────────────────────────────────────────────────────────
# 4. Anime artwork — a local, pre-vetted library
# ─────────────────────────────────────────────────────────────────────────────
#
# The library is stocked ONCE by `--build-pool`, then every render just draws
# from disk. That matters for more than speed: a fixed, inspectable set of
# images is something you can actually curate — delete the ones you dislike and
# they never come back — whereas fetching live means every render is a fresh
# roll of the dice against whatever the booru happens to serve that second.


@dataclass
class AnimeImage:
    image_id: str
    path: Path
    width: int
    height: int
    tags: str = ""
    source_url: str = ""

    @classmethod
    def from_manifest(cls, row: dict) -> "AnimeImage":
        return cls(
            image_id=row["image_id"],
            path=ART_DIR / row["file"],
            width=row["width"],
            height=row["height"],
            tags=row.get("tags", ""),
            source_url=row.get("source_url", ""),
        )

    def to_manifest(self) -> dict:
        return {
            "image_id": self.image_id,
            "file": self.path.name,
            "width": self.width,
            "height": self.height,
            "tags": self.tags,
            "source_url": self.source_url,
        }


class ArtPool:
    """
    A local library of vetted anime art, stocked from safebooru.

    safebooru is safe-rated by design and its API returns dimensions and tags
    inline, so unsuitable art is rejected before a single byte is downloaded.
    """

    API = "https://safebooru.org/index.php"

    # The booru understands `width:>=` and `height:>=`, so the shape and
    # resolution requirements go into the *query* rather than being filtered
    # out afterwards. That matters: an unconstrained "scenery highres" search
    # returns ~80% portrait art, of which we can use none, so we would be
    # paging through the archive four times over to find anything.
    #
    # An earlier version of this library was stocked without these and ended
    # up 72% portrait — every one of those images renders as a narrow strip
    # of art floating in blurred filler.
    SIZE_FILTER = (f"width:>={int(artcheck.MIN_VIABLE_EDGE)} "
                   f"height:>={int(artcheck.VIEW_H * artcheck.ZOOM_MAX / artcheck.MAX_UPSCALE)}")

    # Each query is a themed slice of the archive; sampling across all of them
    # is what gives the library variety rather than 100 near-identical images.
    THEMES: list[str] = [
        "scenery", "landscape", "1girl sky", "night sky", "cityscape",
        "sunset", "cherry_blossoms", "starry_sky", "ocean", "mountain",
        "cloud", "flower_field", "rain", "forest", "field", "horizon",
        "snow", "beach", "sunrise", "lake",
    ]

    BANNED_TAGS = artcheck.BANNED_TAGS

    @property
    def tag_pools(self) -> list[str]:
        return [f"{theme} absurdres {self.SIZE_FILTER}" for theme in self.THEMES]

    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        self.images: list[AnimeImage] = []
        if ART_MANIFEST.exists():
            rows = json.loads(ART_MANIFEST.read_text(encoding="utf-8"))
            # Trust the files on disk, not the manifest: deleting an image you
            # dislike should be all it takes to retire it from the library.
            self.images = [AnimeImage.from_manifest(r) for r in rows
                           if (ART_DIR / r["file"]).exists()]

    def __len__(self) -> int:
        return len(self.images)

    # ── Stocking ────────────────────────────────────────────────────────────
    def _query(self, tags: str, limit: int = 100) -> list[dict]:
        params = {
            "page": "dapi", "s": "post", "q": "index", "json": "1",
            "limit": str(limit),
            "tags": f"{tags} sort:random",
        }
        data = http_json(f"{self.API}?{urllib.parse.urlencode(params)}")
        return data if isinstance(data, list) else []

    def _suitable(self, post: dict, have: set[str]) -> tuple[bool, str]:
        """
        Judge a candidate from its JSON, before spending any bandwidth.

        Returns (ok, why-not). The reason is worth carrying: when a build
        turns up nothing usable you want to know whether the archive is dry
        or the thresholds are impossible.
        """
        if post.get("rating") != "general":
            return False, "not safe-rated"
        if str(post.get("id")) in have:
            return False, "already have it"
        if not post.get("file_url", "").lower().endswith(
                (".jpg", ".jpeg", ".png")):
            return False, "not a still image"

        reasons = artcheck.tag_verdict(html.unescape(post.get("tags") or ""))
        reasons += artcheck.metadata_verdict(int(post.get("width") or 0),
                                             int(post.get("height") or 0))
        return (not reasons), "; ".join(reasons)

    def _ingest(self, post: dict) -> AnimeImage:
        """Download one post and normalise it into the library."""
        url = post["file_url"]
        image_id = str(post["id"])
        suffix = Path(urllib.parse.urlparse(url).path).suffix or ".jpg"
        raw = WORK_DIR / f"raw-{image_id}{suffix}"
        http_download(url, raw)

        # Normalise on the way in: cap the long edge and re-encode as quality
        # JPEG. Originals run to 20 MB and 8000px, far more than a 2400px plate
        # can use — this keeps 100 images at a sane size on disk while staying
        # comfortably above what the render actually needs.
        dest = ART_DIR / f"{image_id}.jpg"
        run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(raw),
            "-vf", f"scale=w=min({ART_MAX_EDGE}\\,iw):h=min({ART_MAX_EDGE}\\,ih):"
                   f"force_original_aspect_ratio=decrease:flags=lanczos",
            "-q:v", "2", str(dest),
        ], quiet=True)
        raw.unlink(missing_ok=True)

        # The metadata checks can only see dimensions and tags. Brightness,
        # contrast and actual sharpness need the pixels, so the last word
        # belongs to the file we just wrote — and if it fails, it goes now
        # rather than sitting in the library waiting to be curated out.
        reasons = artcheck.check_file(dest)
        if reasons:
            dest.unlink(missing_ok=True)
            raise ValueError("; ".join(reasons))

        w, h = artcheck.capped_size(int(post["width"]), int(post["height"]))
        return AnimeImage(
            image_id=image_id, path=dest, width=w, height=h,
            tags=html.unescape(post.get("tags", "")), source_url=url,
        )

    def build(self, target: int = ART_POOL_SIZE,
              jobs: int = POOL_JOBS) -> None:
        """
        Top the library up to `target` images. Safe to re-run.

        Ingests run `jobs` at a time. Each one is a download, an ffmpeg
        re-encode and a pixel check: the first waits on the network, the other
        two on the CPU. Run one at a time, whichever resource isn't currently
        busy sits idle — which on a box with a fast link and slow cores is
        most of the run.

        The theme round-robin survives: each batch is drawn from one theme's
        results, so an interrupted build still leaves a varied library rather
        than forty sunsets.
        """
        ART_DIR.mkdir(parents=True, exist_ok=True)
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        have = {img.image_id for img in self.images}
        tried = set(have)      # includes failures, so they are not retried
        print(f"[pool] have {len(have)}, want {target}, {jobs} at a time")

        themes = list(self.tag_pools)
        random.shuffle(themes)
        theme_idx, barren_rounds = 0, 0
        rejected: dict[str, int] = {}
        started = time.monotonic()

        while len(have) < target and barren_rounds < len(themes) * 2:
            control.check_cancelled()
            tags = themes[theme_idx % len(themes)]
            theme_idx += 1
            try:
                posts = self._query(tags)
            except Exception as exc:
                print(f"[pool] query failed ({exc}); continuing")
                barren_rounds += 1
                continue

            usable = []
            for post in posts:
                ok, why = self._suitable(post, tried)
                if ok:
                    usable.append(post)
                elif why != "already have it":
                    # Group by the kind of problem, not the specifics, so
                    # the summary is a handful of lines rather than hundreds.
                    key = ("banned tags" if why.startswith("banned tags")
                           else why.split("(")[0].split(";")[0].strip())
                    rejected[key] = rejected.get(key, 0) + 1
            if not usable:
                barren_rounds += 1
                continue
            barren_rounds = 0

            # One batch per theme, big enough to keep every worker busy but
            # never overshooting the target.
            batch = random.sample(
                usable, min(max(jobs * 2, 4), len(usable), target - len(have)))
            for post in batch:
                tried.add(str(post["id"]))

            # Results are consumed here on the main thread, so `self.images`,
            # `have` and the progress counter need no locking.
            with futures.ThreadPoolExecutor(max_workers=jobs) as pool:
                pending = {pool.submit(self._ingest, post): post
                           for post in batch}
                for future in futures.as_completed(pending):
                    post = pending[future]
                    try:
                        image = future.result()
                    except Cancelled:
                        for other in pending:
                            other.cancel()
                        raise
                    except Exception as exc:
                        print(f"[pool] rejected #{post.get('id')}: {exc}")
                        rejected["failed pixel checks"] = \
                            rejected.get("failed pixel checks", 0) + 1
                        continue
                    self.images.append(image)
                    have.add(image.image_id)
                    print(f"[pool] {len(have):3d}/{target}  #{image.image_id} "
                          f"({image.width}x{image.height})  {tags.split()[0]}")

            # Checkpoint after each batch. A build interrupted at 90 of 150
            # should keep those 90, not lose the manifest for them.
            self.save()

        self.save()
        print(f"[pool] library now holds {len(self.images)} images in "
              f"{ART_DIR}  ({time.monotonic() - started:.0f}s)")
        if rejected:
            print("[pool] candidates turned away:")
            for why, n in sorted(rejected.items(), key=lambda kv: -kv[1]):
                print(f"         {n:4d}  {why}")
        if len(have) < target:
            print(f"[pool] NOTE: wanted {target}, got {len(have)} — the "
                  f"archive ran dry at these thresholds. Re-run to page "
                  f"further, or widen ArtPool.THEMES.")

    def save(self) -> None:
        ART_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        ART_MANIFEST.write_text(
            json.dumps([i.to_manifest() for i in self.images], indent=2),
            encoding="utf-8")

    # ── Drawing from the library ────────────────────────────────────────────
    def pick(self) -> AnimeImage:
        if not self.images:
            raise PoolEmpty("Anime art library is empty. Run:  "
                            "python nightcore.py --build-pool")

        fresh = [i for i in self.images if not self.ledger.image_used(i.image_id)]
        if not fresh:
            # Recycle rather than fail. Once every image has been used the
            # no-repeat guarantee has done all it can; halting here would stop
            # the script dead over a purely cosmetic constraint.
            print(f"[art] all {len(self.images)} images used; new cycle")
            self.ledger.images.clear()
            fresh = self.images

        image = random.choice(fresh)
        print(f"[art] #{image.image_id} ({image.width}x{image.height}) "
              f"from a library of {len(self.images)}")
        return image


# ─────────────────────────────────────────────────────────────────────────────
# 5. Video rendering
# ─────────────────────────────────────────────────────────────────────────────

def esc_path(path: str) -> str:
    """ffmpeg filtergraphs treat ':' as an option separator, even in C:/ paths."""
    return path.replace("\\", "/").replace(":", r"\:")


class VideoRenderer:
    """
    Renders the final 1080p60 video in three ffmpeg passes.

    Every split is the same trick applied at a different timescale: do the
    expensive thing once, over the smallest number of frames that can
    reproduce it.

      1. `compose_still`  — the backdrop blur is heavy and the artwork never
         changes, so the plate is composed once instead of on every frame.
      2. `render_loop`    — the zoom, scrim and text are all static or exactly
         24-second periodic, so they are baked into a 1440-frame clip instead
         of being recomputed for every frame of a 3-minute song.
      3. `render`         — only the visualiser genuinely varies frame to
         frame, so only the visualiser runs on the full timeline.
    """

    def __init__(self, fps: int = FPS, watermark: str = WATERMARK):
        self.fps = fps
        self.watermark = watermark

    # ── Pass 1: the static plate ────────────────────────────────────────────
    def build_still_graph(self) -> str:
        return ";".join([
            # Backdrop: cover-crop + blur. This exists so portrait or square art
            # still fills a 16:9 frame without ever being cropped or stretched.
            f"[0:v]scale={COMP_W}:{COMP_H}:force_original_aspect_ratio=increase:"
            f"flags=lanczos,crop={COMP_W}:{COMP_H},gblur=sigma=28,"
            f"eq=brightness=-0.14:saturation=1.12[bg]",

            # Foreground: the art itself, contained. On a 16:9 source it fills
            # the frame exactly and the backdrop is never seen.
            f"[0:v]scale={COMP_W}:{COMP_H}:force_original_aspect_ratio=decrease:"
            f"flags=lanczos[fg]",

            "[bg][fg]overlay=(W-w)/2:(H-h)/2[plate]",
        ])

    def compose_still(self, image: Path, work_dir: Path) -> Path:
        dest = work_dir / "plate.png"
        print(f"[video] composing {COMP_W}x{COMP_H} plate")
        graph_file = work_dir / "still_graph.txt"
        graph_file.write_text(self.build_still_graph(), encoding="utf-8")
        run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(image),
            "-filter_complex_script", str(graph_file),
            "-map", "[plate]", "-frames:v", "1", str(dest),
        ])
        return dest

    def compose_scrim(self, work_dir: Path) -> Path:
        """
        Bake the text scrim as a transparent PNG.

        A flat drawbox leaves a hard seam straight across the frame, which reads
        as a distraction rather than as part of the art. This is a smoothstep
        ramp into a solid plateau instead: smooth enough to show no seam, but
        already at full strength by the time it reaches the text.

        Baking it once also means the expensive per-pixel geq runs a single
        time rather than on every one of ~10,000 frames.
        """
        dest = work_dir / "scrim.png"
        # t = normalised depth into the ramp; then the classic 3t²-2t³ smoothstep,
        # which flattens at both ends so neither edge of the ramp shows a seam.
        # t is inlined rather than stashed with st()/ld(): ffmpeg sequences
        # expressions with ';', which is also the filtergraph separator, so a
        # single self-contained expression is the one unambiguous spelling.
        t = f"clip((Y-{SCRIM_TOP})/{SCRIM_RAMP},0,1)"
        alpha = f"{SCRIM_ALPHA}*255*({t})*({t})*(3-2*({t}))"
        run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c=black:s={WIDTH}x{HEIGHT}",
            "-vf", f"format=rgba,geq=r=0:g=0:b=0:a='{alpha}'",
            "-frames:v", "1", str(dest),
        ], quiet=True)
        return dest

    # ── Pass 2: the 24-second motion loop ───────────────────────────────────
    def build_loop_graph(self, text_files: dict[str, Path]) -> str:
        vis_y = HEIGHT - VIS_HEIGHT
        # Text sits *above* the visualiser band: showcqt bars grow upward from
        # their baseline and on loud passages fill the whole band, so anything
        # drawn inside it would be strobed over.
        title_y = vis_y - 168
        subtitle_y = vis_y - 84
        zoom_frames = self.fps * ZOOM_PERIOD_S

        parts = [
            # ── Slow sinusoidal "breathing" zoom over the pre-composed plate.
            #    A full sine cycle returns to its exact start, so the motion
            #    loops seamlessly with no jump cut. Zooming an oversized plate
            #    down to 1080p keeps every frame a downsample, never a blur-up.
            f"[0:v]zoompan="
            f"z='{ZOOM_BASE:.4f}+{ZOOM_AMPLITUDE}*sin(2*PI*on/{zoom_frames})':"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d=1:s={WIDTH}x{HEIGHT}:fps={self.fps}[zoomed]",

            # ── Scrim: guarantees text contrast regardless of how bright the
            #    art is. Pre-baked (input 1) so this is a cheap alpha overlay.
            "[zoomed][1:v]overlay=0:0[base]",
        ]

        # ── Text. Bold title, lighter subtitle, both inside the scrim.
        #
        # `textfile=` rather than `text=`, because song titles are full of
        # exactly the characters a filtergraph uses as syntax — "Don't Stop",
        # "Life, Love", "Remix [Extended]" — and drawtext escaping is
        # two-level: once for the option parser, once for the graph parser.
        # A single-quoted `text=` cannot even carry an apostrophe, since
        # ffmpeg has no in-quote escape. Reading the string from a file takes
        # the whole class of problem off the table.
        title_draw = (
            f"drawtext=fontfile='{esc_path(FONT_BOLD)}':"
            f"textfile='{esc_path(str(text_files['title']))}':"
            f"fontcolor=white@0.96:fontsize=62:"
            f"x=72:y={title_y}:shadowcolor=black@0.6:shadowx=0:shadowy=3"
        )
        subtitle_draw = (
            f"drawtext=fontfile='{esc_path(FONT_REG)}':"
            f"textfile='{esc_path(str(text_files['subtitle']))}':"
            f"fontcolor=white@0.72:fontsize=30:"
            f"x=74:y={subtitle_y}:shadowcolor=black@0.5:shadowx=0:shadowy=2"
        )
        chain = [title_draw, subtitle_draw]

        if self.watermark:
            # "Subtle and non-intrusive", per the watermark rule — but subtle
            # still has to be legible. Without the shadow it vanishes entirely
            # over pale artwork (a bright sky, snow), which is most of the
            # scenery this pulls; the shadow keeps it readable on any backdrop.
            chain.append(
                f"drawtext=fontfile='{esc_path(FONT_REG)}':"
                f"textfile='{esc_path(str(text_files['watermark']))}':"
                f"fontcolor=white@0.40:"
                f"fontsize=26:x=w-tw-56:y=56:"
                f"shadowcolor=black@0.45:shadowx=0:shadowy=2"
            )

        chain.append("format=yuv420p")
        parts.append("[base]" + ",".join(chain) + "[v]")
        return ";\n".join(parts)

    def _write_text_files(self, work_dir: Path, title: str,
                          subtitle: str) -> dict[str, Path]:
        """
        Put each on-screen string in its own file for drawtext to read.

        No trailing newline: drawtext renders one, and it shifts the vertical
        centring of the line.
        """
        files = {"title": title, "subtitle": subtitle,
                 "watermark": self.watermark or ""}
        paths = {}
        for name, value in files.items():
            path = work_dir / f"text-{name}.txt"
            path.write_text(value, encoding="utf-8", newline="")
            paths[name] = path
        return paths

    def render_loop(self, plate: Path, scrim: Path, work_dir: Path,
                    title: str, subtitle: str) -> Path:
        """
        Render the 24-second seamless motion loop — everything but the bars.

        This is the whole optimisation. The zoom is a full sine cycle over
        exactly ZOOM_PERIOD_S, so frame 1440 is identical to frame 0; the
        scrim, the title, the subtitle and the watermark never move at all.
        A three-and-a-half minute song therefore contains the same 1440
        distinct frames, nine times over.

        Rendering them once and looping turns ~12,600 lanczos rescales into
        1,440. That matters far more than the x264 preset does, because
        `zoompan` is effectively single-threaded: on a 4-core Ivy Bridge box
        it, not the encoder, is what the render spends its evening waiting on.
        """
        dest = work_dir / "loop.mp4"
        frames = self.fps * ZOOM_PERIOD_S

        text_files = self._write_text_files(work_dir, title, subtitle)
        graph_file = work_dir / "loop_graph.txt"
        graph_file.write_text(self.build_loop_graph(text_files),
                              encoding="utf-8")

        print(f"[video] baking {ZOOM_PERIOD_S}s motion loop ({frames} frames)")
        run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-stats",
            "-loop", "1", "-framerate", str(self.fps), "-i", str(plate),
            "-loop", "1", "-framerate", str(self.fps), "-i", str(scrim),
            "-filter_complex_script", str(graph_file),
            "-map", "[v]", "-frames:v", str(frames),
            # A throwaway intermediate: spend no time compressing it. At CRF
            # 12 over a soft, blurred, barely-moving plate the final encode
            # cannot tell the difference.
            "-c:v", "libx264", "-preset", LOOP_PRESET, "-crf", str(LOOP_CRF),
            "-pix_fmt", "yuv420p", "-an",
            str(dest),
        ])
        return dest

    # ── Pass 3: the visualiser, over the looped motion ──────────────────────
    def build_final_graph(self) -> str:
        vis_y = HEIGHT - VIS_HEIGHT
        return ";\n".join([
            # ── Visualiser: a constant-Q transform, i.e. bars on a *musical*
            #    log-frequency axis, so they track notes rather than raw FFT
            #    bins. The one thing here that genuinely has to run per frame.
            f"[1:a]showcqt=s={WIDTH}x{VIS_HEIGHT}:fps={self.fps}:count=1:"
            f"axis_h=0:sono_h=0:bar_g=2:bar_v=12:tc=0.33:"
            f"cscheme=1|0.28|0.62|0.62|0.28|1[cqt]",

            # ── Screen-blend it in: black stays black, so the bars glow over
            #    the art instead of sitting in an opaque box. Both sides are
            #    forced to planar RGB first — screen-blending YUV would apply
            #    the formula to the chroma planes too and shift every colour.
            f"[cqt]pad={WIDTH}:{HEIGHT}:0:{vis_y}:color=black,format=gbrp[cqtpad]",
            "[0:v]format=gbrp[basergb]",
            "[basergb][cqtpad]blend=all_mode=screen,format=yuv420p[v]",
        ])

    def render(self, image: Path, audio: Path, dest: Path,
               title: str, subtitle: str, work_dir: Path) -> Path:
        plate = self.compose_still(image, work_dir)
        scrim = self.compose_scrim(work_dir)
        loop = self.render_loop(plate, scrim, work_dir, title, subtitle)

        graph_file = work_dir / "filtergraph.txt"
        graph_file.write_text(self.build_final_graph(), encoding="utf-8")

        print(f"[video] rendering {WIDTH}x{HEIGHT}@{self.fps} (this takes a while)")
        run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-stats",
            # -stream_loop repeats the baked clip for as long as the audio
            # lasts; -shortest then trims the tail mid-cycle. The seam is
            # invisible because the loop's last frame matches its first.
            "-stream_loop", "-1", "-i", str(loop),
            "-i", str(audio),
            "-filter_complex_script", str(graph_file),
            "-map", "[v]", "-map", "1:a",
            "-c:v", "libx264", "-preset", X264_PRESET, "-crf", str(CRF),
            "-threads", str(ENCODE_THREADS),
            "-pix_fmt", "yuv420p", "-r", str(self.fps),
            "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", str(SAMPLE_RATE),
            "-movflags", "+faststart", "-shortest",
            str(dest),
        ])
        return dest


# ─────────────────────────────────────────────────────────────────────────────
# Verification
# ─────────────────────────────────────────────────────────────────────────────

def verify(video: Path, expected_duration: float) -> None:
    """Probe the finished file and assert it meets the published requirements."""
    raw = run([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", str(video),
    ], capture=True, quiet=True)
    info = json.loads(raw)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next(s for s in info["streams"] if s["codec_type"] == "audio")
    duration = float(info["format"]["duration"])

    checks = [
        ("resolution >= 1080p", v["width"] >= 1920 and v["height"] >= 1080,
         f'{v["width"]}x{v["height"]}'),
        ("yuv420p (universal playback)", v["pix_fmt"] == "yuv420p", v["pix_fmt"]),
        ("stereo 48 kHz audio", a["channels"] == 2 and a["sample_rate"] == "48000",
         f'{a["channels"]}ch {a["sample_rate"]}Hz'),
        ("full song length", abs(duration - expected_duration) < 2.0,
         f"{duration:.1f}s"),
    ]
    print("\n[verify]")
    ok = True
    for name, passed, detail in checks:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}  ({detail})")
        ok = ok and passed
    if not ok:
        raise RuntimeError("output failed verification")


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RenderResult:
    """Everything the publisher needs to know about a finished video."""

    path: Path
    song: Song
    playlist_tag: str | None
    duration: float
    image_id: str


def make_one(args: argparse.Namespace) -> RenderResult:
    require_tools("ffmpeg", "ffprobe", "yt-dlp")
    for d in (OUT_DIR, WORK_DIR, STATE_DIR, ART_DIR):
        d.mkdir(parents=True, exist_ok=True)

    ledger = Ledger.load(LEDGER_PATH)
    print(f"[ledger] {len(ledger.songs)} songs used, "
          f"{len(ledger.skipped)} skipped as untaggable")

    # 4 (first) ── art. The library is checked up front, before anything is
    # downloaded, so an empty pool fails in a second rather than after a
    # five-minute render has already burned a song out of the ledger.
    pool = ArtPool(ledger)
    if not len(pool):
        raise PoolEmpty("Anime art library is empty. Run:  "
                        "python nightcore.py --build-pool")

    # 1 + 2 ── song. Two independent reasons to move on to the next candidate,
    # and they are not the same kind of reason:
    #
    #   * no playlist tag — a permanent verdict on the song, recorded so we
    #     never spend two API calls on it again;
    #   * no sourceable audio — charts are full of tracks that simply aren't
    #     on YouTube (brand-new releases, regional hits, streaming-only
    #     artists), which says nothing about whether they'd be fine tomorrow.
    #
    # Both are checked before the download, because the tag gate is two HTTP
    # requests and the alternative is finding out after a five-minute render.
    catalogue = SongCatalogue(ledger)
    seasonal = seasonal_playlist() if NETWORK_ENABLED else None
    if seasonal and ledger.seasonal_used_today(seasonal[0]):
        seasonal = None
    seasonal_catalogue = (
        SongCatalogue(ledger, [SpotifyPlaylist(seasonal[1])])
        if seasonal else None
    )
    fetcher = AudioFetcher(ledger, WORK_DIR)
    song = fetched = None
    classification = None
    selected_tag = None
    rejected: set[str] = set()

    for _ in range(SONG_ATTEMPTS):
        if seasonal_catalogue is not None:
            try:
                song = seasonal_catalogue.pick(exclude=rejected)
                selected_tag = seasonal[0]
            except NoSongsLeft:
                seasonal_catalogue = None
                selected_tag = None
                song = catalogue.pick(exclude=rejected)
        else:
            song = catalogue.pick(exclude=rejected)

        if NETWORK_ENABLED and selected_tag is None:
            try:
                classification = tagging.classify_song(song.artist, song.title)
            except tagging.LookupUnavailable as exc:
                # Both metadata sources are down. That is a statement about
                # the internet, not about the song, so it is not written off.
                print(f"[tag] {song.label}: {exc}")
                rejected.add(song.key)
                continue
            except NoTagMatch as exc:
                print(f"[tag] {song.label}: {exc} — skipping permanently")
                ledger.record_skip(song, str(exc))
                ledger.save()
                rejected.add(song.key)
                continue

        try:
            fetched = fetcher.fetch(song)
            break
        except NoAudioSource as exc:
            print(f"[song] {song.label}: {exc}")
            rejected.add(song.key)
    else:
        raise NoAudioSource(
            f"Could not tag and source audio for any of {SONG_ATTEMPTS} "
            f"songs. Check that yt-dlp is current: yt-dlp -U")

    # 3 ── nightcore
    nc_audio = WORK_DIR / f"{slugify(song.label)}-nightcore.wav"
    Nightcorifier(args.speed).process(fetched.path, nc_audio)
    expected_duration = fetched.duration / args.speed

    image = pool.pick()

    # 5 ── video
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = OUT_DIR / f"{stamp}-{slugify(song.label)}-nightcore.mp4"
    try:
        VideoRenderer(fps=args.fps).render(
            image=image.path,
            audio=nc_audio,
            dest=dest,
            title=song.title,
            subtitle=f"{song.artist}  ·  Nightcore",
            work_dir=WORK_DIR,
        )
        verify(dest, expected_duration)
    except BaseException:
        # A cancelled or failed render leaves a truncated mp4 behind. Leaving
        # it in ./out would mean the next person to look in there — or any
        # future "upload everything in out/" — finds a broken file that looks
        # exactly like a finished one.
        dest.unlink(missing_ok=True)
        raise

    # Only commit to the ledger once the file actually exists and passed.
    ledger.record_song(song, fetched.video_id, fetched.source_title,
                       selected_tag)
    ledger.images.add(image.image_id)
    ledger.videos.add(dest.name)
    ledger.save()

    if not args.keep_work:
        # The pool image is library stock — never delete that one.
        for f in (fetched.path, nc_audio, WORK_DIR / "plate.png",
                  WORK_DIR / "scrim.png", WORK_DIR / "loop.mp4",
                  *WORK_DIR.glob("text-*.txt")):
            f.unlink(missing_ok=True)

    playlist_tag = selected_tag or (
        classification.tag if classification else None)
    tag_line = f"#playlist_{playlist_tag}" if playlist_tag else "(network mode off)"
    print(textwrap.dedent(f"""
        [done] {dest}
               {song.label}  ·  {args.speed:.2f}x  ·  {expected_duration:.0f}s
               tag: {tag_line}
               art: safebooru #{image.image_id}
    """).rstrip())
    return RenderResult(
        path=dest,
        song=song,
        playlist_tag=playlist_tag,
        duration=expected_duration,
        image_id=image.image_id,
    )


def main() -> None:
    p = argparse.ArgumentParser(
        description="Make one nightcore video: download, speed up, visualise.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--speed", type=float, default=SPEED,
                   help="playback multiplier (pitch rises with it)")
    p.add_argument("--fps", type=int, default=FPS, help="output framerate")
    p.add_argument("--build-pool", nargs="?", type=int, const=ART_POOL_SIZE,
                   metavar="N", default=None,
                   help="stock the local anime art library to N images and exit")
    p.add_argument("--pool-jobs", type=int, default=POOL_JOBS, metavar="N",
                   help="how many images to download and process at once "
                        "when building the pool")
    p.add_argument("--keep-work", action="store_true",
                   help="keep intermediate audio in ./work")
    p.add_argument("--list-used", action="store_true",
                   help="print the ledger and exit")
    p.add_argument("--audit-pool", action="store_true",
                   help="re-check every image in the art library against the "
                        "frame and report what fails")
    p.add_argument("--prune-pool", action="store_true",
                   help="like --audit-pool, but delete the failures")

    loop = p.add_argument_group("continuous operation")
    loop.add_argument("--loop", action="store_true",
                      help="keep making videos on an interval until stopped")
    loop.add_argument("--interval", type=int, default=3600, metavar="SECONDS",
                      help="time between cycle *starts* when looping")
    loop.add_argument("--exit-when-empty", action="store_true",
                      help="stop the loop when no unused songs remain, "
                           "instead of waiting for the charts to turn over")

    yt = p.add_argument_group("youtube")
    yt.add_argument("--no-upload", action="store_true",
                    help="render only; don't publish")
    yt.add_argument("--privacy", default="public",
                    choices=["public", "unlisted", "private"],
                    help="privacy status for uploaded videos")
    yt.add_argument("--youtube-auth", action="store_true",
                    help="run the OAuth browser flow and write state/"
                         "yt_token.json, then exit")
    yt.add_argument("--upload", metavar="FILE",
                    help="upload an already-rendered video and exit "
                         "(needs --playlist-tag unless NIGHTCORE_NETWORK=0)")
    yt.add_argument("--playlist-tag", metavar="TAG",
                    help="playlist tag to use with --upload, e.g. 'pop'")

    p.add_argument("--benchmark", action="store_true",
                   help="time the render at several encoder settings and exit")

    args = p.parse_args()

    if args.build_pool is not None:
        require_tools("ffmpeg")
        control.install_signal_handlers()
        try:
            ArtPool(Ledger.load(LEDGER_PATH)).build(args.build_pool,
                                                    jobs=args.pool_jobs)
        except Cancelled:
            sys.exit("cancelled — images fetched so far are kept")
        return

    if args.list_used:
        _print_ledger()
        return

    if args.audit_pool or args.prune_pool:
        _audit_pool(prune=args.prune_pool)
        return

    if args.youtube_auth:
        _youtube_auth()
        return

    if args.benchmark:
        import benchmark
        benchmark.run(fps=args.fps)
        return

    if args.upload:
        _upload_existing(args)
        return

    if args.loop:
        import scheduler
        sys.exit(scheduler.run_loop(args))

    # One-shot. The library code raises rather than exiting so that the loop
    # can survive its failures; here at the top level they turn back into a
    # message and a status code.
    control.install_signal_handlers()
    try:
        result = make_one(args)
    except Cancelled:
        sys.exit("cancelled")
    except NightcoreError as exc:
        sys.exit(str(exc))

    if not args.no_upload:
        import scheduler
        import youtube
        quota = youtube.QuotaLedger.load(STATE_DIR / "quota.json")
        uploader = youtube.Uploader(
            client_secret=STATE_DIR / "client_secret.json",
            token_path=STATE_DIR / "yt_token.json",
            privacy=args.privacy)
        try:
            scheduler.publish(result, uploader, quota)
        except NightcoreError as exc:
            sys.exit(f"rendered {result.path.name} but could not publish: {exc}")


def _print_ledger() -> None:
    ledger = Ledger.load(LEDGER_PATH)
    pool = ArtPool(ledger)
    print(f"art library: {len(pool)} images, {len(ledger.images)} used "
          f"this cycle")
    if not ledger.songs:
        print("No songs used yet.")
    for entry in ledger.songs.values():
        published = f"  https://youtu.be/{entry['youtube_id']}" \
            if entry.get("youtube_id") else ""
        tag = f"  #playlist_{entry['playlist_tag']}" \
            if entry.get("playlist_tag") else ""
        print(f"  {entry['used_at']}  {entry['artist']} - {entry['title']}"
              f"{tag}{published}")
    if ledger.skipped:
        print(f"\nskipped as untaggable ({len(ledger.skipped)}):")
        for entry in ledger.skipped.values():
            print(f"  {entry['artist']} - {entry['title']}  ({entry['reason']})")


def _audit_pool(prune: bool = False) -> None:
    """
    Re-measure the whole library against the frame.

    The ingest checks are the same code, so in a healthy library this finds
    nothing — and that is the point. Anything it turns up is either art that
    predates the current thresholds or a hole in the ingest gate.
    """
    files = sorted(p for p in ART_DIR.glob("*.jpg"))
    if not files:
        print(f"no images in {ART_DIR}")
        return

    print(f"[audit] {len(files)} images against a {WIDTH}x{HEIGHT} frame "
          f"(aspect {artcheck.MIN_ASPECT:.2f}–{artcheck.MAX_ASPECT:.2f}, "
          f"coverage >= {artcheck.MIN_COVERAGE})")

    failures = []
    for path in files:
        reasons = artcheck.check_file(path)
        if reasons:
            failures.append((path, reasons))
            print(f"  FAIL  {path.name}")
            for why in reasons:
                print(f"          {why}")

    print(f"\n[audit] {len(files) - len(failures)} pass, {len(failures)} fail")
    if not failures:
        return

    if not prune:
        print("       re-run with --prune-pool to delete them")
        return

    manifest = []
    if ART_MANIFEST.exists():
        manifest = json.loads(ART_MANIFEST.read_text(encoding="utf-8"))
    doomed = {p.name for p, _ in failures}
    for path, _ in failures:
        path.unlink(missing_ok=True)
    ART_MANIFEST.write_text(
        json.dumps([r for r in manifest if r["file"] not in doomed], indent=2),
        encoding="utf-8")
    print(f"[audit] deleted {len(failures)}; "
          f"{len(list(ART_DIR.glob('*.jpg')))} images remain")


def _youtube_auth() -> None:
    import youtube
    uploader = youtube.Uploader(
        client_secret=STATE_DIR / "client_secret.json",
        token_path=STATE_DIR / "yt_token.json")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        uploader.authorise(interactive=True)
    except NightcoreError as exc:
        sys.exit(str(exc))
    print(f"[yt] token written to {uploader.token_path}\n"
          f"     Copy it to state/ on the render box — it needs no browser.")


def _upload_existing(args: argparse.Namespace) -> None:
    """Publish a file that is already on disk. The manual recovery path."""
    import scheduler
    import youtube

    video = Path(args.upload)
    if not video.exists():
        sys.exit(f"no such file: {video}")
    if NETWORK_ENABLED:
        if not args.playlist_tag:
            sys.exit("--upload also needs --playlist-tag "
                     "(one of: " + ", ".join(sorted(tagging.VALID_TAGS)) + ")")
        if args.playlist_tag not in tagging.VALID_TAGS:
            sys.exit(f"{args.playlist_tag!r} is not a published playlist tag")
    playlist_tag = args.playlist_tag if NETWORK_ENABLED else None

    # Recover the song from the ledger entry the render wrote, so the title
    # and description match what an automatic upload would have produced.
    ledger = Ledger.load(LEDGER_PATH)
    entry = next(
        (e for e in ledger.songs.values()
         if slugify(f"{e['artist']} - {e['title']}") in video.stem),
        None)
    if entry is None:
        sys.exit(f"can't work out which song {video.name} is; "
                 f"it isn't in {LEDGER_PATH}")

    song = Song(artist=entry["artist"], title=entry["title"])
    result = RenderResult(path=video, song=song, playlist_tag=playlist_tag,
                          duration=0.0, image_id="")
    control.install_signal_handlers()
    quota = youtube.QuotaLedger.load(STATE_DIR / "quota.json")
    uploader = youtube.Uploader(
        client_secret=STATE_DIR / "client_secret.json",
        token_path=STATE_DIR / "yt_token.json",
        privacy=args.privacy)
    try:
        scheduler.publish(result, uploader, quota)
    except NightcoreError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
