"""
The classifier is a gate, so its failure modes matter as much as its hits.

Everything here drives `classify`, which is pure: the network lookups are
tested separately by substitution, because what needs pinning down is the
*rules*, not whether MusicBrainz was up this morning.
"""

from __future__ import annotations

import pytest

import tagging
from errors import NoTagMatch
from tagging import Metadata, classify


def tag_for(**kwargs) -> str:
    return classify(Metadata(**kwargs)).tag


# ── The happy paths ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("meta, expected", [
    # English-language songs fall through to genre.
    (dict(itunes_genre="Pop", language="eng"), "pop"),
    (dict(itunes_genre="Hip-Hop/Rap", language="eng"), "hiphop"),
    (dict(itunes_genre="Música Mexicana", language=None), "spanish"),
    # MusicBrainz beats iTunes, because it is the specific one.
    (dict(itunes_genre="Dance", mb_tags=("phonk",), language="eng"), "phonk"),
    (dict(itunes_genre="Dance", mb_tags=("hardstyle",), language="eng"),
     "hardstyle"),
    # Language wins over genre for non-English songs.
    (dict(itunes_genre="Metal", language="deu"), "german"),
    (dict(itunes_genre="Dance", mb_tags=("electro house",), language="fra"),
     "french"),
    (dict(itunes_genre="Pop", language="mul"), "multilingual"),
    # Korean has no language tag of its own; K-pop is the bucket.
    (dict(itunes_genre="Pop", language="kor"), "kpop"),
])
def test_resolves(meta, expected):
    assert tag_for(**meta) == expected


@pytest.mark.parametrize("genre_tag", ["anime", "folk", "k-pop"])
def test_exempt_genres_beat_the_language_rule(genre_tag):
    """
    The wiki exempts anime, folk and K-pop from the language rule.

    Without this, a Japanese anime opening would be filed under
    #playlist_japanese and never reach the anime playlist at all.
    """
    tag = tag_for(mb_tags=(genre_tag,), language="jpn", itunes_genre="Pop")
    assert tag in tagging.LANGUAGE_EXEMPT


def test_longest_genre_key_wins():
    """
    "future house" must not be swallowed by "house".

    Dict iteration order would happily return `house` here; the sort in
    `_MB_KEYS` is the only thing preventing it, and it is the kind of thing a
    later edit silently breaks.
    """
    assert tag_for(mb_tags=("house", "future house"), language="eng") == \
        "futurehouse"
    assert tag_for(mb_tags=("trance", "uplifting trance"),
                   language="eng") == "uplifting"


def test_compound_tags_match_on_word_boundary():
    """MusicBrainz tags arrive as "melodic dubstep" at least as often as clean."""
    assert tag_for(mb_tags=("melodic dubstep",), language="eng") == "dubstep"
    # ...but a word that merely *contains* a key must not match.
    assert tag_for(mb_tags=("housewarming",), itunes_genre="Pop",
                   language="eng") == "pop"


def test_every_emitted_tag_is_published_on_the_wiki():
    """
    Nothing may escape the vocabulary.

    A typo in either mapping table would otherwise produce a hashtag that
    looks right, uploads fine, and is silently ignored by the network.
    """
    emitted = set(tagging.MB_GENRE_MAP.values())
    emitted |= set(tagging.ITUNES_GENRE_MAP.values())
    emitted |= set(tagging.LANGUAGE_MAP.values())
    assert emitted <= tagging.VALID_TAGS, emitted - tagging.VALID_TAGS


# ── The rejections, which are the point ─────────────────────────────────────

def test_unmapped_genre_is_rejected():
    with pytest.raises(NoTagMatch):
        tag_for(itunes_genre="Comedy", language="eng")


def test_hidden_playlists_resolve():
    M = tagging.Metadata
    assert tagging.classify(M(language="eng", mb_tags=("happy hardcore",))).tag == "happyhardcore"
    assert tagging.classify(M(language="eng", itunes_genre="Reggae")).tag == "reggae"
    assert tagging.classify(M(language="nob", itunes_genre="Pop")).tag == "norwegian"
    assert tagging.classify(M(language="ces", itunes_genre="Pop")).tag == "czechia"


def test_christmas_in_german_and_french_use_seasonal_playlists():
    M = tagging.Metadata
    assert tagging.classify(M(language="deu", mb_tags=("christmas music",))).tag == "germanchristmas"
    assert tagging.classify(M(language="fra", mb_tags=("christmas music",))).tag == "frenchchristmas"


def test_language_without_a_playlist_is_rejected():
    """
    Swedish is not on the wiki's list, and a Swedish song has nowhere to go.

    Falling back to its genre would be the tempting thing to do and would
    break the rule the wiki is most explicit about.
    """
    with pytest.raises(NoTagMatch):
        tag_for(itunes_genre="Pop", language="swe")


def test_no_metadata_at_all_is_rejected():
    with pytest.raises(NoTagMatch):
        tag_for()


# ── Degradation when a source is down ───────────────────────────────────────

def test_gather_survives_one_source_failing(monkeypatch):
    monkeypatch.setattr(tagging, "lookup_itunes",
                        lambda a, t: (_ for _ in ()).throw(OSError("down")))
    monkeypatch.setattr(tagging, "lookup_musicbrainz",
                        lambda a, t: ("deu", ("metal",)))
    meta = tagging.gather("Rammstein", "Du hast", verbose=False)
    assert meta.language == "deu"
    assert classify(meta).tag == "german"


def test_total_blackout_is_distinguishable_from_a_bad_song(monkeypatch):
    """
    Both raise NoTagMatch, but only one means "never try this song again".

    `make_one` writes a permanent skip record on NoTagMatch; if an internet
    outage produced the same exception type, one bad hour would blacklist six
    perfectly good songs for good.
    """
    boom = lambda *a, **k: (_ for _ in ()).throw(OSError("down"))  # noqa: E731
    monkeypatch.setattr(tagging, "lookup_itunes", boom)
    monkeypatch.setattr(tagging, "lookup_musicbrainz", boom)

    with pytest.raises(tagging.LookupUnavailable):
        tagging.classify_song("Anyone", "Anything", verbose=False)

    assert issubclass(tagging.LookupUnavailable, NoTagMatch)


# ── Helpers used by the picker ──────────────────────────────────────────────

def test_best_recording_prefers_the_widely_released_take():
    """
    MusicBrainz scores every plausible match 100, so score cannot break ties.

    The canonical recording is the one licensed onto two dozen compilations;
    the stub with one release has no tags and no work relation, which is the
    whole reason we are calling MusicBrainz.
    """
    stub = {"id": "stub", "title": "Alors on danse", "score": 100,
            "releases": [{}]}
    canonical = {"id": "real", "title": "Alors on danse", "score": 100,
                 "releases": [{}] * 25}
    assert tagging._best_recording([stub, canonical],
                                   "Alors on danse")["id"] == "real"


def test_best_recording_avoids_dj_mix_segments():
    mix = {"id": "mix", "title": "Alors on danse", "score": 100,
           "disambiguation": "part of a “Grandmix 2010” DJ-mix",
           "releases": [{}] * 40}
    plain = {"id": "plain", "title": "Alors on danse", "score": 100,
             "releases": [{}] * 3}
    assert tagging._best_recording([mix, plain],
                                   "Alors on danse")["id"] == "plain"


def test_language_comes_from_the_work_not_the_release():
    """
    A release's text-representation describes its *track listing*, not the
    lyrics, so "Alors on danse" reads as English on any English compilation.
    Reading the linked work is the only way to get `fra`.
    """
    detail = {
        "relations": [{"work": {"title": "Alors on danse",
                                "languages": ["fra"]}}],
        "releases": [{"status": "Official",
                      "text-representation": {"language": "eng"}}],
    }
    assert tagging._recording_language(detail) == "fra"


def test_language_falls_back_to_official_releases():
    detail = {"relations": [], "releases": [
        {"status": "Bootleg", "text-representation": {"language": "eng"}},
        {"status": "Official", "text-representation": {"language": "deu"}},
    ]}
    assert tagging._recording_language(detail) == "deu"


def test_multi_language_work_is_multilingual():
    detail = {"relations": [{"work": {"languages": ["eng", "spa"]}}]}
    assert tagging._recording_language(detail) == "mul"


# ── Picking which MusicBrainz recording to read ─────────────────────────────

def rec(rid, disambiguation="", releases=1, score=100, title="Take Me Out"):
    return {"id": rid, "title": title, "score": score,
            "disambiguation": disambiguation, "releases": [{}] * releases}


def test_live_bootlegs_are_never_the_canonical_take():
    """
    Searching "Take Me Out" by Franz Ferdinand returns ten entries all scored
    100 with one release each, eight of them live bootlegs. Live recordings
    carry no genre tags, so picking one empties the MusicBrainz half of the
    evidence and the classifier falls back to iTunes — which answers "Pop".
    """
    pool = [rec("live1", "live, 2006-04-17: Ricoh Coliseum, Toronto"),
            rec("live2", "live, 2009-03-05: Barrowland Ballroom"),
            rec("studio")]
    assert tagging._best_recording(pool, "Take Me Out")["id"] == "studio"


def test_radio_edit_is_not_treated_as_derivative():
    """
    A radio edit is the commercial single, often the most-released version,
    and on "Alors on danse" it is the only entry carrying the work relation
    that proves the song is French. Excluding it picks a one-release stub
    whose release metadata says English.
    """
    pool = [rec("stub", "", releases=1, title="Alors on danse"),
            rec("single", "radio edit", releases=25, title="Alors on danse")]
    assert tagging._best_recording(pool, "Alors on danse")["id"] == "single"


def test_release_count_outranks_a_clean_disambiguation():
    pool = [rec("stub", "", releases=1),
            rec("canonical", "", releases=30)]
    assert tagging._best_recording(pool, "Take Me Out")["id"] == "canonical"


def test_artist_genres_rescue_an_untagged_recording(monkeypatch):
    """
    MusicBrainz genre coverage is far better per-artist than per-track. An
    untagged recording should borrow from the artist rather than concede the
    decision to iTunes.
    """
    monkeypatch.setattr(tagging, "_mb_throttle", lambda: None)
    monkeypatch.setattr(tagging, "_get_json", lambda url, **kw: {
        "genres": [{"name": "alternative rock"}, {"name": "indie rock"}]})
    got = tagging._artist_genres(
        {"artist-credit": [{"artist": {"id": "abc"}}]})
    assert "alternative rock" in got
    assert tagging._match_genre(tuple(got), "Pop") == "rock"


def test_no_artist_credit_means_no_fallback(monkeypatch):
    monkeypatch.setattr(tagging, "_get_json",
                        lambda *a, **k: pytest.fail("should not be called"))
    assert tagging._artist_genres({}) == []


# ── Exempt genres take priority over longer generic keys ────────────────────

@pytest.mark.parametrize("tags, expected", [
    # BLACKPINK is tagged both; plain longest-match picks "hip hop" (7 chars)
    # over "k pop" (5), the song stops being exempt, and the language rule
    # sends it to #playlist_multilingual instead of the K-pop playlist.
    (("hip hop", "k-pop", "pop"), "kpop"),
    (("anime song", "j-pop"), "anime"),
    (("folk", "indie rock"), "folk"),
])
def test_exempt_genres_win_the_match(tags, expected):
    assert tagging._match_genre(tags, None) == expected


def test_jpop_does_not_collapse_to_pop():
    """"j-pop" flattens to "j pop", where the bare "pop" key matches."""
    assert tagging._match_genre(("electropop", "j-pop"), None) == "japanese"


def test_blackpink_end_to_end_is_kpop():
    meta = Metadata(itunes_genre="K-Pop", language="mul",
                    mb_tags=("hip hop", "k-pop", "pop"))
    assert classify(meta).tag == "kpop"


def test_franz_ferdinand_end_to_end_is_rock():
    meta = Metadata(itunes_genre="Pop", language="eng",
                    mb_tags=("alternative dance", "alternative rock",
                             "art rock", "dance-punk", "indie rock"))
    assert classify(meta).tag == "rock"


# ── Regressions: valid-but-wrong tags seen in the field ─────────────────────

def test_hardcore_hip_hop_is_hiphop_not_hardcore():
    assert tagging._match_genre(("hardcore hip hop", "hip hop"), "Hip-Hop/Rap") == "hiphop"


def test_dance_pop_is_pop_not_unclassified_edm():
    tags = ("dance-pop", "electro", "electronic", "electropop", "pop")
    assert tagging._match_genre(tags, "Pop") == "pop"


def test_itunes_kpop_beats_musicbrainz_dance_pop():
    assert tagging._match_genre(("dance-pop", "pop"), "K-Pop") == "kpop"


def test_stray_soundtrack_tag_does_not_beat_a_real_genre():
    assert tagging._match_genre(("hip hop", "rap", "soundtrack"), None) == "hiphop"
    assert tagging._match_genre(("soundtrack",), None) == "moviesoundtracks"


def test_reggae_pop_is_reggae():
    assert tagging._match_genre(("pop", "reggae-pop", "roots reggae"), "Pop") == "reggae"


def test_itunes_fallback_ignores_other_artists(monkeypatch):
    monkeypatch.setattr(tagging, "_get_json", lambda url, **kw: {"results": [
        {"primaryGenreName": "Reggae", "trackName": "Other Song",
         "artistName": "Someone Else"}]})
    assert tagging.lookup_itunes("Destin Conrad", "I LOVE YOUR BODY") is None
