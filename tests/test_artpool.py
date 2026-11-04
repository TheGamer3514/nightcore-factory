"""
The art library: the frame checks, and the concurrent build loop.

The build test fakes the network entirely. What it is protecting is not the
downloading — it is that running ingests in parallel still produces each image
exactly once, stops on the target, and leaves the manifest agreeing with the
list in memory. Those are the things concurrency quietly breaks.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

import artcheck
import nightcore
from nightcore import AnimeImage, ArtPool, Ledger


# ── Frame geometry ──────────────────────────────────────────────────────────

def test_max_edge_cannot_make_wide_art_impossible():
    """
    The downscale ceiling has to leave room for the zoom.

    A 16:9 source is contained to the full plate width and then magnified by
    ZOOM_MAX, so capping the long edge below COMP_W * ZOOM_MAX / MAX_UPSCALE
    means *no* image at or above 16:9 can ever pass the sharpness check — no
    matter how large the original. That was true at the original 3000.
    """
    assert artcheck.MAX_EDGE >= artcheck.MIN_VIABLE_EDGE
    wide = artcheck.MAX_EDGE, round(artcheck.MAX_EDGE * 9 / 16)
    assert artcheck.upscale_factor(*wide) <= artcheck.MAX_UPSCALE


@pytest.mark.parametrize("w, h, expect_ok", [
    (3200, 1800, True),        # 16:9 at the cap
    (3400, 2000, True),        # bigger than the cap, downscaled into range
    (2000, 3000, False),       # portrait — blurred side bars
    (4000, 1200, False),       # ultra-wide — blurred top/bottom bars
    (1600, 900, False),        # the old MIN_LONG_EDGE: far too soft now
])
def test_metadata_gate(w, h, expect_ok):
    assert (not artcheck.metadata_verdict(w, h)) is expect_ok


def test_coverage_is_one_only_inside_the_aspect_window():
    assert artcheck.coverage(3200, 1800) == pytest.approx(1.0)
    assert artcheck.coverage(2000, 3000) < 0.8
    lo = artcheck.coverage(round(artcheck.MIN_ASPECT * 1000), 1000)
    assert lo == pytest.approx(1.0, abs=0.01)


def test_banned_tags_catch_other_peoples_branding():
    """
    Someone else's signature next to our watermark looks like a mistake, and
    booru art is full of them — this was the single largest rejection reason
    on a live build.
    """
    for tag in ["watermark", "signature", "twitter_username", "artist_name",
                "english_text", "speech_bubble", "letterboxed", "pixel_art"]:
        assert artcheck.tag_verdict(f"1girl sky {tag}"), tag
    assert not artcheck.tag_verdict("1girl sky scenery highres")


# ── Concurrent build ────────────────────────────────────────────────────────

@pytest.fixture
def pool(tmp_path, monkeypatch):
    monkeypatch.setattr(nightcore, "ART_DIR", tmp_path / "anime")
    monkeypatch.setattr(nightcore, "ART_MANIFEST", tmp_path / "art_pool.json")
    monkeypatch.setattr(nightcore, "WORK_DIR", tmp_path / "work")
    return ArtPool(Ledger(path=tmp_path / "ledger.json"))


def fake_posts(start: int, n: int) -> list[dict]:
    return [{"id": start + i, "rating": "general", "width": 3200,
             "height": 1800, "tags": "scenery",
             "file_url": f"https://example.invalid/{start + i}.jpg"}
            for i in range(n)]


def test_build_ingests_each_image_exactly_once(pool, monkeypatch):
    seen, lock = [], threading.Lock()

    monkeypatch.setattr(ArtPool, "_query",
                        lambda self, tags, limit=100: fake_posts(1000, 60))

    def fake_ingest(self, post):
        with lock:
            seen.append(str(post["id"]))
        time.sleep(0.01)                      # let the workers interleave
        return AnimeImage(image_id=str(post["id"]),
                          path=nightcore.ART_DIR / f"{post['id']}.jpg",
                          width=3200, height=1800)

    monkeypatch.setattr(ArtPool, "_ingest", fake_ingest)
    pool.build(target=20, jobs=6)

    assert len(seen) == len(set(seen)), "an image was ingested twice"
    ids = [i.image_id for i in pool.images]
    assert len(ids) == len(set(ids)) == 20


def test_build_does_not_overshoot_the_target(pool, monkeypatch):
    """
    A batch is sized against what is still needed. Submitting a full batch
    regardless would overshoot by up to jobs*2 every time — which on a
    --build-pool 100 run is a dozen wasted downloads.
    """
    monkeypatch.setattr(ArtPool, "_query",
                        lambda self, tags, limit=100: fake_posts(2000, 80))
    monkeypatch.setattr(ArtPool, "_ingest", lambda self, post: AnimeImage(
        image_id=str(post["id"]), path=nightcore.ART_DIR / "x.jpg",
        width=3200, height=1800))
    pool.build(target=7, jobs=6)
    assert len(pool.images) == 7


def test_failed_ingest_is_not_retried_forever(pool, monkeypatch):
    attempts = {}

    monkeypatch.setattr(ArtPool, "_query",
                        lambda self, tags, limit=100: fake_posts(3000, 10))

    def flaky(self, post):
        pid = str(post["id"])
        attempts[pid] = attempts.get(pid, 0) + 1
        raise ValueError("too dark (mean luma 20)")

    monkeypatch.setattr(ArtPool, "_ingest", flaky)
    pool.build(target=5, jobs=4)

    assert attempts, "nothing was attempted"
    assert max(attempts.values()) == 1, "a known-bad image was retried"
    assert pool.images == []


def test_build_checkpoints_the_manifest(pool, monkeypatch):
    """An interrupted build must not lose the images it already fetched."""
    monkeypatch.setattr(ArtPool, "_query",
                        lambda self, tags, limit=100: fake_posts(4000, 40))
    monkeypatch.setattr(ArtPool, "_ingest", lambda self, post: AnimeImage(
        image_id=str(post["id"]),
        path=nightcore.ART_DIR / f"{post['id']}.jpg",
        width=3200, height=1800))
    pool.build(target=12, jobs=4)

    rows = json.loads(nightcore.ART_MANIFEST.read_text(encoding="utf-8"))
    assert len(rows) == 12
    assert {r["image_id"] for r in rows} == {i.image_id for i in pool.images}


def test_suitable_reports_why_not(pool):
    ok, why = pool._suitable({"id": 1, "rating": "general", "width": 2000,
                              "height": 3000, "tags": "scenery",
                              "file_url": "x.jpg"}, set())
    assert not ok and "too tall" in why

    ok, why = pool._suitable({"id": 2, "rating": "questionable",
                              "width": 3200, "height": 1800, "tags": "",
                              "file_url": "x.jpg"}, set())
    assert not ok and "safe-rated" in why
