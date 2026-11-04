"""
Whether a piece of art survives contact with our frame.

This module owns the *frame geometry* — the sizes and zoom range that decide
how an image meets the video — and the checks that follow from it. It is the
one place that knows a 2000x3000 illustration can be beautiful on safebooru
and still be 45% grey mush in a 16:9 nightcore video.

Two entry points:

  * `metadata_verdict(width, height)` — cheap, from the booru's JSON, before a
    single byte is downloaded.
  * `pixel_verdict(path)` — the full measurement, on the ingested file.

Both return a list of reasons; empty means keep. Used at ingest time by
ArtPool and after the fact by `--audit-pool`, so the library can never drift
away from the standard the renderer needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Frame geometry. nightcore.py imports these rather than declaring its own, so
# there is exactly one definition of how big the frame is and how far it zooms.
# ─────────────────────────────────────────────────────────────────────────────

WIDTH, HEIGHT = 1920, 1080
COMP_W, COMP_H = 2400, 1350         # oversized plate the zoom pans over
ZOOM_BASE = COMP_W / WIDTH          # 1.25 → 1:1 pixel mapping at rest
ZOOM_AMPLITUDE = 0.045
ZOOM_PERIOD_S = 24
VIS_HEIGHT = 340                    # visualiser band at the bottom

ZOOM_MIN = ZOOM_BASE - ZOOM_AMPLITUDE   # widest view  → most of the plate seen
ZOOM_MAX = ZOOM_BASE + ZOOM_AMPLITUDE   # tightest view → most magnification

# Two different questions get answered at two different points of the zoom.
#
#   Framing  is judged at REST (zoom == ZOOM_BASE), where the visible plate
#            region is exactly WIDTH x HEIGHT. That is the typical view, and
#            it is the one the library was curated against.
#   Sharpness is judged at the TIGHTEST zoom, because that is the worst case:
#            the smallest piece of plate stretched across the whole output.
VIEW_W, VIEW_H = WIDTH, HEIGHT                  # plate region seen at rest

# Pixel thresholds, all measured on the reconstructed frame.
MAX_UPSCALE = 1.02        # >1 means we are inventing detail

# Long edge the ingest downscales to. A source below the resolution floor
# cannot be rescued, but one above it is capped to this.
#
# The floor is not a matter of taste. A 16:9 source is contained to the full
# 2400px plate width, and the tightest zoom magnifies that by ZOOM_MAX, so it
# needs COMP_W * ZOOM_MAX / MAX_UPSCALE ≈ 3047 source pixels to avoid
# stretching. Capping at 3000 — as this did originally — makes it arithmetically
# impossible for *any* image wider than 16:9 to pass the sharpness check, no
# matter how large the original was. 3200 leaves headroom.
MIN_VIABLE_EDGE = COMP_W * ZOOM_MAX / MAX_UPSCALE        # ≈ 3047
MAX_EDGE = 3200
assert MAX_EDGE >= MIN_VIABLE_EDGE, (
    f"MAX_EDGE {MAX_EDGE} < {MIN_VIABLE_EDGE:.0f}: the downscale would "
    f"guarantee a failing upscale factor for wide art")

# Retention bar: what an image already in the library must clear. 0.895 keeps
# anything showing ~90% real art, which is where the library was curated.
MIN_COVERAGE = 0.895

# Acquisition bar: what a *new* download must clear. Deliberately stricter —
# it costs nothing to be fussy before spending the bandwidth, and it would be
# perverse to keep churning art that has already been vetted. Full coverage
# means the blurred backdrop is never on screen at rest.
INGEST_MIN_COVERAGE = 1.0

# Art is contained in the plate, so aspect alone decides whether the blurred
# backdrop shows. Full coverage at rest needs the art to span VIEW_W x VIEW_H:
#     portrait-ish: COMP_H * a >= VIEW_W
#     landscape:    COMP_W / a >= VIEW_H
MIN_ASPECT = VIEW_W / COMP_H         # 1.422
MAX_ASPECT = COMP_W / VIEW_H         # 2.222

MAX_VIS_LUMA = 175        # visualiser bars are screen-blended; bright kills them
MAX_WM_P90 = 210          # watermark is the only text with no scrim behind it
MIN_SATURATION = 0.15
MIN_CONTRAST = 35
MIN_SHARPNESS = 400
MIN_LUMA = 45

# Tags that mean "this image already has someone else's text on it", is not a
# clean single illustration, or is deliberately low-fidelity.
BANNED_TAGS = frozenset({
    # Someone else's branding, fighting ours.
    "watermark", "signature", "artist_name", "twitter_username",
    "web_address", "logo", "dated", "copyright_name", "character_name",
    # Text baked into the art.
    "english_text", "japanese_text", "speech_bubble", "text_focus",
    # Framing that fights a full-bleed 16:9 crop.
    "border", "letterboxed",
    # Not a single clean illustration.
    "4koma", "comic", "sketch", "photo_(medium)", "monochrome", "greyscale",
    "wallpaper_forced",
    # Deliberately low-fidelity. Pixel art is often *stored* at 4000px+, so it
    # sails past any resolution check while being exactly the pixelation the
    # network's guidance rules out.
    "pixel_art", "dithering", "oekaki", "lowres", "jpeg_artifacts",
    # Tone.
    "guro", "blood", "corpse", "death", "gore", "meme", "spoilers",
})


@dataclass(frozen=True)
class Metrics:
    """Everything measured about one image, in frame terms."""

    width: int
    height: int
    aspect: float
    coverage: float      # fraction of the visible frame that is real art
    upscale: float       # source px per output px at the tightest zoom; >1 bad
    vis_luma: float
    wm_p90: float
    saturation: float
    contrast: float
    sharpness: float
    luma: float


# ─────────────────────────────────────────────────────────────────────────────
# Cheap checks — booru metadata only, before anything is downloaded
# ─────────────────────────────────────────────────────────────────────────────

def capped_size(width: int, height: int) -> tuple[int, int]:
    """Dimensions after ingest downscales the long edge to MAX_EDGE."""
    longest = max(width, height)
    if longest <= MAX_EDGE:
        return width, height
    s = MAX_EDGE / longest
    return max(1, round(width * s)), max(1, round(height * s))


def upscale_factor(width: int, height: int) -> float:
    """
    Source pixels per output pixel at the tightest zoom. Above 1.0 we are
    stretching, which on a near-static 1080p frame is plainly visible.
    """
    w, h = capped_size(width, height)
    plate_scale = min(COMP_W / w, COMP_H / h)          # contain
    src_per_out = (1 / ZOOM_MAX) / plate_scale
    return 1.0 / src_per_out


def coverage(width: int, height: int) -> float:
    """Fraction of the frame occupied by art rather than blurred backdrop."""
    a = width / height
    art_w, art_h = (COMP_W, COMP_W / a) if a >= COMP_W / COMP_H \
        else (COMP_H * a, COMP_H)
    return (min(art_w, VIEW_W) / VIEW_W) * (min(art_h, VIEW_H) / VIEW_H)


def metadata_verdict(width: int, height: int) -> list[str]:
    """
    Reasons to reject a *candidate download*, from its dimensions alone.

    Stricter than `pixel_verdict` by design — see INGEST_MIN_COVERAGE.
    """
    if not width or not height:
        return ["no dimensions"]

    reasons = []
    a = width / height
    if coverage(width, height) < INGEST_MIN_COVERAGE:
        if a < MIN_ASPECT:
            reasons.append(f"too tall (aspect {a:.2f} < {MIN_ASPECT:.2f}); "
                           f"would show blurred side bars")
        else:
            reasons.append(f"too wide (aspect {a:.2f} > {MAX_ASPECT:.2f}); "
                           f"would show blurred top/bottom bars")
    up = upscale_factor(width, height)
    if up > MAX_UPSCALE:
        reasons.append(f"too small ({width}x{height} → {up:.2f}x upscale "
                       f"at the zoom crop)")
    return reasons


def tag_verdict(tags: str) -> list[str]:
    hit = sorted(set(tags.split()) & BANNED_TAGS)
    return [f"banned tags: {', '.join(hit)}"] if hit else []


# ─────────────────────────────────────────────────────────────────────────────
# Pixel checks — the real frame, reconstructed
# ─────────────────────────────────────────────────────────────────────────────

def _numpy_and_pil():
    try:
        import numpy as np
        from PIL import Image, ImageFilter
    except ImportError as exc:                       # pragma: no cover
        raise RuntimeError(
            "art pixel checks need Pillow and numpy:  "
            "pip install -r requirements.txt") from exc
    Image.MAX_IMAGE_PIXELS = None
    return np, Image, ImageFilter


SCALE = 4       # measure at 1/4 size: exact for luma/sat, ample for the rest


def reconstruct_frame(path: Path, scale: int = SCALE):
    """
    The 16:9 crop the renderer will actually show, at 1/scale size.

    Mirrors `VideoRenderer.build_still_graph` plus the zoompan crop: blurred
    cover-crop backdrop, contained foreground, then the central rectangle the
    zoom sits on at rest. Measuring the *source image* instead would miss the
    whole point — the blur bars only exist once it is in our frame.
    """
    np, Image, ImageFilter = _numpy_and_pil()
    cw, ch = COMP_W // scale, COMP_H // scale
    ow, oh = WIDTH // scale, HEIGHT // scale

    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size

        s = max(cw / w, ch / h)
        bg = im.resize((max(1, round(w * s)), max(1, round(h * s))), Image.LANCZOS)
        left, top = (bg.width - cw) // 2, (bg.height - ch) // 2
        bg = bg.crop((left, top, left + cw, top + ch))
        bg = bg.filter(ImageFilter.GaussianBlur(28 / scale))
        bg = Image.eval(bg, lambda v: max(0, int(v - 0.14 * 255)))

        s = min(cw / w, ch / h)
        fw, fh = max(1, round(w * s)), max(1, round(h * s))
        plate = bg.copy()
        plate.paste(im.resize((fw, fh), Image.LANCZOS), ((cw - fw) // 2,
                                                         (ch - fh) // 2))
        gray = np.asarray(
            im.convert("L").resize((min(512, w), max(1, round(min(512, w) * h / w))),
                                   Image.LANCZOS), dtype="float32")

    x0, y0 = (cw - ow) // 2, (ch - oh) // 2
    return plate.crop((x0, y0, x0 + ow, y0 + oh)), gray, (w, h)


def _laplacian_var(gray) -> float:
    """Variance of a Laplacian — the standard cheap focus measure."""
    np, _, _ = _numpy_and_pil()
    p = np.pad(gray, 1, mode="edge")
    lap = (p[:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:]
           - 4 * p[1:-1, 1:-1])
    return float(lap.var())


def measure(path: Path) -> Metrics:
    np, _, _ = _numpy_and_pil()
    crop, gray, (w, h) = reconstruct_frame(path)

    a = np.asarray(crop, dtype="float32")
    lum = 0.2126 * a[..., 0] + 0.7152 * a[..., 1] + 0.0722 * a[..., 2]
    mx, mn = a.max(axis=2), a.min(axis=2)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0)

    s = SCALE
    wm = lum[30 // s:110 // s, 1450 // s:1890 // s]
    vis = lum[(HEIGHT - VIS_HEIGHT) // s:, :]

    return Metrics(
        width=w, height=h, aspect=round(w / h, 3),
        coverage=round(coverage(w, h), 3),
        upscale=round(upscale_factor(w, h), 3),
        vis_luma=round(float(vis.mean()), 1),
        wm_p90=round(float(np.percentile(wm, 90)), 1),
        saturation=round(float(sat.mean()), 3),
        contrast=round(float(lum.std()), 1),
        sharpness=round(_laplacian_var(gray), 1),
        luma=round(float(lum.mean()), 1),
    )


def pixel_verdict(m: Metrics) -> list[str]:
    """Reasons to reject, given measurements. Empty means keep."""
    checks = [
        (m.upscale > MAX_UPSCALE,
         f"upscaled {m.upscale:.2f}x at the zoom crop"),
        (m.coverage < MIN_COVERAGE,
         f"only {m.coverage * 100:.0f}% of the frame is art "
         f"(aspect {m.aspect})"),
        (m.vis_luma > MAX_VIS_LUMA,
         f"bottom band luma {m.vis_luma:.0f} — the screen-blended visualiser "
         f"washes out"),
        (m.wm_p90 > MAX_WM_P90,
         f"top-right luma {m.wm_p90:.0f} — the watermark has no scrim and "
         f"disappears"),
        (m.saturation < MIN_SATURATION, f"drab (saturation {m.saturation:.2f})"),
        (m.contrast < MIN_CONTRAST, f"flat (contrast {m.contrast:.0f})"),
        (m.sharpness < MIN_SHARPNESS, f"soft (detail {m.sharpness:.0f})"),
        (m.luma < MIN_LUMA, f"too dark (mean luma {m.luma:.0f})"),
    ]
    return [why for failed, why in checks if failed]


def check_file(path: Path) -> list[str]:
    """Measure and judge one file on disk."""
    try:
        return pixel_verdict(measure(path))
    except Exception as exc:
        return [f"unreadable: {type(exc).__name__}: {exc}"]
