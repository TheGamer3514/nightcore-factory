"""
Metadata construction and quota accounting.

Nothing here touches the network. The upload path is exercised through a fake
`next_chunk`, because what needs testing is which errors we retry and which
we surrender to — not Google's HTTP stack.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import control
import youtube
from errors import QuotaExhausted, UploadFailed


# ── Title ───────────────────────────────────────────────────────────────────

def test_title_has_the_expected_shape():
    assert youtube.build_title("Stromae", "Alors on danse") == \
        "Stromae - Alors on danse 【Nightcore】"


def test_long_title_is_truncated_but_keeps_the_suffix():
    """
    The suffix is what makes the video findable as nightcore, so the song
    name gives ground instead of the tag.
    """
    title = youtube.build_title("A Very Long Artist Name Indeed" * 3,
                                "And An Even Longer Song Title Than That" * 3)
    assert len(title) <= youtube.TITLE_MAX
    assert title.endswith(youtube.TITLE_SUFFIX)


def test_exactly_100_characters_is_not_truncated():
    suffix = len(youtube.TITLE_SUFFIX)
    head = "x" * (youtube.TITLE_MAX - suffix)
    assert youtube.build_title(head[:5], head[8:]) .endswith(
        youtube.TITLE_SUFFIX)


# ── Description ─────────────────────────────────────────────────────────────

def test_description_carries_exactly_one_playlist_hashtag():
    """
    The network's automation reads one. Two and the video is mis-filed with
    no error anywhere — which is why the builder asserts rather than logs.
    """
    text = youtube.build_description("Stromae", "Alors on danse", "french")
    assert text.count("#playlist_") == 1
    assert "#playlist_french" in text


def test_playlist_hashtag_comes_first():
    text = youtube.build_description("Artist", "Song", "pop")
    hashtag_line = [l for l in text.splitlines() if l.startswith("#")][0]
    assert hashtag_line.split()[0] == "#playlist_pop"


def test_description_contains_the_required_boilerplate():
    text = youtube.build_description("Artist", "Song", "pop")
    assert "Song Name: Artist - Song" in text
    assert "🌐 Nightcore Network" in text
    assert youtube.NETWORK_URL in text


def test_a_song_named_after_a_playlist_tag_cannot_smuggle_a_second_one():
    """
    "#playlist_pop" from the tag plus a title that flattens to something
    starting "playlist_" would be two. The hashtag builder strips underscores
    out of free text, so this can't happen — but assert it, because the
    consequence is silent.
    """
    text = youtube.build_description("playlist_trance", "playlist_house", "pop")
    assert text.count("#playlist_") == 1


# ── Network mode off (playlist_tag=None) ────────────────────────────────────

def test_without_a_playlist_tag_description_has_no_network_mention():
    text = youtube.build_description("Artist", "Song", None)
    assert "Song Name: Artist - Song" in text
    assert "#playlist_" not in text
    assert "nightcore.com" not in text
    assert "Nightcore Network" not in text
    assert "#nightcore" in text


def test_without_a_playlist_tag_api_tags_drop_the_network_keywords():
    tags = youtube.build_tags("Artist", "Song", None)
    assert "nightcore" in tags
    assert "nightcore network" not in tags


# ── API tags ────────────────────────────────────────────────────────────────

def test_tags_fit_youtube_budget():
    tags = youtube.build_tags("A" * 200, "B" * 200, "pop")
    serialised = sum(len(t) + (3 if " " in t else 1) for t in tags)
    assert serialised <= youtube.TAGS_MAX_CHARS


def test_tags_always_include_nightcore():
    assert "nightcore" in youtube.build_tags("Artist", "Song", "pop")


# ── Quota ───────────────────────────────────────────────────────────────────

def make_quota(tmp_path, spent=0, day=None):
    path = tmp_path / "quota.json"
    day = day or datetime.now(ZoneInfo(youtube.QUOTA_TZ)).date().isoformat()
    path.write_text(json.dumps({"pacific_day": day, "units_spent": spent}))
    return youtube.QuotaLedger.load(path)


def test_budget_allows_exactly_six_uploads(tmp_path):
    q = make_quota(tmp_path)
    for _ in range(6):
        assert q.can_upload()
        q.charge()
    assert not q.can_upload()


def test_boundary_of_can_upload(tmp_path):
    assert make_quota(tmp_path, spent=8400).can_upload()      # 1600 left
    assert not make_quota(tmp_path, spent=8401).can_upload()  # 1599 left


def test_yesterdays_spend_is_forgotten(tmp_path):
    """
    The counter is keyed on the Pacific date because that is where Google
    resets — not UTC, and definitely not the VM's local time.
    """
    yesterday = (datetime.now(ZoneInfo(youtube.QUOTA_TZ)).date()
                 - timedelta(days=1)).isoformat()
    q = make_quota(tmp_path, spent=10000, day=yesterday)
    assert q.units_spent == 0
    assert q.can_upload()


def test_mark_exhausted_overrides_an_optimistic_local_count(tmp_path):
    """The API is the authority; our count is only ever a prediction."""
    q = make_quota(tmp_path, spent=0)
    q.mark_exhausted()
    assert not q.can_upload()
    assert youtube.QuotaLedger.load(q.path).remaining == 0


def test_corrupt_quota_file_does_not_crash(tmp_path):
    path = tmp_path / "quota.json"
    path.write_text("{not json")
    assert youtube.QuotaLedger.load(path).can_upload()


def test_seconds_until_reset_is_within_a_day(tmp_path):
    seconds = make_quota(tmp_path).seconds_until_reset()
    assert 60 <= seconds <= 24 * 3600


# ── Upload error handling ───────────────────────────────────────────────────

class FakeResp:
    def __init__(self, status):
        self.status = status


class FakeHttpError(Exception):
    def __init__(self, status, reason):
        super().__init__(f"{status} {reason}")
        self.resp = FakeResp(status)
        self.content = json.dumps(
            {"error": {"errors": [{"reason": reason}]}}).encode()


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    monkeypatch.setattr(control, "sleep", lambda _s: True)
    control.reset_stop()


def uploader_with(monkeypatch, chunks):
    """An Uploader whose `next_chunk` yields the given sequence."""
    calls = iter(chunks)

    class FakeRequest:
        def next_chunk(self):
            outcome = next(calls)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    class FakeVideos:
        def insert(self, **_kwargs):
            return FakeRequest()

    class FakeService:
        def videos(self):
            return FakeVideos()

    monkeypatch.setattr(youtube, "_import_google", lambda: _fake_google())
    up = youtube.Uploader(client_secret=None, token_path=None)
    up._service = FakeService()
    return up


def _fake_google():
    from types import SimpleNamespace

    class FakeMedia:
        def __init__(self, *a, **k):
            pass

    return SimpleNamespace(Request=None, Credentials=None,
                           InstalledAppFlow=None, build=None,
                           HttpError=FakeHttpError, MediaFileUpload=FakeMedia)


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "v.mp4"
    path.write_bytes(b"\0" * 1024)
    return path


def test_transient_error_is_retried(monkeypatch, video, tmp_path):
    up = uploader_with(monkeypatch, [
        FakeHttpError(503, "backendError"),
        (None, {"id": "abc123"}),
    ])
    assert up.upload(video, _meta(), quota=make_quota(tmp_path)) == "abc123"


def test_quota_error_is_not_retried_and_zeroes_the_budget(
        monkeypatch, video, tmp_path):
    quota = make_quota(tmp_path)
    up = uploader_with(monkeypatch, [FakeHttpError(403, "quotaExceeded")])
    with pytest.raises(QuotaExhausted):
        up.upload(video, _meta(), quota=quota)
    assert not quota.can_upload()


def test_permanent_rejection_is_not_retried(monkeypatch, video, tmp_path):
    up = uploader_with(monkeypatch, [FakeHttpError(400, "invalidVideoMetadata")])
    with pytest.raises(UploadFailed):
        up.upload(video, _meta(), quota=make_quota(tmp_path))


def test_gives_up_after_max_attempts(monkeypatch, video, tmp_path):
    up = uploader_with(monkeypatch,
                       [FakeHttpError(500, "backendError")] * 20)
    with pytest.raises(UploadFailed):
        up.upload(video, _meta(), quota=make_quota(tmp_path))


def test_refuses_to_start_without_quota(monkeypatch, video, tmp_path):
    up = uploader_with(monkeypatch, [(None, {"id": "x"})])
    with pytest.raises(QuotaExhausted):
        up.upload(video, _meta(), quota=make_quota(tmp_path, spent=10000))


def _meta():
    return youtube.VideoMetadata.build("Artist", "Song", "pop")
