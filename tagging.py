"""
Resolve a song to exactly one Nightcore Network playlist hashtag.

The member guidance (wiki.nightcore.com/member-guidance/featuring-videos)
publishes a fixed list of `#playlist_*` tags and three rules:

  * exactly one playlist hashtag per video — their automation reads one and
    breaks on two;
  * non-English songs use their *language* playlist, not their genre;
  * anime, folk and K-pop are exempt from that language rule.

So a song we cannot confidently place is a song we don't publish. This module
is therefore a gate as much as a classifier, and it runs *before* anything is
downloaded — two HTTP requests are a cheap way to avoid a five-minute render
we'd have to throw away.

Sources, both keyless:

  * iTunes Search — near-universal coverage of commercial releases, but only a
    coarse genre ("Dance", "Pop", "Hip-Hop/Rap").
  * MusicBrainz    — patchier, rate-limited to 1 req/s, but it is the only
                     free source that knows the difference between hardstyle
                     and drum and bass, and the only one that publishes the
                     language a song is *sung* in.

Either source may be down; the classifier degrades to whichever is up. Only a
total blackout gives up, and it gives up with `LookupUnavailable` so the caller
can tell "the internet is broken" from "this song is unsuitable".
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass

from errors import NoTagMatch

# MusicBrainz requires a User-Agent that identifies the application and gives
# them a way to contact you; requests without one get throttled to 503s. Set
# NIGHTCORE_CONTACT to an email or a project URL — it is the difference between
# a reliable lookup and one that silently degrades to iTunes-only.
_CONTACT = os.environ.get("NIGHTCORE_CONTACT") or "https://gamer3514.co.uk"
USER_AGENT = f"nightcore-tool/1.0 ( {_CONTACT} )"

ITUNES_SEARCH = "https://itunes.apple.com/search"
MUSICBRAINZ_SEARCH = "https://musicbrainz.org/ws/2/recording"
MUSICBRAINZ_ARTIST = "https://musicbrainz.org/ws/2/artist"

# MusicBrainz asks for no more than one request per second and will start
# returning 503s if you ignore it. One lock, one timestamp, honoured process-
# wide — the scheduler is single-threaded but tests are not.
_MB_LOCK = threading.Lock()
_MB_LAST_CALL = 0.0
MB_MIN_INTERVAL = 1.1


# ─────────────────────────────────────────────────────────────────────────────
# The tag vocabulary
# ─────────────────────────────────────────────────────────────────────────────

# Verbatim from the wiki, minus the leading "#playlist_". Nothing outside this
# set may ever be emitted.
VALID_TAGS = frozenset("""
8d ai alternative anime bigroom brazilianfunk country drumandbass drumstep
dubstep dutch eurodance folk french futurebass futurehouse german
gamesoundtracks handsup hardbass hardcore hardstyle hiphop house hungarian
instrumental italian japanese jazz krushfunk kpop mandarin mashups
moviesoundtracks multilingual nerdcore phonk polish pop portuguese relaxing
rhythmandblues rock romanian russian sad serbian spanish switchingvocals
synthpop techno trance trap tvsoundtracks unclassifiededm uplifting vietnamese
christmas germanchristmas halloween
""".split()) | frozenset("""
happyhardcore norwegian danish ukrainian eurobeat basque frenchchristmas
croatian albanian filipino irish reggae finnish czechia slovak musical
parodies maleversion
""".split())
# The second group is playlists that exist on the network but are missing from
# the wiki list. Their tag names are inferred from the wiki's own convention
# (lowercase, no separators), not published — if one is rejected, fix it here.

# A Christmas song in these languages has its own seasonal playlist, which beats
# the plain language playlist.
CHRISTMAS_BY_LANGUAGE = {"german": "germanchristmas", "french": "frenchchristmas"}

# Genres the wiki exempts from the "non-English songs use their language
# playlist" rule. These three beat the language check.
LANGUAGE_EXEMPT = frozenset({"anime", "folk", "kpop"})

# ISO 639, both the two- and three-letter spellings, because MusicBrainz
# returns -3 on releases and -1 in a few other places.
#
# Korean maps to `kpop` rather than a `#playlist_korean`, because the wiki has
# no Korean language tag — K-pop *is* the Korean bucket.
LANGUAGE_MAP: dict[str, str] = {
    "deu": "german", "ger": "german", "de": "german",
    "fra": "french", "fre": "french", "fr": "french",
    "spa": "spanish", "es": "spanish",
    "por": "portuguese", "pt": "portuguese",
    "rus": "russian", "ru": "russian",
    "pol": "polish", "pl": "polish",
    "ron": "romanian", "rum": "romanian", "ro": "romanian",
    "srp": "serbian", "sr": "serbian",
    "hun": "hungarian", "hu": "hungarian",
    "ita": "italian", "it": "italian",
    "jpn": "japanese", "ja": "japanese",
    "kor": "kpop", "ko": "kpop",
    "zho": "mandarin", "cmn": "mandarin", "chi": "mandarin", "zh": "mandarin",
    "vie": "vietnamese", "vi": "vietnamese",
    "nld": "dutch", "dut": "dutch", "nl": "dutch",
    "mul": "multilingual",
    "nor": "norwegian", "nob": "norwegian", "nno": "norwegian",
    "no": "norwegian", "nb": "norwegian", "nn": "norwegian",
    "dan": "danish", "da": "danish",
    "ukr": "ukrainian", "uk": "ukrainian",
    "eus": "basque", "baq": "basque", "eu": "basque",
    "hrv": "croatian", "hr": "croatian",
    "sqi": "albanian", "alb": "albanian", "sq": "albanian",
    "fil": "filipino", "tgl": "filipino", "tl": "filipino",
    "gle": "irish", "ga": "irish",
    "fin": "finnish", "fi": "finnish",
    "ces": "czechia", "cze": "czechia", "cs": "czechia",
    "slk": "slovak", "slo": "slovak", "sk": "slovak",
}

ENGLISH = frozenset({"eng", "en"})

# MusicBrainz community tags → our vocabulary. Checked before iTunes because
# this is where the specificity lives: iTunes calls everything below "Dance".
#
# Matched longest-key-first (see `_match_genre`), so "future house" wins over
# "house" and "uplifting trance" wins over "trance".
MB_GENRE_MAP: dict[str, str] = {
    "hardstyle": "hardstyle", "rawstyle": "hardstyle",
    "drum and bass": "drumandbass", "drum n bass": "drumandbass",
    "dnb": "drumandbass", "liquid funk": "drumandbass", "jungle": "drumandbass",
    "drumstep": "drumstep",
    "dubstep": "dubstep", "brostep": "dubstep", "riddim": "dubstep",
    "phonk": "phonk", "drift phonk": "phonk",
    "trap": "trap", "edm trap": "trap",
    "future bass": "futurebass",
    "future house": "futurehouse", "bass house": "futurehouse",
    "big room": "bigroom", "big room house": "bigroom",
    "hands up": "handsup", "handsup": "handsup",
    "hardbass": "hardbass",
    "hardcore": "hardcore", "uptempo hardcore": "hardcore",
    "gabber": "hardcore", "frenchcore": "hardcore",
    "happy hardcore": "happyhardcore", "happyhardcore": "happyhardcore",
    "eurodance": "eurodance",
    "eurobeat": "eurobeat", "italo disco": "eurobeat",
    "reggae": "reggae", "dub reggae": "reggae", "roots reggae": "reggae",
    "dancehall": "reggae", "ska": "reggae",
    "musical": "musical", "musical theatre": "musical",
    "show tunes": "musical", "cast recording": "musical",
    "parody": "parodies", "parodies": "parodies",
    "weird al": "parodies",
    "uplifting trance": "uplifting",
    "trance": "trance", "psytrance": "trance",
    "techno": "techno", "minimal techno": "techno",
    "house": "house", "deep house": "house", "progressive house": "house",
    "tech house": "house", "electro house": "house",
    "synthpop": "synthpop", "synth pop": "synthpop", "synthwave": "synthpop",
    "nerdcore": "nerdcore",
    "mashup": "mashups", "bootleg": "mashups",
    "funk carioca": "brazilianfunk", "baile funk": "brazilianfunk",
    "brazilian funk": "brazilianfunk",
    "krushfunk": "krushfunk",
    "video game music": "gamesoundtracks", "chiptune": "gamesoundtracks",
    "television soundtrack": "tvsoundtracks", "tv theme": "tvsoundtracks",
    "film score": "moviesoundtracks", "soundtrack": "moviesoundtracks",
    "christmas music": "christmas", "christmas": "christmas",
    "halloween": "halloween",
    "ambient": "relaxing", "lo fi": "relaxing", "lofi": "relaxing",
    "chillout": "relaxing", "downtempo": "relaxing",
    "anime": "anime", "anime song": "anime", "anison": "anime",
    "k pop": "kpop", "kpop": "kpop",
    # "j-pop" flattens to "j pop", where the bare "pop" key matches on a word
    # boundary and wins. Map it explicitly or every J-pop track lands on
    # #playlist_pop instead of #playlist_japanese.
    "j pop": "japanese", "j rock": "japanese", "city pop": "japanese",
    "folk": "folk", "folk music": "folk", "traditional folk": "folk",
    # "hardcore hip hop" must outrank the bare "hardcore" key (Eminem, 50 Cent).
    "hip hop": "hiphop", "rap": "hiphop", "hardcore hip hop": "hiphop",
    "gangsta rap": "hiphop", "pop rap": "hiphop", "trap rap": "hiphop",
    "r b": "rhythmandblues", "rnb": "rhythmandblues",
    "alternative r b": "rhythmandblues",
    # Pop made with synths/dance beats is still pop. Without these, the generic
    # "dance"/"electronic" keys below turn "dance-pop" into an EDM tag.
    "dance pop": "pop", "electropop": "pop", "electro pop": "pop",
    "teen pop": "pop", "art pop": "pop", "pop rock": "pop",
    "contemporary r b": "rhythmandblues", "soul": "rhythmandblues",
    "rock": "rock", "metal": "rock", "punk": "rock", "pop punk": "rock",
    "alt rock": "rock", "alternative rock": "rock",
    "indie rock": "alternative", "britpop": "alternative",
    "alternative": "alternative", "indie": "alternative",
    "indie pop": "alternative",
    "country": "country",
    "jazz": "jazz",
    "pop": "pop",
    "classical": "instrumental", "orchestral": "instrumental",
    "instrumental": "instrumental",
    "edm": "unclassifiededm", "electronic": "unclassifiededm",
    "dance": "unclassifiededm",
}

# iTunes `primaryGenreName` → our vocabulary. Keys are pre-flattened (see
# `_flatten`), so "Hip-Hop/Rap" and "Música Mexicana" both land without any
# per-call punctuation or accent handling.
#
# Deliberately incomplete: genres with no sensible home on the wiki (Reggae,
# Worldwide, Christian & Gospel, Comedy, Spoken Word…) are absent, and their
# absence is exactly what rejects the song.
ITUNES_GENRE_MAP: dict[str, str] = {
    "pop": "pop",
    "hip hop rap": "hiphop",
    "r b soul": "rhythmandblues",
    "blues": "rhythmandblues",
    "rock": "rock",
    "metal": "rock",
    "hard rock": "rock",
    "punk": "rock",
    "alternative": "alternative",
    "singer songwriter": "alternative",
    "indie pop": "alternative",
    "country": "country",
    "dance": "unclassifiededm",
    "electronic": "unclassifiededm",
    "house": "unclassifiededm",
    "techno": "unclassifiededm",
    "jazz": "jazz",
    "k pop": "kpop",
    "j pop": "japanese",
    "anime": "anime",
    "soundtrack": "moviesoundtracks",
    "folk": "folk",
    "classical": "instrumental",
    "new age": "relaxing",
    "easy listening": "relaxing",
    "holiday": "christmas",
    "christmas": "christmas",
    "reggae": "reggae",
    "musicals": "musical",
    "original cast": "musical",
    "stage screen": "musical",
    # The Latin shelf. The language rule normally catches these first, but
    # iTunes is often the only source that has heard of a brand-new regional
    # hit, and then this is the whole classification.
    "latin": "spanish",
    "latino": "spanish",
    "pop latino": "spanish",
    "musica mexicana": "spanish",
    "regional mexicano": "spanish",
    "reggaeton y hip hop": "spanish",
    "brazilian": "portuguese",
    "sertanejo": "portuguese",
    "mpb": "portuguese",
    "french pop": "french",
    "german pop": "german",
}


# ─────────────────────────────────────────────────────────────────────────────
# Lookup results
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Classification:
    """What we decided, and enough of why to write a useful skip reason."""

    tag: str
    language: str | None
    itunes_genre: str | None
    mb_tags: tuple[str, ...]

    @property
    def hashtag(self) -> str:
        return f"#playlist_{self.tag}"


@dataclass(frozen=True)
class Metadata:
    """Raw facts gathered about a song, before any judgement is applied."""

    itunes_genre: str | None = None
    language: str | None = None
    mb_tags: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.itunes_genre or self.language or self.mb_tags)


# ─────────────────────────────────────────────────────────────────────────────
# Normalisation and matching
# ─────────────────────────────────────────────────────────────────────────────

def _flatten(text: str) -> str:
    """Lowercase, strip accents, reduce punctuation to single spaces."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


# Longest key first, so a song tagged both "future house" and "house" lands on
# futurehouse. Computed once: the maps are constants.
_MB_KEYS = sorted(MB_GENRE_MAP, key=len, reverse=True)

# The subset mapping to anime / folk / kpop, checked before everything else.
_EXEMPT_KEYS = [k for k in _MB_KEYS if MB_GENRE_MAP[k] in LANGUAGE_EXEMPT]
# J-pop and friends are a genre *and* a language; they must beat the pop-ish
# keys ("electropop" is longer than "j pop") that would otherwise win on length.
_JAPAN_KEYS = [k for k in _MB_KEYS if MB_GENRE_MAP[k] == "japanese"]
# Weak signals, tried last: a catch-all EDM label, or a "soundtrack" tag that
# is routinely stuck on a chart hit (Kanye's "Stronger" is tagged soundtrack).
_WEAK_TAGS = {"unclassifiededm", "moviesoundtracks", "tvsoundtracks",
              "gamesoundtracks"}
_GENERIC_KEYS = [k for k in _MB_KEYS if MB_GENRE_MAP[k] in _WEAK_TAGS]
_SPECIFIC_KEYS = [k for k in _MB_KEYS if k not in _GENERIC_KEYS]


def _match_genre(mb_tags: tuple[str, ...], itunes_genre: str | None) -> str | None:
    """
    Best genre tag for this song, or None.

    MusicBrainz first — a chart single tagged "phonk" there is called "Dance"
    by iTunes, and "phonk" is the answer the network wants.
    """
    flat_tags = [_flatten(t) for t in mb_tags]

    def scan(keys) -> str | None:
        for key in keys:
            for tag in flat_tags:
                # Word-boundary containment, not equality: MusicBrainz tags
                # arrive as "melodic dubstep" and "german hip hop" at least as
                # often as they arrive clean.
                if tag == key or re.search(
                        rf"(?:^| ){re.escape(key)}(?:$| )", tag):
                    return MB_GENRE_MAP[key]
        return None

    # The three exempt genres get first refusal. They are the only ones whose
    # presence changes which *rule* applies — anime, folk and K-pop override
    # the language requirement — so losing them to a longer, more generic key
    # changes the outcome, not just the label. BLACKPINK is tagged both
    # "k-pop" and "hip hop"; plain longest-match picks "hip hop", the song
    # stops being exempt, and it lands on #playlist_multilingual instead of
    # the K-pop playlist the wiki asks for.
    itunes_tag = (ITUNES_GENRE_MAP.get(_flatten(itunes_genre))
                  if itunes_genre else None)
    # iTunes saying "K-Pop" is as authoritative as a MusicBrainz tag, and
    # TWICE's MusicBrainz tags are just "dance-pop".
    if itunes_tag in LANGUAGE_EXEMPT:
        return scan(_EXEMPT_KEYS) or itunes_tag
    # Catch-all EDM keys lose to everything specific: a song tagged both
    # "electronic" and "pop" is a pop song.
    return (scan(_EXEMPT_KEYS) or scan(_JAPAN_KEYS) or scan(_SPECIFIC_KEYS)
            or scan(_GENERIC_KEYS) or itunes_tag)


def classify(meta: Metadata) -> Classification:
    """
    Apply the wiki's rules to gathered metadata. Pure — no I/O.

    Order matters and is the whole point:
      1. work out the genre;
      2. anime / folk / kpop are exempt from the language rule, so they win;
      3. otherwise a non-English song must use its language playlist — and if
         its language has no playlist, it is not publishable here;
      4. otherwise use the genre;
      5. otherwise give up.
    """
    genre = _match_genre(meta.mb_tags, meta.itunes_genre)
    lang = (meta.language or "").strip().lower() or None

    def decide(tag: str) -> Classification:
        assert tag in VALID_TAGS, f"{tag!r} is not a published playlist tag"
        return Classification(tag=tag, language=lang,
                              itunes_genre=meta.itunes_genre,
                              mb_tags=meta.mb_tags)

    if genre in LANGUAGE_EXEMPT:
        return decide(genre)

    if genre == "christmas" and LANGUAGE_MAP.get(lang or "") in CHRISTMAS_BY_LANGUAGE:
        return decide(CHRISTMAS_BY_LANGUAGE[LANGUAGE_MAP[lang]])

    if lang and lang not in ENGLISH:
        if lang in LANGUAGE_MAP:
            return decide(LANGUAGE_MAP[lang])
        raise NoTagMatch(
            f"no playlist tag: language {lang!r} has no playlist on the wiki")

    if genre:
        return decide(genre)

    raise NoTagMatch(
        f"no playlist tag: genre={meta.itunes_genre!r} "
        f"mb_tags={list(meta.mb_tags)[:6]} language={lang!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Network lookups
# ─────────────────────────────────────────────────────────────────────────────

def _get_json(url: str, *, timeout: int = 20) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    # MusicBrainz sheds load with 503s and iTunes with 403s; one blip used to
    # silently blank a song's whole genre evidence, so retry before giving up.
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def lookup_itunes(artist: str, title: str) -> str | None:
    """`primaryGenreName` for the best-matching track, or None."""
    params = urllib.parse.urlencode({
        "term": f"{artist} {title}",
        "media": "music", "entity": "song", "limit": "5",
    })
    data = _get_json(f"{ITUNES_SEARCH}?{params}")
    if not isinstance(data, dict):
        return None

    wanted_title, wanted_artist = _flatten(title), _flatten(artist)
    best: str | None = None
    for row in data.get("results", []):
        genre = row.get("primaryGenreName")
        if not genre:
            continue
        row_title = _flatten(row.get("trackName") or "")
        row_artist = _flatten(row.get("artistName") or "")
        # Exact title match plus an artist that at least overlaps. Without the
        # title check the top hit for a new single is routinely a different
        # track from the same album.
        artist_ok = wanted_artist in row_artist or row_artist in wanted_artist
        if row_title == wanted_title and artist_ok:
            return genre
        # Fallback only for the right artist. Taking any row's genre handed
        # Destin Conrad's R&B track the "Reggae" of an unrelated song.
        if artist_ok:
            best = best or genre
    return best


def _mb_throttle() -> None:
    """Honour MusicBrainz's one-request-per-second rule, process-wide."""
    global _MB_LAST_CALL
    with _MB_LOCK:
        wait = MB_MIN_INTERVAL - (time.monotonic() - _MB_LAST_CALL)
        if wait > 0:
            time.sleep(wait)
        _MB_LAST_CALL = time.monotonic()


def lookup_musicbrainz(artist: str, title: str) -> tuple[str | None, tuple[str, ...]]:
    """
    (lyrics language, genre names) for the best-matching recording.

    Two requests. The *search* endpoint returns recordings without their tags
    and without any language; only the *lookup* endpoint, asked for
    `inc=releases+tags+genres+work-rels`, carries all of it. So: search for an
    id, then look that id up.
    """
    _mb_throttle()
    query = (f'recording:"{_escape_lucene(title)}" '
             f'AND artist:"{_escape_lucene(artist)}"')
    params = urllib.parse.urlencode({"query": query, "fmt": "json", "limit": "10"})
    data = _get_json(f"{MUSICBRAINZ_SEARCH}?{params}")
    if not isinstance(data, dict):
        return None, ()

    chosen = _best_recording(data.get("recordings") or [], title)
    if not chosen or not chosen.get("id"):
        return None, ()

    _mb_throttle()
    detail = _get_json(f"{MUSICBRAINZ_SEARCH}/{chosen['id']}"
                       f"?inc=releases+tags+genres+work-rels&fmt=json")
    if not isinstance(detail, dict):
        return None, ()

    # Curated genres and free-form tags both feed the matcher. Genres are the
    # cleaner vocabulary; tags are where the niche EDM labels actually appear.
    names: dict[str, None] = {}
    for row in (detail.get("genres") or []) + (detail.get("tags") or []):
        if isinstance(row, dict) and row.get("name"):
            names.setdefault(row["name"], None)

    # Plenty of recordings are simply untagged — MusicBrainz's genre coverage
    # is far better at the artist level than the track level. Falling back is
    # a real loss of precision (an artist can span genres) but it beats
    # handing the decision to iTunes, which answers "Pop" for everything it
    # cannot place.
    if not names:
        names = dict.fromkeys(_artist_genres(chosen))

    return _recording_language(detail), tuple(names)


def _artist_genres(recording: dict) -> list[str]:
    """Curated genres for the recording's lead artist, or []."""
    credits = recording.get("artist-credit") or []
    artist_id = ((credits[0].get("artist") or {}).get("id")
                 if credits else None)
    if not artist_id:
        return []
    try:
        _mb_throttle()
        detail = _get_json(f"{MUSICBRAINZ_ARTIST}/{artist_id}"
                           f"?inc=tags+genres&fmt=json")
    except Exception:
        return []
    rows = (detail.get("genres") or []) + (detail.get("tags") or [])
    return [r["name"] for r in rows if isinstance(r, dict) and r.get("name")]


# Recordings that are not the studio master. A live take, a DJ-mix segment or
# a demo is the same song, but its metadata belongs to that performance — and
# in practice such entries carry no genre tags at all, which is worse than
# useless: it silently empties the richest half of our evidence.
# "radio edit" is deliberately absent: it is the commercial single, often the
# most-released version of a track, and on "Alors on danse" it is the only
# entry carrying the work relation that tells us the song is in French.
# Excluding it sends the pick to a one-release stub and the language silently
# becomes English.
_DERIVATIVE = re.compile(
    r"\blive\b|dj.?mix|megamix|mashup|medley|\bdemo\b|\bkaraoke\b"
    r"|instrumental|\bremix\b|\bacoustic\b|\bcover\b", re.I)


def _best_recording(recordings: list[dict], title: str) -> dict | None:
    """
    Pick the canonical studio recording out of a search page.

    MusicBrainz scores every plausible match 100, so score alone is no help.
    Searching "Take Me Out" by Franz Ferdinand returns ten entries all scored
    100 with one release apiece, and eight of them are live bootlegs.

    Three signals, in order:

      1. not a live take / mix segment / demo — those carry no tags;
      2. release count. The canonical recording is the one licensed onto two
         dozen compilations, and it is the one that accumulates genres and
         the work relation;
      3. no disambiguation, which is how MusicBrainz spells "just the song".
         This only breaks ties — putting it above release count picks a
         pristine one-release stub over the real thing, which costs the
         language lookup its work relation.
    """
    wanted = _flatten(title)
    exact = [r for r in recordings if _flatten(r.get("title") or "") == wanted]
    pool = exact or recordings
    if not pool:
        return None

    def rank(r: dict) -> tuple[int, int, int, int]:
        disambiguation = r.get("disambiguation") or ""
        return (1 if _DERIVATIVE.search(disambiguation) else 0,
                -len(r.get("releases") or []),
                1 if disambiguation else 0,
                -(r.get("score") or 0))

    return min(pool, key=rank)


def _recording_language(detail: dict) -> str | None:
    """
    The language the song is *sung* in.

    Read off the linked Work, which is where MusicBrainz records lyrics
    language. The obvious-looking alternative — a release's
    `text-representation.language` — describes the language of that release's
    title and track listing, so "Alors on danse" comes back as English,
    German, French and multilingual depending on which compilation you happen
    to read, and the first one alphabetically is English. That would have
    quietly mis-tagged every non-English song that ever charted abroad.
    """
    for relation in detail.get("relations") or []:
        work = relation.get("work") or {}
        languages = work.get("languages") or (
            [work["language"]] if work.get("language") else [])
        if languages:
            # A work with several languages is genuinely multilingual.
            return languages[0] if len(languages) == 1 else "mul"

    # No work linked. Fall back to release text-representation, preferring
    # official pressings, and accept that it is a weaker signal.
    releases = sorted(detail.get("releases") or [],
                      key=lambda r: 0 if r.get("status") == "Official" else 1)
    for release in releases:
        language = (release.get("text-representation") or {}).get("language")
        if language:
            return language
    return None


def _escape_lucene(value: str) -> str:
    """MusicBrainz search is Lucene; quotes and backslashes need escaping."""
    return re.sub(r'([\\"])', r"\\\1", value)


def gather(artist: str, title: str, *, verbose: bool = True) -> Metadata:
    """
    Query both sources, tolerating either one being down.

    Degrading matters: iTunes alone can usually still produce `pop`, and
    MusicBrainz alone can still produce `german`. Only a total blackout leaves
    us with nothing — and that is a transient error, not a verdict on the song.
    """
    itunes_genre = language = None
    mb_tags: tuple[str, ...] = ()

    try:
        itunes_genre = lookup_itunes(artist, title)
    except Exception as exc:
        if verbose:
            print(f"[tag] iTunes lookup failed ({exc})")

    try:
        language, mb_tags = lookup_musicbrainz(artist, title)
    except Exception as exc:
        if verbose:
            print(f"[tag] MusicBrainz lookup failed ({exc})")

    return Metadata(itunes_genre=itunes_genre, language=language, mb_tags=mb_tags)


class LookupUnavailable(NoTagMatch):
    """
    Both metadata sources were unreachable.

    A subclass of NoTagMatch so callers that only care about "can't tag this"
    need no extra branch — but distinguishable, because this one means *try
    again later*, not *this song is unsuitable*. The difference decides whether
    the song is written off in the ledger.
    """


def classify_song(artist: str, title: str, *, verbose: bool = True) -> Classification:
    """Full path: gather metadata, then apply the rules. Raises NoTagMatch."""
    meta = gather(artist, title, verbose=verbose)
    if meta.empty:
        raise LookupUnavailable(
            "no metadata available from iTunes or MusicBrainz")

    result = classify(meta)
    if verbose:
        print(f"[tag] {result.hashtag}  "
              f"(itunes={meta.itunes_genre!r} lang={meta.language!r} "
              f"mb={list(meta.mb_tags)[:4]})")
    return result
