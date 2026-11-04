"""
End-to-end render over synthetic inputs. Needs ffmpeg; marked slow.

    python -m pytest tests -m slow

The thing this protects is the three-pass split. Passes B and C communicate
through a file and a `-stream_loop`, so a filtergraph mistake — a mislabelled
pad, an input index off by one after the scrim moved from input 2 to input 1 —
produces a perfectly valid ffmpeg invocation that renders the wrong thing.
Only actually running it catches that.
"""

from __future__ import annotations

import shutil

import pytest

import nightcore
from nightcore import (COMP_H, COMP_W, SAMPLE_RATE, VideoRenderer, run,
                       verify)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg"),
]

CLIP_SECONDS = 6
# Deliberately below the 24-second loop period, so the render is forced to
# truncate mid-cycle — the case `-shortest` has to get right.
FPS = 30


@pytest.fixture
def inputs(tmp_path):
    image = tmp_path / "art.png"
    audio = tmp_path / "tone.wav"
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i",
         f"testsrc2=s={COMP_W}x{COMP_H}", "-frames:v", "1", str(image)],
        quiet=True)
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i",
         f"sine=frequency=220:sample_rate={SAMPLE_RATE}:duration={CLIP_SECONDS},"
         f"aformat=channel_layouts=stereo",
         str(audio)], quiet=True)
    return image, audio


def test_three_pass_render_produces_a_valid_video(inputs, tmp_path):
    image, audio = inputs
    dest = tmp_path / "out.mp4"

    VideoRenderer(fps=FPS).render(
        image=image, audio=audio, dest=dest,
        title="Test Title", subtitle="Test Artist  ·  Nightcore",
        work_dir=tmp_path)

    # `verify` is the production check: resolution, pixel format, audio
    # layout, and that the video runs for the whole song rather than for one
    # loop period.
    verify(dest, CLIP_SECONDS)


def test_loop_clip_is_exactly_one_zoom_period(inputs, tmp_path):
    """
    The seamlessness of the loop depends on the clip being exactly
    ZOOM_PERIOD_S long — one full sine cycle. A frame too many or too few and
    every repeat shows a visible jump.
    """
    image, _ = inputs
    renderer = VideoRenderer(fps=FPS)
    plate = renderer.compose_still(image, tmp_path)
    scrim = renderer.compose_scrim(tmp_path)
    loop = renderer.render_loop(plate, scrim, tmp_path, "T", "S")

    frames = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                  "-count_frames", "-show_entries", "stream=nb_read_frames",
                  "-of", "csv=p=0", str(loop)], capture=True, quiet=True)
    assert int(frames.strip()) == FPS * nightcore.ZOOM_PERIOD_S


def test_title_text_with_shell_hostile_characters(inputs, tmp_path):
    """
    Song titles carry colons, apostrophes, commas and brackets, all of which
    are filtergraph syntax — and an apostrophe cannot be escaped inside a
    quoted `text=` at all. This is the regression test for the switch to
    `textfile=`, and the only place it meets a real ffmpeg parser.
    """
    image, audio = inputs
    dest = tmp_path / "out.mp4"
    VideoRenderer(fps=FPS).render(
        image=image, audio=audio, dest=dest,
        title="Don't Stop: [Remix], 100%",
        subtitle="A, B & C  ·  Nightcore",
        work_dir=tmp_path)
    assert dest.stat().st_size > 0
