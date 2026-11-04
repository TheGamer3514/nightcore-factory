"""
Song sources: parsing, pooling, and the failure behaviour that matters.

No network. Each source's HTTP call is replaced with a fixture, because what
needs pinning down is how we treat the answers — especially the ones that
arrive wrong.
"""

from __future__ import annotations

import pytest
from datetime import date

import sources
from errors import NoSongsLeft
from sources import (AppleMusicRSS, DeezerCharts, Song, SongCatalogue,
                     SpotifyPlaylist)


class FakeLedger:
    def __init__(self, used=()):
        self.used = set(used)

    def song_used(self, song):
        return song.key in self.used


class StubSource(sources.Source):
    def __init__(self, name, songs=None, boom=None):
        super().__init__()
        self.name = name
        self.songs = songs or []
        self.boom = boom
        self.calls = 0

    def _fetch(self):
        self.calls += 1
        if self.boom:
            raise self.boom
        return self.songs


def song(n):
    return Song(artist=f"Artist {n}", title=f"Song {n}")


@pytest.mark.parametrize("day, expected", [
    (date(2026, 11, 1), ("christmas", "44hnCtnqNzfWtHaSZk98Fh")),
    (date(2026, 12, 25), ("christmas", "44hnCtnqNzfWtHaSZk98Fh")),
    (date(2026, 10, 24), ("halloween", "1i5nWSi6SgqkodJ5raiHVq")),
    (date(2026, 10, 31), ("halloween", "1i5nWSi6SgqkodJ5raiHVq")),
    (date(2026, 1, 1), None),
])
def test_seasonal_playlist_window(day, expected):
    assert sources.seasonal_playlist(day) == expected


def test_seasonal_playlist_ids_come_from_env(monkeypatch):
    import importlib
    monkeypatch.setenv("SPOTIFY_CHRISTMAS_PLAYLIST_ID", "xmas123")
    monkeypatch.setenv("SPOTIFY_HALLOWEEN_PLAYLIST_ID", "")      # blank = default
    try:
        reloaded = importlib.reload(sources)
        assert reloaded.seasonal_playlist(date(2026, 12, 1)) == ("christmas", "xmas123")
        assert reloaded.seasonal_playlist(date(2026, 10, 30)) == (
            "halloween", "1i5nWSi6SgqkodJ5raiHVq")
    finally:
        monkeypatch.undo()
        importlib.reload(sources)


# ── Song ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("credit, lead", [
    ("KAROL G & Judeline & rusowsky", "KAROL G"),
    ("Sam Fender, Olivia Dean", "Sam Fender"),
    ("Tame Impala with JENNIE", "Tame Impala"),
    ("Drake feat. 21 Savage", "Drake"),
    # The collab separator "x" must not eat the one inside a name.
    ("Alex Warren", "Alex Warren"),
])
def test_primary_artist(credit, lead):
    assert Song(artist=credit, title="t").primary_artist == lead


# ── Pooling ─────────────────────────────────────────────────────────────────

def test_pool_merges_and_deduplicates():
    """
    The same track charting on two services must count once. Otherwise a
    popular song is drawn twice as often as the long tail, which is exactly
    the bias pooling was meant to remove.
    """
    a = StubSource("a", [song(1), song(2)])
    b = StubSource("b", [song(2), song(3)])
    cat = SongCatalogue(FakeLedger(), [a, b])
    assert len(cat._gather()) == 3


def test_one_dead_source_does_not_sink_the_pool():
    """Soundcharts 401s routinely; that must be a log line, not an outage."""
    good = StubSource("good", [song(i) for i in range(5)])
    dead = StubSource("dead", boom=RuntimeError("HTTP Error 401"))
    cat = SongCatalogue(FakeLedger(), [good, dead])
    assert len(cat._gather()) == 5
    assert cat.pick() is not None


def test_every_source_failing_raises_no_songs_left():
    cat = SongCatalogue(FakeLedger(), [StubSource("x", boom=OSError("down"))])
    with pytest.raises(NoSongsLeft):
        cat.pick()


def test_sources_are_fetched_once_per_process():
    """
    pick() is called up to SONG_ATTEMPTS times a cycle. Re-fetching ~2900
    songs across 40 HTTP requests each time would dominate the run.
    """
    src = StubSource("a", [song(i) for i in range(3)])
    cat = SongCatalogue(FakeLedger(), [src])
    for _ in range(4):
        cat.pick()
    assert src.calls == 1


def test_used_and_excluded_songs_are_skipped():
    src = StubSource("a", [song(1), song(2), song(3)])
    cat = SongCatalogue(FakeLedger(used={song(1).key}), [src])
    picked = {cat.pick(exclude={song(2).key}).key for _ in range(8)}
    assert picked == {song(3).key}


def test_exhausted_pool_raises_rather_than_exiting():
    """A daemon needs an exception here; sys.exit would kill the loop."""
    src = StubSource("a", [song(1)])
    cat = SongCatalogue(FakeLedger(used={song(1).key}), [src])
    with pytest.raises(NoSongsLeft):
        cat.pick()


# ── Parsing ─────────────────────────────────────────────────────────────────

def test_deezer_parses_genre_charts(monkeypatch):
    payloads = {
        "editorial": {"data": [{"id": 0, "name": "All"},
                               {"id": 464, "name": "Metal"}]},
        "0": {"data": [{"artist": {"name": "A"}, "title": "One"}]},
        "464": {"data": [{"artist": {"name": "B"}, "title": "Two"},
                         {"artist": {"name": "C"}}]},   # no title: dropped
    }

    def fake(url, **kw):
        if url.endswith("/editorial"):
            return payloads["editorial"]
        return payloads[url.split("/chart/")[1].split("/")[0]]

    monkeypatch.setattr(sources, "http_json", fake)
    got = {s.label for s in DeezerCharts().fetch()}
    assert got == {"A - One", "B - Two"}


def test_apple_parses_storefronts(monkeypatch):
    monkeypatch.setattr(sources, "http_json", lambda url, **kw: {
        "feed": {"results": [{"artistName": "A", "name": url[-13:-11]}]}})
    songs = AppleMusicRSS(storefronts=["us", "de"]).fetch()
    assert len(songs) == 2


def test_apple_tolerates_a_dead_storefront(monkeypatch):
    def fake(url, **kw):
        if "/de/" in url:
            raise OSError("404")
        return {"feed": {"results": [{"artistName": "A", "name": "T"}]}}

    monkeypatch.setattr(sources, "http_json", fake)
    assert len(AppleMusicRSS(storefronts=["us", "de"]).fetch()) == 1


# ── Spotify ─────────────────────────────────────────────────────────────────

def test_spotify_uses_the_api_when_credentials_exist(monkeypatch):
    pl = SpotifyPlaylist("pid", client_id="id", client_secret="secret")
    assert pl.has_credentials

    pages = {
        "first": {"items": [{"track": {"name": "One",
                                       "artists": [{"name": "A"}]}}],
                  "next": "second"},
        "second": {"items": [{"track": {"name": "Two",
                                        "artists": [{"name": "B"},
                                                    {"name": "C"}]}}],
                   "next": None},
    }

    def fake(url, **kw):
        if url == sources.SpotifyPlaylist.TOKEN_URL:
            return {"access_token": "t", "expires_in": 3600}
        return pages["second" if url == "second" else "first"]

    monkeypatch.setattr(sources, "http_json", fake)
    songs = pl.fetch()
    assert [s.label for s in songs] == ["A - One", "B, C - Two"]


def test_spotify_falls_back_to_the_embed_page(monkeypatch):
    """No credentials must still work — just capped, and it says so."""
    pl = SpotifyPlaylist("pid", client_id="", client_secret="")
    assert not pl.has_credentials
    monkeypatch.setattr(sources, "http_text", lambda url, **kw: (
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"state":{"data":{"entity":{"trackList":'
        '[{"title":"One","subtitle":"A"}]}}}}}}</script>'))
    assert [s.label for s in pl.fetch()] == ["A - One"]


def test_spotify_api_failure_degrades_to_the_embed(monkeypatch):
    pl = SpotifyPlaylist("pid", client_id="id", client_secret="secret")
    monkeypatch.setattr(sources, "http_json",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("500")))
    monkeypatch.setattr(sources, "http_text", lambda url, **kw: (
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"state":{"data":{"entity":{"trackList":'
        '[{"title":"One","subtitle":"A"}]}}}}}}</script>'))
    assert len(pl.fetch()) == 1


def test_embed_cap_is_flagged(monkeypatch, capsys):
    """
    100 tracks from the embed is indistinguishable from a 100-track playlist
    unless we say so — and silently losing 90% of a 1000-track playlist is
    how the pipeline stalls after four days.
    """
    pl = SpotifyPlaylist("pid", client_id="", client_secret="")
    tracks = ",".join(f'{{"title":"T{i}","subtitle":"A{i}"}}'
                      for i in range(SpotifyPlaylist.EMBED_CAP))
    monkeypatch.setattr(sources, "http_text", lambda url, **kw: (
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"state":{"data":{"entity":{"trackList":['
        + tracks + ']}}}}}}</script>'))
    pl.fetch()
    assert "SPOTIFY_CLIENT_ID" in capsys.readouterr().out
