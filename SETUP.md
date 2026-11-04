# Setup

From nothing to a box publishing a nightcore video every hour. Roughly 30
minutes, most of it waiting for the art library to download.

There is one step that will silently break the whole thing a week after you
set it up if you get it wrong — [step 4.4](#44-publish-the-app-do-not-skip-this).
Everything else is recoverable.

---

## 1. Install the tools

### On the Linux VM (Debian/Ubuntu)

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv ffmpeg fonts-dejavu
```

`yt-dlp` from apt is usually months out of date, which breaks downloads.
Install it from upstream and keep it current:

```bash
sudo curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp
sudo chmod a+rx /usr/local/bin/yt-dlp
```

### On Windows

```bash
winget install Gyan.FFmpeg
winget install yt-dlp.yt-dlp
```

### Check

```bash
ffmpeg -version && ffprobe -version && yt-dlp --version && python3 -V
```

All four must answer. Python 3.10 or newer.

---

## 2. Install the project

```bash
cd /opt/nightcore          # or wherever you put it
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Fonts

Resolved automatically: DejaVu if present (the `fonts-dejavu` package above),
Segoe UI otherwise. Nothing to configure on either platform. To use something
else:

```bash
export NIGHTCORE_FONT_BOLD=/path/to/Bold.ttf
export NIGHTCORE_FONT_REG=/path/to/Regular.ttf
```

If neither default exists the render fails at the loop pass with a drawtext
error — check with `fc-list | grep DejaVu`.

### Contact address

Defaults to `https://gamer3514.co.uk`; set your own. MusicBrainz throttles callers who don't
identify themselves, and when it throttles, tag resolution silently degrades to
iTunes-only — you lose every EDM subgenre and the whole non-English language
rule. Override per-machine if you want:

```bash
export NIGHTCORE_CONTACT="you@example.com"
```

---

## 3. Stock the art library

```bash
python nightcore.py --build-pool 150 --pool-jobs 12
```

Downloads safe-rated anime art from safebooru into `assets/anime/`, normalises
it, and keeps only what survives our frame. About 80 seconds for 150 images at
12 concurrent jobs; drop `--pool-jobs` to be gentler on the box (default 6).

The gate is strict and most candidates fail it, which is expected — a typical
build turns away four or five for every one it keeps. The reasons are the
frame's, not taste:

- **aspect 1.42–2.22** — anything taller renders as a strip of art floating in
  blurred filler; anything wider gets blurred bars top and bottom;
- **big enough not to be upscaled** at the tightest point of the zoom;
- **bottom band not too bright** — the visualiser is screen-blended and
  vanishes over a bright backdrop;
- **top-right not too bright** — the watermark is the only text with no scrim
  behind it;
- **no banned tags** — other people's `signature`, `watermark`,
  `twitter_username` or baked-in text, sitting next to yours.

This is a **library you curate**: delete any image you dislike and it never
comes back. Re-run `--build-pool` any time to top it up.

To re-check an existing library against the current rules:

```bash
python nightcore.py --audit-pool     # report only
python nightcore.py --prune-pool     # ...and delete the failures
```

---

## 4. Set up YouTube

This is the fiddly part. Do it on a machine **with a browser** — your Windows
box — even if the daemon will run on the headless VM.

### 4.1 Create a project

1. Go to <https://console.cloud.google.com/>
2. Project dropdown (top left) → **New Project**
3. Name it something like `nightcore-uploader` → **Create**
4. Make sure it's selected in the dropdown before continuing

### 4.2 Enable the API

1. **APIs & Services** → **Library**
2. Search for **YouTube Data API v3**
3. **Enable**

### 4.3 Configure the consent screen

1. **APIs & Services** → **OAuth consent screen**
2. User type: **External** → **Create**
   (**Internal** is only offered if you have Google Workspace. If you do, use
   it — it skips step 4.4 entirely and is strictly better.)
3. App name: anything. User support email and developer contact email: your
   own address. → **Save and Continue**
4. **Scopes** → **Add or Remove Scopes** → filter for `youtube.upload`, tick
   `https://www.googleapis.com/auth/youtube.upload` → **Update** → **Save and
   Continue**
5. **Test users** → add the Google account that owns the YouTube channel →
   **Save and Continue**

### 4.4 Publish the app (DO NOT SKIP THIS)

Still on **OAuth consent screen**, find **Publishing status**. It will say
**Testing**. Click **PUBLISH APP** and confirm.

**Why this matters more than anything else on this page:** while an External
app is in *Testing*, Google **expires its refresh tokens after 7 days**. Your
daemon would run beautifully for a week and then start failing every upload
with `invalid_grant: Token has been expired or revoked`, and you would have to
redo the browser flow by hand. In *In production*, refresh tokens don't expire
unless revoked or unused for six months.
([DEV Community](https://dev.to/ko-hi/googles-oauth-testing-mode-expires-refresh-tokens-in-7-days-publish-the-consent-screen-before-24hm),
[Unipile](https://www.unipile.com/google-oauth-refresh-token/))

You do **not** need Google to verify the app. Unverified is fine for your own
channel — it just means you'll see a scary warning screen once in step 4.6.

### 4.5 Create the credentials

1. **APIs & Services** → **Credentials** → **Create Credentials** → **OAuth
   client ID**
2. Application type: **Desktop app**. Name it anything. → **Create**
3. **Download JSON**
4. Save it as `state/client_secret.json` in the project directory

```
nightcore/
└── state/
    └── client_secret.json
```

### 4.6 Sign in

```bash
python nightcore.py --youtube-auth
```

A browser opens. Sign in with the account that owns the channel.

You'll hit **"Google hasn't verified this app"**. That's expected — it's your
own unverified app. Click **Advanced** → **Go to <app name> (unsafe)** →
**Continue** → **Allow**.

This writes `state/yt_token.json`. Guard it: anyone with that file can upload
to your channel.

### 4.7 Move the token to the VM

```bash
scp state/yt_token.json user@vm:/opt/nightcore/state/
```

That's the only file the VM needs. `yt_token.json` embeds the client id and
secret alongside the refresh token, so the VM can renew access on its own
forever and never runs the consent flow. `client_secret.json` only matters on
the machine where you run `--youtube-auth`.

### 4.8 Understand the quota ceiling

Each upload costs **1600 quota units**. A fresh project gets **10,000 a day**.

**That is 6 uploads a day, not 24.**

The loop knows. When the day's budget can't fund another upload it sleeps until
midnight Pacific — Google's reset boundary — rather than rendering a video it
can't publish and burning a song for nothing.

If you want more, request an increase via the YouTube API Services audit form.
Once granted:

```bash
export YT_DAILY_BUDGET=50000
```

Running count lives in `state/quota.json`.

---

## 4b. Song sources (optional but recommended)

Three of the four sources need no setup at all and are already on:

| source | key | reach |
|---|---|---|
| Deezer genre charts | none | ~2300 tracks across 23 genre charts |
| Apple Music RSS | none | 100/day per storefront, 10 storefronts |
| Soundcharts | shared sandbox | 100, frequently 401s — expendable |
| Spotify playlist | **optional** | 100 without credentials, **all of it** with |

### Unlock the full Spotify playlist

Without credentials the playlist is read from its public `/embed/` page,
which **hard-caps at 100 tracks regardless of playlist size**. A 1000-track
playlist gives you 100, silently. The tool warns when it hits the cap.

1. Go to <https://developer.spotify.com/dashboard> and sign in.
2. **Create app**. Any name and description; App Status/redirect URI don't
   matter — this is a machine-to-machine (client credentials) flow, so there
   is no browser step and no user consent, on the VM or anywhere else.
3. Open the app → **Settings** → copy the **Client ID**, then **View client
   secret**.
4. Put them in the environment:

```bash
export SPOTIFY_CLIENT_ID=...
export SPOTIFY_CLIENT_SECRET=...
```

For the systemd unit, add them as `Environment=` lines (or better, an
`EnvironmentFile=` with mode 600 — the secret shouldn't be world-readable in
`/etc/systemd/system`).

### Why not a real Soundcharts key?

The credentials in the code are the ones printed in Soundcharts' own docs, so
everyone uses them and the endpoint rate-limits them to 401 at random. That
error is cosmetic — the other sources cover it. A genuine key is an enterprise
sales contract rather than a signup, which is not worth it when Deezer gives
more tracks, more genres, and asks for nothing.

## 5. First run

Render one, don't publish it, and go watch it:

```bash
python nightcore.py --no-upload
```

Expect 3–6 minutes on the Xeon. You should see the tag chosen before anything
downloads:

```
[tag] #playlist_rhythmandblues  (itunes='R&B/Soul' lang=None mb=[...])
```

Then render one **unlisted** to prove the upload path end to end:

```bash
python nightcore.py --privacy unlisted
```

Check the video on YouTube: title, description, and exactly one `#playlist_`
hashtag. Once you're happy, drop `--privacy` and it publishes public.

---

## 6. Run it continuously

```bash
python nightcore.py --loop
```

One cycle an hour, measured from each cycle's start. Ctrl-C stops it cleanly —
the in-flight `ffmpeg`/`yt-dlp` is killed, the partial mp4 deleted, nothing
committed to the ledger. Press it twice to exit immediately.

### As a systemd service

```bash
sudo tee /etc/systemd/system/nightcore.service <<'EOF'
[Unit]
Description=Nightcore video factory
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=nightcore
WorkingDirectory=/opt/nightcore
Environment=NIGHTCORE_CONTACT=you@example.com
ExecStart=/opt/nightcore/.venv/bin/python /opt/nightcore/nightcore.py --loop
Restart=on-failure
RestartSec=300

# SIGTERM runs the same graceful path as Ctrl-C; give ffmpeg time to die.
KillSignal=SIGTERM
TimeoutStopSec=120

# It is a batch job on a shared box. Let interactive work have the CPU.
Nice=10
IOSchedulingClass=idle

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now nightcore
journalctl -u nightcore -f
```

`Nice=10` and `IOSchedulingClass=idle` matter on a 4-vCPU VM with spinning
disks — without them a render makes the box unpleasant to SSH into.

### On Windows

Task Scheduler → Create Task → Trigger: *At startup* → Action: start
`nightcore.py --loop`. The single-instance lock means a double-trigger is
harmless: the second process exits immediately.

---

## 7. Keeping it healthy

```bash
python nightcore.py --list-used     # what's been made, with YouTube links
```

**Update yt-dlp monthly.** It is the single most common cause of a run of
failures — YouTube changes something and yt-dlp needs a new release to cope.

```bash
sudo yt-dlp -U
```

**Watch the art library.** `--list-used` shows how many images are left this
cycle. It recycles rather than failing when exhausted, but more variety is
better:

```bash
python nightcore.py --build-pool 200
```

**Disk.** Each finished video is 60–260 MB and nothing deletes them. Prune
`out/` periodically once uploads are confirmed.

---

## 8. When something breaks

| symptom | cause | fix |
|---|---|---|
| `invalid_grant: Token has been expired or revoked` after ~a week | consent screen is still in **Testing** | do [4.4](#44-publish-the-app-do-not-skip-this), then redo [4.6](#46-sign-in) |
| `no usable YouTube credentials` | `yt_token.json` missing on the VM | copy it from the machine you ran `--youtube-auth` on |
| `quotaExceeded` | the 6-a-day ceiling | nothing to fix; the loop sleeps to the Pacific reset. Rendered videos stay in `out/` — publish by hand with `--upload` |
| `another instance is already running (pid N)` | a second copy started | intended. `cat state/nightcore.lock.pid` to see who holds it |
| every song rejected with `no playlist tag` | MusicBrainz throttling you | check `NIGHTCORE_CONTACT` is set to a real address |
| `soundcharts:global-28 unavailable (401)` | the built-in sandbox credentials | harmless — it falls back to the Spotify playlist automatically |
| render dies at the loop pass with a drawtext error | font path wrong on Linux | see [Fonts on Linux](#fonts-on-linux) |
| renders are far slower than expected | wrong preset for the hardware | `python nightcore.py --benchmark` and compare presets on the real box |

Publish something that's already rendered:

```bash
python nightcore.py --upload out/20260919-185236-ravyn-lenae-love-me-not-nightcore.mp4 \
                    --playlist-tag rhythmandblues
```

---

## Everything that lives on disk

| path | what | safe to delete? |
|---|---|---|
| `state/client_secret.json` | OAuth client — **secret** | no |
| `state/yt_token.json` | your sign-in — **secret**, grants upload to your channel | no |
| `state/ledger.json` | what's been used, skipped, and published | no — it's the no-duplicates memory |
| `state/quota.json` | today's API spend | yes (it'll over-upload once and self-correct) |
| `state/art_pool.json` | art library manifest | yes, regenerated by `--build-pool` |
| `state/nightcore.lock` `.pid` | the single-instance lock | yes, when nothing's running |
| `assets/anime/` | the art library | yes, but then re-run `--build-pool` |
| `work/` | scratch | yes, any time nothing is running |
| `out/` | finished videos | yes, once uploaded |

Never commit `state/client_secret.json` or `state/yt_token.json` anywhere.
