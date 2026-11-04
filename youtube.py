"""
Publish a finished video to YouTube.

Three separable pieces:

  * `QuotaLedger` — local accounting against the Data API's daily budget,
    because the interesting limit is not "can I upload" but "can I upload
    *today*", and the answer decides whether the scheduler bothers rendering.
  * metadata construction — pure functions, no network, so the exact bytes of
    the title and description are testable.
  * `Uploader` — OAuth plus a resumable `videos.insert`.

Credentials: a Desktop-type OAuth client with the YouTube Data API v3 enabled,
saved as state/client_secret.json. `--youtube-auth` turns that into
state/yt_token.json, which is what the daemon actually uses.
"""

from __future__ import annotations

import json
import os
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from pathlib import Path

import control
from errors import QuotaExhausted, UploadFailed

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

# videos.insert is 1600 units; a project that has not been through the audit
# gets 10,000 units a day. Six uploads. Both are overridable so that a granted
# quota increase is a config change, not a code change.
UPLOAD_COST = int(os.environ.get("YT_UPLOAD_COST") or 1600)
DAILY_BUDGET = int(os.environ.get("YT_DAILY_BUDGET") or 10000)

# Google's quota day resets at midnight Pacific, not midnight UTC and not
# midnight local.
QUOTA_TZ = "America/Los_Angeles"

CATEGORY_MUSIC = "10"
NETWORK_URL = "https://www.nightcore.com/"

TITLE_SUFFIX = " 【Nightcore】"
TITLE_MAX = 100
TAGS_MAX_CHARS = 450          # YouTube's cap is 500; leave room for separators
CHUNK_SIZE = 4 * 1024 * 1024  # resumable chunk — small enough to abort quickly

RETRY_STATUSES = {500, 502, 503, 504}
RETRY_REASONS = {"backendError", "internalError", "rateLimitExceeded"}
MAX_ATTEMPTS = 5


def _pacific_now() -> datetime:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(QUOTA_TZ))


# ─────────────────────────────────────────────────────────────────────────────
# Quota accounting
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class QuotaLedger:
    """
    What we believe we have spent against today's API budget.

    Local accounting can drift — a failed upload that still consumed quota, a
    second machine on the same project — so it is a *prediction*, and the API
    is the authority. `mark_exhausted` exists for the moment reality disagrees.
    """

    path: Path
    day: str = ""
    units_spent: int = 0
    budget: int = DAILY_BUDGET
    cost: int = UPLOAD_COST

    @classmethod
    def load(cls, path: Path) -> "QuotaLedger":
        ledger = cls(path=path)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                ledger.day = data.get("pacific_day", "")
                ledger.units_spent = int(data.get("units_spent", 0))
            except (ValueError, OSError):
                # A corrupt counter should cost us one day of caution, not the
                # whole daemon.
                pass
        ledger._roll_over()
        return ledger

    def _roll_over(self) -> None:
        today = _pacific_now().date().isoformat()
        if self.day != today:
            self.day, self.units_spent = today, 0

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"pacific_day": self.day,
                        "units_spent": self.units_spent}, indent=2),
            encoding="utf-8")

    @property
    def remaining(self) -> int:
        self._roll_over()
        return max(0, self.budget - self.units_spent)

    def can_upload(self) -> bool:
        return self.remaining >= self.cost

    def charge(self, units: int | None = None) -> None:
        self._roll_over()
        self.units_spent += self.cost if units is None else units
        self.save()

    def mark_exhausted(self) -> None:
        """The API said quotaExceeded. It is right and we are wrong."""
        self._roll_over()
        self.units_spent = self.budget
        self.save()

    def seconds_until_reset(self) -> float:
        now = _pacific_now()
        midnight = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return max(60.0, (midnight - now).total_seconds())


# ─────────────────────────────────────────────────────────────────────────────
# Metadata
# ─────────────────────────────────────────────────────────────────────────────

def build_title(artist: str, title: str) -> str:
    """
    "Artist - Title 【Nightcore】", inside YouTube's 100-character limit.

    The suffix is what makes the video findable as nightcore, so it survives
    truncation and the song name gives ground instead.
    """
    head = f"{artist} - {title}"
    room = TITLE_MAX - len(TITLE_SUFFIX)
    if len(head) > room:
        head = head[:room].rstrip()
        # Prefer to lose a whole word rather than leave a severed one, but not
        # if that would cost more than a quarter of the space.
        cut = head.rfind(" ")
        if cut > room * 0.75:
            head = head[:cut]
        head = head.rstrip(" -–—") + "…"
    return head + TITLE_SUFFIX


def _hashtag(value: str) -> str | None:
    """A YouTube-legal hashtag from free text, or None if nothing survives."""
    flat = re.sub(r"[^0-9a-zA-Z]+", "", value)
    return f"#{flat.lower()}" if len(flat) >= 3 else None


def build_hashtags(playlist_tag: str | None, artist: str, title: str) -> str:
    """
    One line of hashtags, the network's playlist tag first.

    Position matters twice over: YouTube surfaces the first few above the
    video title, and the network's automation is reading for the playlist tag.
    With no playlist tag (network mode off) the line just has the generic ones.
    """
    tags = ["#nightcore", "#nightcoremusic", "#spedup"]
    if playlist_tag:
        tags.insert(0, f"#playlist_{playlist_tag}")
    for extra in (_hashtag(artist), _hashtag(title)):
        if extra and extra not in tags:
            tags.append(extra)
    return " ".join(tags)


def build_description(artist: str, title: str,
                      playlist_tag: str | None) -> str:
    """With playlist_tag=None: no network blurb, link or #playlist_ hashtag."""
    network = (
        f"🌐 Nightcore Network\n"
        f"We are a member of the Nightcore network, you can discover more "
        f"nightcore at {NETWORK_URL}\n"
        f"\n"
    ) if playlist_tag else ""
    description = (
        f"Song Name: {artist} - {title}\n"
        f"\n"
        f"{network}"
        f"{build_hashtags(playlist_tag, artist, title)}\n"
    )
    if not playlist_tag:
        return description
    # The guidance is explicit that their automation only handles one playlist
    # hashtag. Two would be published and silently mis-filed, so this is a
    # hard assertion rather than a log line.
    found = re.findall(r"#playlist_\w+", description)
    if len(found) != 1:
        raise UploadFailed(
            f"description must carry exactly one playlist hashtag, has {found}")
    return description


def build_tags(artist: str, title: str,
               playlist_tag: str | None) -> list[str]:
    """Keyword tags for the API, trimmed to fit YouTube's 500-char budget."""
    wanted = ["nightcore", artist, title, f"{artist} nightcore",
              "sped up", "nightcore remix"]
    if playlist_tag:
        wanted += [playlist_tag, "nightcore network"]
    tags, used = [], 0
    for tag in wanted:
        tag = tag.strip()
        # YouTube counts a quoted tag's quotes when a tag contains a space.
        cost = len(tag) + (3 if " " in tag else 1)
        if not tag or used + cost > TAGS_MAX_CHARS:
            continue
        tags.append(tag)
        used += cost
    return tags


@dataclass(frozen=True)
class VideoMetadata:
    title: str
    description: str
    tags: list[str]
    playlist_tag: str | None

    @classmethod
    def build(cls, artist: str, title: str,
              playlist_tag: str | None) -> "VideoMetadata":
        return cls(
            title=build_title(artist, title),
            description=build_description(artist, title, playlist_tag),
            tags=build_tags(artist, title, playlist_tag),
            playlist_tag=playlist_tag,
        )

    def to_body(self, privacy: str = "public") -> dict:
        return {
            "snippet": {
                "title": self.title,
                "description": self.description,
                "tags": self.tags,
                "categoryId": CATEGORY_MUSIC,
            },
            "status": {
                "privacyStatus": privacy,
                "selfDeclaredMadeForKids": False,
                "embeddable": True,
            },
        }


# ─────────────────────────────────────────────────────────────────────────────
# Uploading
# ─────────────────────────────────────────────────────────────────────────────

def _import_google():
    """
    Import the Google client libraries, with an actionable error if absent.

    Deferred rather than top-level so that `--build-pool`, a plain one-shot
    render, and the whole test suite all work on a machine that has never
    installed them.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise UploadFailed(
            "YouTube upload needs the Google client libraries:\n"
            "    pip install -r requirements.txt"
        ) from exc
    return SimpleNamespace(
        Request=Request, Credentials=Credentials,
        InstalledAppFlow=InstalledAppFlow, build=build,
        HttpError=HttpError, MediaFileUpload=MediaFileUpload)


class Uploader:
    """A signed-in YouTube client. Construct once, upload many."""

    def __init__(self, client_secret: Path, token_path: Path,
                 privacy: str = "public"):
        self.client_secret = client_secret
        self.token_path = token_path
        self.privacy = privacy
        self._service = None

    # ── Auth ────────────────────────────────────────────────────────────────
    def authorise(self, interactive: bool = False):
        """
        Return credentials, refreshing or minting them as needed.

        `interactive` opens a browser. The VM this runs on has none, which is
        why the flow is a separate `--youtube-auth` command: run it once
        somewhere with a browser and copy the resulting token file across. The
        refresh token in it renews access indefinitely without a browser.
        """
        g = _import_google()

        creds = None
        if self.token_path.exists():
            try:
                creds = g.Credentials.from_authorized_user_file(
                    str(self.token_path), SCOPES)
            except ValueError as exc:
                raise UploadFailed(
                    f"{self.token_path} is not a valid token file ({exc}); "
                    f"delete it and re-run --youtube-auth") from exc

        if creds and creds.valid:
            return creds

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(g.Request())
            self._write_token(creds)
            return creds

        if not interactive:
            raise UploadFailed(
                f"no usable YouTube credentials at {self.token_path}. "
                f"Run  python nightcore.py --youtube-auth  on a machine with a "
                f"browser, then copy the token file here.")

        if not self.client_secret.exists():
            raise UploadFailed(
                f"missing {self.client_secret}. Create an OAuth client of type "
                f"'Desktop app' in a Google Cloud project with the YouTube "
                f"Data API v3 enabled, download the JSON, and save it there.")

        flow = g.InstalledAppFlow.from_client_secrets_file(
            str(self.client_secret), SCOPES)
        creds = flow.run_local_server(port=0)
        self._write_token(creds)
        return creds

    def _write_token(self, creds) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(creds.to_json(), encoding="utf-8")
        try:
            os.chmod(self.token_path, 0o600)
        except OSError:
            pass   # Windows, or a filesystem without POSIX modes.

    def service(self):
        if self._service is None:
            g = _import_google()
            self._service = g.build("youtube", "v3",
                                    credentials=self.authorise(),
                                    cache_discovery=False)
        return self._service

    # ── Upload ──────────────────────────────────────────────────────────────
    def upload(self, video: Path, metadata: VideoMetadata,
               quota: QuotaLedger | None = None) -> str:
        """
        Upload `video`, returning its YouTube id.

        Resumable and chunked: these files run to a few hundred megabytes, and
        a single-shot POST that dies at 95% has to start over.
        """
        g = _import_google()

        if quota is not None and not quota.can_upload():
            raise QuotaExhausted(
                f"only {quota.remaining} quota units left today, "
                f"an upload costs {quota.cost}")

        print(f"[yt] uploading {video.name} ({video.stat().st_size/1e6:.0f} MB)")
        print(f"[yt] title: {metadata.title}")

        media = g.MediaFileUpload(str(video), chunksize=CHUNK_SIZE,
                                resumable=True, mimetype="video/mp4")
        request = self.service().videos().insert(
            part="snippet,status",
            body=metadata.to_body(self.privacy),
            media_body=media,
        )

        # Quota is charged on the attempt, not the outcome: Google bills the
        # insert whether or not we like the answer, so counting it up front is
        # the honest prediction.
        if quota is not None:
            quota.charge()

        response = None
        attempt = 0
        last_progress = -10
        while response is None:
            control.check_cancelled()
            try:
                status, response = request.next_chunk()
            except g.HttpError as exc:
                attempt += 1
                self._handle_http_error(exc, attempt, quota)
                continue
            except (TimeoutError, ConnectionError, OSError) as exc:
                attempt += 1
                if attempt >= MAX_ATTEMPTS:
                    raise UploadFailed(f"upload failed after {attempt} "
                                       f"attempts: {exc}") from exc
                _backoff(attempt)
                continue

            attempt = 0
            if status:
                pct = int(status.progress() * 100)
                if pct >= last_progress + 10:
                    print(f"[yt]   {pct}%")
                    last_progress = pct

        video_id = response.get("id")
        if not video_id:
            raise UploadFailed(f"upload returned no video id: {response}")
        print(f"[yt] published https://youtu.be/{video_id}")
        return video_id

    def _handle_http_error(self, exc, attempt: int,
                           quota: QuotaLedger | None) -> None:
        status = getattr(getattr(exc, "resp", None), "status", None)
        reason = _error_reason(exc)

        if reason == "quotaExceeded" or status == 403 and reason in {
                "quotaExceeded", "dailyLimitExceeded"}:
            if quota is not None:
                quota.mark_exhausted()
            raise QuotaExhausted("YouTube daily upload quota exhausted") from exc

        if status in RETRY_STATUSES or reason in RETRY_REASONS:
            if attempt >= MAX_ATTEMPTS:
                raise UploadFailed(
                    f"upload failed after {attempt} attempts: {exc}") from exc
            print(f"[yt] transient error ({status or reason}), "
                  f"retry {attempt}/{MAX_ATTEMPTS}")
            _backoff(attempt)
            return

        raise UploadFailed(f"upload rejected ({status} {reason}): {exc}") from exc


def _error_reason(exc) -> str:
    """Dig the machine-readable reason out of a googleapiclient HttpError."""
    try:
        payload = json.loads(getattr(exc, "content", b"") or b"{}")
        return (payload.get("error", {}).get("errors", [{}])[0]
                .get("reason", "")) or payload.get("error", {}).get("status", "")
    except (ValueError, AttributeError, IndexError):
        return ""


def _backoff(attempt: int) -> None:
    """Exponential backoff with jitter, interruptible by Ctrl-C."""
    delay = min(60.0, 2.0 ** attempt) + random.uniform(0, 1.5)
    control.sleep(delay)
    control.check_cancelled()
