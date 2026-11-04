"""
Time the render pipeline on *this* machine.

The encoder settings in nightcore.py were chosen by reasoning about an Ivy
Bridge Xeon, not by measuring one. This exists so the reasoning can be
checked: it builds a synthetic plate and a synthetic tone, runs the real
filtergraphs over them, and reports throughput.

Two numbers matter and they are reported separately:

  * the **loop** pass — the single-threaded zoompan work, paid once per video
    regardless of song length;
  * the **final** pass — showcqt plus x264, paid per second of output.

The final pass is the one to judge: a speed above 1.0x realtime means the
render finishes before the song would have finished playing.
"""

from __future__ import annotations

import time
from pathlib import Path

from nightcore import (COMP_H, COMP_W, FPS, LOOP_CRF, LOOP_PRESET,
                       SAMPLE_RATE, WORK_DIR, VideoRenderer, ZOOM_PERIOD_S,
                       ENCODE_THREADS, HEIGHT, WIDTH, AUDIO_BITRATE)
from nightcore import run as sh

# Enough output to be representative without being a render in its own right.
BENCH_SECONDS = 20

# `veryfast/20` is what the pipeline ships with; the neighbours either side
# show what that choice is actually buying or costing.
MATRIX = [("ultrafast", 20), ("veryfast", 20), ("faster", 20), ("medium", 18)]


def _synthetic_inputs(work: Path) -> tuple[Path, Path]:
    """A plate and a track with roughly the cost profile of the real ones."""
    plate = work / "bench-plate.png"
    audio = work / "bench-audio.wav"

    # Noise rather than a flat colour: a flat plate compresses to nothing and
    # would flatter every preset equally.
    sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i",
        f"color=c=gray:s={COMP_W}x{COMP_H},noise=alls=40:allf=t+u,gblur=sigma=3",
        "-frames:v", "1", str(plate)], quiet=True)

    # A wobbling tone sweeps the constant-Q axis, so showcqt does about as
    # much work as it would on real music.
    sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i",
        f"sine=frequency=110:sample_rate={SAMPLE_RATE}:duration={BENCH_SECONDS},"
        f"vibrato=f=1:d=1,aformat=channel_layouts=stereo",
        str(audio)], quiet=True)
    return plate, audio


def run(fps: int = FPS) -> None:
    """Run the benchmark and print a table."""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    renderer = VideoRenderer(fps=fps)

    print(f"[bench] {WIDTH}x{HEIGHT}@{fps}, {BENCH_SECONDS}s of output, "
          f"{ENCODE_THREADS} encode threads\n")

    plate_src, audio = _synthetic_inputs(WORK_DIR)
    plate = renderer.compose_still(plate_src, WORK_DIR)
    scrim = renderer.compose_scrim(WORK_DIR)

    loop_frames = fps * ZOOM_PERIOD_S
    start = time.monotonic()
    loop = renderer.render_loop(plate, scrim, WORK_DIR,
                                "Benchmark Track", "Test Artist  ·  Nightcore")
    loop_elapsed = time.monotonic() - start
    print(f"\n[bench] loop pass ({LOOP_PRESET}/crf{LOOP_CRF}): "
          f"{loop_elapsed:.1f}s for {loop_frames} frames "
          f"= {loop_frames / loop_elapsed:.1f} fps  "
          f"[paid once per video]\n")

    graph = WORK_DIR / "bench_graph.txt"
    graph.write_text(renderer.build_final_graph(), encoding="utf-8")

    print(f"{'preset':<12}{'crf':<6}{'seconds':<10}{'fps':<9}{'x realtime'}")
    print("-" * 48)
    for preset, crf in MATRIX:
        dest = WORK_DIR / f"bench-{preset}-{crf}.mp4"
        start = time.monotonic()
        sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-stream_loop", "-1", "-i", str(loop),
            "-i", str(audio),
            "-filter_complex_script", str(graph),
            "-map", "[v]", "-map", "1:a",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-threads", str(ENCODE_THREADS),
            "-pix_fmt", "yuv420p", "-r", str(fps),
            "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", str(SAMPLE_RATE),
            "-shortest", str(dest)], quiet=True)
        elapsed = time.monotonic() - start
        size_mb = dest.stat().st_size / 1e6
        print(f"{preset:<12}{crf:<6}{elapsed:<10.1f}"
              f"{BENCH_SECONDS * fps / elapsed:<9.1f}"
              f"{BENCH_SECONDS / elapsed:.2f}x   ({size_mb:.0f} MB/20s)")
        dest.unlink(missing_ok=True)

    print(f"\n[bench] a full song costs {loop_elapsed:.0f}s for the loop pass "
          f"plus the final pass at the rate above.\n"
          f"        Only the second term grows with song length.")

    for leftover in (plate_src, audio, plate, scrim, loop, graph):
        leftover.unlink(missing_ok=True)
