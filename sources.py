"""
Where songs come from.

Four sources, pooled rather than ranked. Pooling matters for more than
resilience: a strict priority order drains the whole first source before it
ever reaches the second, which is how a run ends up publishing fifty pop
songs in a row and tagging every one of them `#playlist_pop`.

  * Deezer genre charts — keyless, ~2000 unique tracks across 23 genres, and
    the only source here with real genre spread. Metal, Electro, Latin,
    Brazilian, Asian and Country charts are what actually populate the
    network's niche playlist tags.
  * Apple Music RSS — keyless, 100 per country per day, refreshed daily, so
    it keeps producing new material rather than being a fixed pool.
  * Spotify playlist — via the Web API when credentials are configured, which
    paginates the whole thing. Without them it falls back to the /embed/ page,
    which hard-caps at 100 tracks no matter how large the playlist is.
  * Soundcharts — kept as a long-stop. Its public sandbox credentials are
    shared by everyone reading their docs and get rate-limited into 401s, so
    it is treated as expendable.

Every source caches for the life of the process: `SongCatalogue.pick` is
called up to SONG_ATTEMPTS times per cycle and must not re-fetch each time.
"""

from __future__ import annotations

import base64
from datetime import date
import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent import futures
from dataclasses import dataclass

from errors import NoSongsLeft

USER_AGENT = "nightcore-tool/1.0 (+https://gamer3514.co.uk)"

SPOTIFY_PLAYLIST_ID = (os.environ.get("SPOTIFY_PLAYLIST_ID")
                       or "5lvLZprCePVSVHwUv2rBbe")
SEASONAL_PLAYLISTS = {
    "christmas": (os.environ.get("SPOTIFY_CHRISTMAS_PLAYLIST_ID")
                  or "44hnCtnqNzfWtHaSZk98Fh", (11, 1), (12, 25)),
    "halloween": (os.environ.get("SPOTIFY_HALLOWEEN_PLAYLIST_ID")
                  or "1i5nWSi6SgqkodJ5raiHVq", (10, 24), (10, 31)),
}
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")

SOUNDCHARTS_BASE = "https://customer.api.soundcharts.com"
SOUNDCHARTS_CHART = os.environ.get("SOUNDCHARTS_CHART") or "global-28"
SOUNDCHARTS_APP_ID = os.environ.get("SOUNDCHARTS_APP_ID") or "soundcharts"
SOUNDCHARTS_API_KEY = os.environ.get("SOUNDCHARTS_API_KEY") or "soundcharts"

# Storefronts for the Apple feed. The non-English ones are deliberate: they
# are the cheapest way to reach songs that resolve to a *language* playlist
# tag rather than yet another #playlist_pop.
APPLE_STOREFRONTS = (os.environ.get("APPLE_STOREFRONTS")
                     or "us,gb,de,fr,es,br,jp,it,pl,mx").split(",")


def seasonal_playlist(today: date | None = None) -> tuple[str, str] | None:
    """Return the active seasonal tag and playlist id, if any."""
    today = today or date.today()
    for tag, (playlist_id, start, end) in SEASONAL_PLAYLISTS.items():
        start_date = date(today.year, *start)
        end_date = date(today.year, *end)
        if start_date <= today <= end_date:
            return tag, playlist_id
    return None


# ─────────────────────────────────────────────────────────────────────────────
# HTTP
# ─────────────────────────────────────────────────────────────────────────────

def http_text(url: str, *, headers: dict[str, str] | None = None,
              timeout: int = 30, data: bytes | None = None) -> str:
    req = urllib.request.Request(
        url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def http_json(url: str, **kwargs) -> object:
    return json.loads(http_text(url, **kwargs))


# ─────────────────────────────────────────────────────────────────────────────
# Song
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Song:
    artist: str
    title: str

    @property
    def key(self) -> str:
        return f"{self.artist.lower()} - {self.title.lower()}"

    @property
    def label(self) -> str:
        return f"{self.artist} - {self.title}"

    @property
    def primary_artist(self) -> str:
        """
        Just the lead act.

        Chart credits arrive as "KAROL G & Judeline & rusowsky"; searching that
        whole string matches far fewer uploads than searching the lead artist,
        because most uploaders don't spell the full credit out.

        The word separators demand whitespace on *both* sides. Allowing zero
        width on the left turns "Alex Warren" into "Ale", because the collab
        "x" happily matches the one inside the name.
        """
        head = re.split(r"\s*,\s*|\s+(?:&|\+|feat\.?|ft\.?|with|x|vs\.?)\s+",
                        self.artist, maxsplit=1, flags=re.I)[0]
        return head.strip() or self.artist

    @property
    def search_query(self) -> str:
        return f"{self.primary_artist} {self.title} audio"


class Source:
    """A place songs come from. Fetches once, then serves from memory."""

    name = "source"

    def __init__(self):
        self._cache: list[Song] | None = None

    def fetch(self) -> list[Song]:
        if self._cache is None:
            self._cache = self._fetch()
        return self._cache

    def _fetch(self) -> list[Song]:      # pragma: no cover - interface
        raise NotImplementedError


# ─────────────────────────────────────────────────────────────────────────────
# Deezer
# ─────────────────────────────────────────────────────────────────────────────

class DeezerCharts(Source):
    """
    Every Deezer editorial genre chart, merged.

    No key, no token, no rate limit worth worrying about. Twenty-odd charts of
    a hundred tracks each, and because they are split *by genre* they reach
    the metal, electro, latin and regional music that a single global pop
    chart never surfaces.
    """

    name = "deezer-charts"
    API = "https://api.deezer.com"

    def _fetch(self) -> list[Song]:
        genres = http_json(f"{self.API}/editorial")
        ids = [g["id"] for g in (genres or {}).get("data", [])] or [0]

        songs: list[Song] = []
        # Independent requests over a fast link; serially this is 24 round
        # trips of latency for no reason.
        with futures.ThreadPoolExecutor(max_workers=8) as pool:
            for result in pool.map(self._chart, ids):
                songs.extend(result)
        return songs

    def _chart(self, genre_id: int) -> list[Song]:
        try:
            data = http_json(
                f"{self.API}/chart/{genre_id}/tracks?limit=100")
        except Exception:
            return []
        out = []
        for track in (data or {}).get("data", []):
            artist = (track.get("artist") or {}).get("name")
            title = track.get("title")
            if artist and title:
                out.append(Song(artist=artist.strip(), title=title.strip()))
        return out


# ─────────────────────────────────────────────────────────────────────────────
# Apple Music
# ─────────────────────────────────────────────────────────────────────────────

class AppleMusicRSS(Source):
    """
    Apple's "most played" feed, across several storefronts.

    Keyless and regenerated daily, so unlike a fixed playlist it keeps
    producing material indefinitely.
    """

    name = "apple-rss"
    API = "https://rss.applemarketingtools.com/api/v2"

    def __init__(self, storefronts: list[str] | None = None):
        super().__init__()
        self.storefronts = [s.strip() for s in
                            (storefronts or APPLE_STOREFRONTS) if s.strip()]

    def _fetch(self) -> list[Song]:
        songs: list[Song] = []
        with futures.ThreadPoolExecutor(max_workers=6) as pool:
            for result in pool.map(self._storefront, self.storefronts):
                songs.extend(result)
        return songs

    def _storefront(self, code: str) -> list[Song]:
        try:
            data = http_json(
                f"{self.API}/{code}/music/most-played/100/songs.json")
        except Exception:
            return []
        out = []
        for row in ((data or {}).get("feed") or {}).get("results", []):
            artist, title = row.get("artistName"), row.get("name")
            if artist and title:
                out.append(Song(artist=artist.strip(), title=title.strip()))
        return out


# ─────────────────────────────────────────────────────────────────────────────
# Spotify
# ─────────────────────────────────────────────────────────────────────────────

class SpotifyPlaylist(Source):
    """
    A Spotify playlist, in full when credentials allow.

    Two paths, and the difference is not cosmetic:

      * With a client id and secret, the Web API pages through the entire
        playlist 100 tracks at a time. A 1000-track playlist yields 1000.
      * Without them, the public /embed/ page is scraped instead. That page
        inlines its track list as JSON and needs no auth at all — but it
        **hard-caps at 100 tracks** regardless of playlist size, silently. A
        1000-track playlist yields 100, and no error says so.

    Client credentials are machine-to-machine: no browser, no user consent, no
    refresh dance. Register an app at developer.spotify.com/dashboard and set
    SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET.
    """

    name = "spotify-playlist"
    TOKEN_URL = "https://accounts.spotify.com/api/token"
    API = "https://api.spotify.com/v1"
    EMBED_CAP = 100

    def __init__(self, playlist_id: str = SPOTIFY_PLAYLIST_ID,
                 client_id: str = "", client_secret: str = ""):
        super().__init__()
        self.playlist_id = playlist_id
        self.client_id = client_id or SPOTIFY_CLIENT_ID
        self.client_secret = client_secret or SPOTIFY_CLIENT_SECRET
        self._token = ""
        self._token_expires = 0.0

    @property
    def has_credentials(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _fetch(self) -> list[Song]:
        if self.has_credentials:
            try:
                return self._fetch_api()
            except Exception as exc:
                print(f"[song] spotify api failed ({exc}); "
                      f"falling back to the embed page (first 100 only)")
        return self._fetch_embed()

    # ── Web API ─────────────────────────────────────────────────────────────
    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()).decode()
        payload = http_json(
            self.TOKEN_URL,
            data=b"grant_type=client_credentials",
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"})
        self._token = payload["access_token"]
        self._token_expires = time.time() + int(payload.get("expires_in", 3600))
        return self._token

    def _fetch_api(self) -> list[Song]:
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        url = (f"{self.API}/playlists/{self.playlist_id}/tracks"
               f"?limit=100&fields=next,items(track(name,artists(name)))")
        songs: list[Song] = []
        while url:
            page = http_json(url, headers=headers)
            for item in (page or {}).get("items", []):
                track = item.get("track") or {}
                names = [a.get("name") for a in track.get("artists") or []
                         if a.get("name")]
                if track.get("name") and names:
                    songs.append(Song(artist=", ".join(names).strip(),
                                      title=track["name"].strip()))
            url = (page or {}).get("next")
        return songs

    # ── Embed fallback ──────────────────────────────────────────────────────
    def _fetch_embed(self) -> list[Song]:
        html = http_text(
            f"https://open.spotify.com/embed/playlist/{self.playlist_id}")
        match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            html, re.S)
        if not match:
            raise RuntimeError("no __NEXT_DATA__ in embed page (layout changed?)")

        entity = (json.loads(match.group(1))
                  ["props"]["pageProps"]["state"]["data"]["entity"])
        songs = []
        for track in entity.get("trackList", []):
            title, subtitle = track.get("title"), track.get("subtitle")
            if title and subtitle:
                songs.append(Song(artist=subtitle.strip(), title=title.strip()))

        if len(songs) >= self.EMBED_CAP and not self.has_credentials:
            print(f"[song] spotify embed returned {len(songs)} tracks — that is "
                  f"the page's cap, not your playlist. Set SPOTIFY_CLIENT_ID "
                  f"and SPOTIFY_CLIENT_SECRET to read all of it.")
        return songs


# ─────────────────────────────────────────────────────────────────────────────
# Soundcharts
# ─────────────────────────────────────────────────────────────────────────────

class SoundchartsChart(Source):
    """
    A Soundcharts song chart, via the public sandbox credentials.

    Those credentials are printed in their documentation, so everybody uses
    them and the endpoint rate-limits to 401 without warning. Kept as a
    long-stop and expected to fail; a real key is an enterprise contract, not
    a signup.
    """

    name = f"soundcharts:{SOUNDCHARTS_CHART}"

    def _fetch(self) -> list[Song]:
        data = http_json(
            f"{SOUNDCHARTS_BASE}/api/v2/chart/song/{SOUNDCHARTS_CHART}"
            f"/ranking/latest?limit=100",
            headers={"x-app-id": SOUNDCHARTS_APP_ID,
                     "x-api-key": SOUNDCHARTS_API_KEY})
        if not isinstance(data, dict) or not data.get("items"):
            raise RuntimeError(f"unexpected response: {str(data)[:200]}")

        songs = []
        for item in data["items"]:
            song = item.get("song") or {}
            title, credit = song.get("name"), song.get("creditName")
            if title and credit:
                songs.append(Song(artist=credit.strip(), title=title.strip()))
        return songs


# ─────────────────────────────────────────────────────────────────────────────
# The catalogue
# ─────────────────────────────────────────────────────────────────────────────

def default_sources() -> list[Source]:
    return [DeezerCharts(), AppleMusicRSS(), SpotifyPlaylist(),
            SoundchartsChart()]


class SongCatalogue:
    """
    Every source, merged and de-duplicated, picked from at random.

    De-duplication is by `Song.key`, so the same track charting on Deezer and
    Apple counts once — otherwise popular songs would be drawn twice as often
    as the long tail, which defeats the point of pooling.
    """

    def __init__(self, ledger, sources: list[Source] | None = None):
        self.ledger = ledger
        self.sources = sources if sources is not None else default_sources()
        self._pool: dict[str, Song] | None = None

    def _gather(self) -> dict[str, Song]:
        if self._pool is not None:
            return self._pool

        pool: dict[str, Song] = {}
        # One slow or hanging source should not hold up the other three.
        with futures.ThreadPoolExecutor(max_workers=len(self.sources)) as ex:
            jobs = {ex.submit(s.fetch): s for s in self.sources}
            for job in futures.as_completed(jobs):
                source = jobs[job]
                try:
                    songs = job.result()
                except Exception as exc:
                    print(f"[song] {source.name} unavailable ({exc})")
                    continue
                new = sum(1 for s in songs if s.key not in pool)
                for song in songs:
                    pool.setdefault(song.key, song)
                print(f"[song] {source.name}: {len(songs)} tracks "
                      f"({new} new to the pool)")

        self._pool = pool
        return pool

    def pick(self, exclude: frozenset[str] | set[str] = frozenset()) -> Song:
        pool = self._gather()
        fresh = [s for s in pool.values()
                 if not self.ledger.song_used(s) and s.key not in exclude]
        print(f"[song] pool: {len(fresh)} unused of {len(pool)}")
        if not fresh:
            raise NoSongsLeft(
                "No unused songs left in any source. The charts turn over "
                "daily, so this usually clears on its own; otherwise point "
                "SPOTIFY_PLAYLIST_ID at another playlist or clear "
                "state/ledger.json.")
        return random.choice(fresh)
