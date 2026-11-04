# nightcore-factory

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Powered by FFmpeg](https://img.shields.io/badge/powered%20by-FFmpeg-007808)

An unattended nightcore video factory: picks a charting song nobody's used,
finds a clean master, speeds it up 20% (pitch and tempo together), renders a
1080p60 video over vetted anime art with a constant-Q visualiser, and publishes
it to YouTube with a Nightcore Network playlist tag.

Built around the rules and guidelines of the [Nightcore Network](https://www.nightcore.com/):
each song gets a network playlist tag and each description carries the
network blurb. Not a member, or want something different? Set
`NIGHTCORE_NETWORK=0` to turn all of that off —
see [Playlist tags](#playlist-tags).

Made by **Gamer3514** — [gamer3514.co.uk](https://gamer3514.co.uk).

**See it running live:** the script publishes to
[@sillynightcore](https://www.youtube.com/@sillynightcore) on YouTube — every
video there was made and uploaded by this code.

## Features

- **Song sourcing** from Apple, Spotify and Soundcharts charts, pooled and
  de-duplicated, with a ledger so no song is ever made twice.
- **Clean masters** found via `yt-dlp`, ranked to avoid video rips and karaoke.
- **Playlist tagging** from iTunes + MusicBrainz metadata; songs that match no
  tag are never rendered.
- **Three-pass 1080p60 render** (blurred plate → baked loop → visualiser) that
  is ~2.7x faster than a naive single pass.
- **Vetted art library** with a quality gate so pictures survive the frame.
- **Unattended loop** with a single-instance lock, graceful shutdown, failure
  back-off and YouTube quota accounting.

## Quick start

```bash
git clone https://github.com/TheGamer3514/nightcore-factory.git
cd nightcore-factory
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Then:

```bash
python nightcore.py --build-pool          # once: stock the art library
python nightcore.py --no-upload           # try it: one video, rendered only
python nightcore.py --youtube-auth        # once: sign in (needs a browser)
python nightcore.py --loop                # forever: one video an hour
```

Rendering alone needs no credentials. Publishing needs your own Google Cloud
OAuth client — see [Setting up](#setting-up).

## Requirements

- Python 3.10 or newer
- `ffmpeg`, `ffprobe` and `yt-dlp` on `PATH`
- `pip install -r requirements.txt` (Pillow, numpy and the Google API client;
  only the Google packages are needed for uploading)

## Setting up

**→ [SETUP.md](SETUP.md)** walks through it end to end: tools, the art
library, the Google Cloud console, running it as a service, and what to do
when something breaks. The short version:

1. In a Google Cloud project, enable the **YouTube Data API v3**.
2. Configure the OAuth consent screen and **publish it** — leaving it in
   *Testing* makes Google expire your refresh token after 7 days.
3. Create an OAuth client of type **Desktop app**, download the JSON, and save
   it as `state/client_secret.json`.
4. Run `python nightcore.py --youtube-auth`. A browser opens; approve, and
   `state/yt_token.json` is written.
5. **On a headless box**, do step 4 on a machine that has a browser and copy
   `state/yt_token.json` across — it carries everything needed to refresh.

### The quota ceiling

`videos.insert` costs **1600 quota units** and a default project gets **10,000
a day** — so **6 uploads a day**, not 24. The loop checks the budget before it
renders: when the day is spent, it sleeps until midnight Pacific (Google's
reset) rather than burning CPU and a song on a video it cannot publish.

If you get an audited quota increase, set `YT_DAILY_BUDGET` and the loop will
use it. `state/quota.json` holds the running count.

## Playlist tags

These rules come from the Nightcore Network. To opt out entirely, set
`NIGHTCORE_NETWORK=0`: nothing below applies, uploads get a plain description
with the generic hashtags, and no iTunes/MusicBrainz lookups are made.

Every upload carries exactly one `#playlist_` hashtag from the
[member guidance](https://wiki.nightcore.com/member-guidance/featuring-videos.md),
resolved from iTunes and MusicBrainz metadata before anything is downloaded:

- a non-English song uses its **language** playlist (`#playlist_german`, …)
- **anime, folk and K-pop** are exempt from that and use their genre
- anything else uses its **genre**
- **a song that matches no tag is never rendered.** It is written to
  `state/ledger.json` under `skipped` with a reason and never reconsidered.

Language comes from the MusicBrainz *work*, which is where lyrics language
lives. A release's `text-representation.language` describes that release's
track listing, so reading it would call "Alors on danse" English on any English
compilation.

## Running continuously

```bash
python nightcore.py --loop --interval 3600
```

- The interval is measured from each cycle's **start**, so a long render does
  not push the schedule later and later.
- Only one instance can run: `state/nightcore.lock` is an OS file lock, so it
  is released by the kernel even after a hard reset. A second process exits
  immediately rather than racing the ledger.
- **Ctrl-C** cancels the in-flight cycle — the `ffmpeg`/`yt-dlp` child is
  terminated, the partial mp4 is deleted, nothing is committed — and exits 0.
  Press it again to exit on the spot.
- A failed cycle is logged and the loop continues. Five failures in a row backs
  the interval off to 4 hours; one success restores it.
- An exhausted chart is not a failure — charts turn over daily. Use
  `--exit-when-empty` if you'd rather it stopped.

Nothing reaches the ledger until the video has rendered, verified and uploaded,
so every abort path leaves the song eligible for the next cycle.

### Running it as a service (Linux)

```ini
# /etc/systemd/system/nightcore.service
[Service]
ExecStart=/usr/bin/python3 /opt/nightcore/nightcore.py --loop
WorkingDirectory=/opt/nightcore
Restart=on-failure
RestartSec=300
KillSignal=SIGTERM
TimeoutStopSec=120
```

`SIGTERM` runs the same graceful path as Ctrl-C; the timeout gives an in-flight
ffmpeg time to die.

## Performance

Tuned for 4 virtual cores of a Xeon E5-2670 v2 (Ivy Bridge, no AVX2). The
render is three ffmpeg passes, each doing expensive work over the smallest
number of frames that can reproduce it:

1. **plate** — the backdrop blur, once, because the artwork never changes.
2. **loop** — the zoom, scrim, title, subtitle and watermark baked into a
   24-second clip, because the zoom is a full sine cycle over exactly that
   period and everything else is static. 1440 frames instead of ~12,600.
3. **final** — `-stream_loop` over that clip plus the visualiser, which is the
   only thing that genuinely varies frame to frame.

Measured on a developer workstation at identical encoder settings, a
3½-minute song went from ~9 minutes (original single pass, `medium`/CRF 18) to
~3.3 minutes — about 2.7x. Check it on your own hardware:

```bash
python nightcore.py --benchmark
```

Once the zoom work is gone, `showcqt` dominates, which is why `ultrafast` buys
only ~4% over `veryfast`. Override the thread count with `NIGHTCORE_THREADS`.

## Other commands

```bash
python nightcore.py                       # one video, rendered and published
python nightcore.py --no-upload           # one video, rendered only
python nightcore.py --privacy unlisted    # publish unlisted instead of public
python nightcore.py --list-used           # the ledger, with YouTube links
python nightcore.py --audit-pool          # re-check the art library
python nightcore.py --prune-pool          # ...and delete whatever fails
python nightcore.py --build-pool 150 --pool-jobs 12   # restock, 12 at a time
python nightcore.py --upload out/x.mp4 --playlist-tag pop   # publish by hand
```

## Tests

```bash
python -m pytest            # fast: no network, no ffmpeg
python -m pytest -m slow    # also renders real video (needs ffmpeg)
```

## Layout

| path | what |
|---|---|
| `nightcore.py` | config, CLI, the render pipeline |
| `tagging.py` | iTunes + MusicBrainz → one playlist tag, or a rejection |
| `youtube.py` | OAuth, resumable upload, metadata, quota accounting |
| `sources.py` | the four song sources, pooled and de-duplicated |
| `artcheck.py` | whether a picture survives the frame |
| `scheduler.py` | the loop |
| `control.py` | cancellation and the single-instance lock |
| `envfile.py` | loads `.env` into the environment before anything reads it |
| `state/` | ledger, art manifest, quota counter, OAuth token |
| `assets/anime/` | the vetted art library |
| `out/` | finished videos |

## Environment variables

Copy [`.env.example`](.env.example) to `.env` (git-ignored) and edit it, or
set the variables in your shell — a real environment variable always beats
`.env`.

| variable | what |
|---|---|
| `NIGHTCORE_CONTACT` | an email or project URL. MusicBrainz throttles requests that don't identify a contact, so without this the tag lookup quietly degrades to iTunes-only and loses every EDM subgenre and every language check. |
| `NIGHTCORE_THREADS` | encode threads (default 4) |
| `NIGHTCORE_POOL_JOBS` | images downloaded and processed at once by `--build-pool` (default 6) |
| `NIGHTCORE_FONT_BOLD` / `NIGHTCORE_FONT_REG` | override the on-screen fonts |
| `NIGHTCORE_NETWORK` | `1` (default) follows the [Nightcore Network](https://www.nightcore.com/) rules. `0`/`false`/`off` disables them: no tag lookup, no `#playlist_` hashtag, no network blurb or nightcore.com link in the description, no seasonal playlists, and no song skipped for lacking a tag. |
| `NIGHTCORE_WATERMARK` | the handle burned into a corner of every frame (default `@sillynightcore`). **Set this to your own**, or to an empty string for none. |
| `YT_DAILY_BUDGET` | daily quota units, if you've been granted an increase (default 10000) |
| `YT_UPLOAD_COST` | units per upload (default 1600) |
| `SPOTIFY_PLAYLIST_ID` | which playlist to pull from |
| `SPOTIFY_CHRISTMAS_PLAYLIST_ID` / `SPOTIFY_HALLOWEEN_PLAYLIST_ID` | playlists used instead of `SPOTIFY_PLAYLIST_ID` from 1 Nov – 25 Dec and 24 – 31 Oct respectively (default: the @sillynightcore seasonal playlists) |
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | free credentials from developer.spotify.com. Without them the playlist is read from its embed page, which silently caps at 100 tracks however big the playlist is. |
| `APPLE_STOREFRONTS` | comma-separated country codes for the Apple chart (default `us,gb,de,fr,es,br,jp,it,pl,mx`) |
| `SOUNDCHARTS_APP_ID` / `SOUNDCHARTS_API_KEY` | a real Soundcharts account, if you have one; the built-in sandbox credentials return 401 |

## Disclaimer

This tool automates downloading third-party music and artwork and posting to
YouTube. You are responsible for what you publish with it:

- Respect the copyright of the songs you process and the
  [YouTube Terms of Service](https://www.youtube.com/t/terms) and API Services
  terms. Uploading other people's music may get videos blocked or a channel
  struck.
- The art library is downloaded from safebooru for **your own** use. It is not
  included in this repository, and the original artists retain their rights.
- Credentials (`state/client_secret.json`, `state/yt_token.json`) are yours;
  they are git-ignored. Never commit them.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Author

Built by **Gamer3514** — see more of my work at **[gamer3514.co.uk](https://gamer3514.co.uk)**.

## License

[MIT](LICENSE) © 2026 Gamer3514
