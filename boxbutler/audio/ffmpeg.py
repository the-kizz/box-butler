"""ffprobe/ffmpeg-backed `RendererProtocol` (spec §3.4, §3.5).

## Loudnorm defaults OFF — this is deliberate, not an oversight

The operator measured a real bedtime story: LRA **21.3**, input_i
**-24.44 LUFS**. That looks like a defect. It is not.

These files are a **loud narrated story followed by a deliberately quiet
ambient tail**, so the child drifts off to sleep. Normalising to the usual
streaming target (LRA 11) *raises the quiet part* relative to the loud
part — which is exactly backwards for something meant to get quieter at
bedtime. Loudnorm is opt-in per item (`RenderSpec.loudnorm`), for
mixed-source libraries whose items come from different origins and
genuinely need levelling. Do not flip this default "to fix" an LRA
number that looks alarming out of context — read this docstring first.

## Stream-copy trim is preferred whenever possible

`-t cap -c copy` was measured at ~17s versus minutes for a re-encode, with
zero quality loss. Task 13 pinned `-f 140` (AAC 129k 44.1kHz stereo)
specifically so that most YouTube-sourced audio lands in a shape this
layer can trim without transcoding. `choose_mode` (protocol.py) only
transcodes when the sink truly can't accept the container/codec as-is.

## Loudnorm, when requested, is always two-pass

Single-pass loudnorm uses a moving window and audibly pumps across a
story/ambient boundary like the one above — exactly the content this
tool targets. Two-pass (measure with `print_format=json`, then apply the
measured values with `linear=true`) avoids that. The trim measures the
**trimmed** range: pass 1 runs with `-t cap` too, so the measurement
matches what pass 2 actually produces, not the full untrimmed source.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from boxbutler.audio.protocol import (
    ProbeResult,
    RenderError,
    RenderSpec,
    RenditionInfo,
    choose_mode,
)
from boxbutler.domain.models import RenditionMode

_LOUDNORM_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=11"
_LOUDNORM_BLOCK_RE = re.compile(r"\{.*?\}", re.DOTALL)


def trim_args(src: Path, dst: Path, cap: int) -> list[str]:
    return [
        "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
        "-i", str(src), "-t", str(cap), "-c", "copy", str(dst),
    ]


def transcode_args(src: Path, dst: Path, cap: int) -> list[str]:
    return [
        "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
        "-i", str(src), "-t", str(cap),
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        str(dst),
    ]


def loudnorm_measure_args(src: Path, cap: int) -> list[str]:
    # Measures the *trimmed* range (-t cap on pass 1) so pass 2's values
    # match what actually gets produced, not the whole untrimmed source.
    # loglevel "info" (not "error") because the loudnorm filter prints its
    # JSON measurement block at info level, to stderr.
    return [
        "ffmpeg", "-nostdin", "-y", "-loglevel", "info",
        "-i", str(src), "-t", str(cap),
        "-af", f"{_LOUDNORM_FILTER}:print_format=json",
        "-f", "null", "-",
    ]


def loudnorm_apply_args(src: Path, dst: Path, cap: int, measured: dict) -> list[str]:
    af = (
        f"{_LOUDNORM_FILTER}"
        f":measured_I={measured.get('input_i')}"
        f":measured_TP={measured.get('input_tp')}"
        f":measured_LRA={measured.get('input_lra')}"
        f":measured_thresh={measured.get('input_thresh')}"
        f":offset={measured.get('target_offset', '0')}"
        f":linear=true"
    )
    return [
        "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
        "-i", str(src), "-t", str(cap),
        "-af", af,
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        str(dst),
    ]


def parse_loudnorm_json(stderr: str) -> dict:
    """Extract the loudnorm measurement block: the *last* `{...}` in
    stderr (ffmpeg may print other braces earlier; the filter's own
    print_format=json block is always the final one)."""
    blocks = _LOUDNORM_BLOCK_RE.findall(stderr)
    if not blocks:
        raise RenderError("no loudnorm JSON block found in ffmpeg stderr")
    return json.loads(blocks[-1])


class FfmpegRenderer:
    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
    ):
        self._runner = runner
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        if args and args[0] == "ffmpeg":
            args = [self._ffmpeg, *args[1:]]
        result = self._runner(args, capture_output=True, text=True)
        if result.returncode != 0:
            raise RenderError((result.stderr or "")[-500:])
        return result

    def probe(self, path: Path) -> ProbeResult:
        args = [
            self._ffprobe, "-v", "error",
            "-show_entries",
            "format=duration,size,format_name:stream=codec_name,sample_rate,channels:format_tags",
            "-of", "json", str(path),
        ]
        result = self._runner(args, capture_output=True, text=True)
        if result.returncode != 0:
            raise RenderError((result.stderr or "")[-500:])
        if not result.stdout.strip():
            raise RenderError(f"ffprobe produced no output for {path}")
        data = json.loads(result.stdout)
        fmt = data.get("format", {})
        streams = data.get("streams", [])
        audio = next((s for s in streams if s.get("codec_type") in (None, "audio")), {})

        # A missing, unparseable, zero, or negative duration is a probe
        # failure, not "0.0 seconds" — treating it as a sentinel would let
        # choose_mode's `probe.seconds <= cap` trivially pass and route
        # content of unknown length to COPY mode: shipped untouched and
        # recorded as a complete rendition. That is the same failure
        # family as a partial download or a killed render being mistaken
        # for a finished one — fail closed instead.
        raw_duration = fmt.get("duration")
        try:
            seconds = float(raw_duration)
        except (TypeError, ValueError):
            raise RenderError(
                f"ffprobe returned no usable duration for {path} (format.duration={raw_duration!r})"
            ) from None
        if seconds <= 0:
            raise RenderError(f"ffprobe reported non-positive duration {seconds!r} for {path}")

        return ProbeResult(
            seconds=seconds,
            codec=audio.get("codec_name", ""),
            sample_rate=int(audio.get("sample_rate", 0) or 0),
            channels=int(audio.get("channels", 0) or 0),
            bytes=int(fmt.get("size", 0) or 0),
            container=fmt.get("format_name", ""),
            tags=dict(fmt.get("tags") or {}),
        )

    def render(self, src: Path, dst: Path, spec: RenderSpec) -> RenditionInfo:
        probe = self.probe(src)
        mode = choose_mode(probe, spec)

        # Write to a temp name and rename atomically on success only: a
        # killed ffmpeg (or a raised RenderError) must never leave a
        # partial/truncated file sitting at `dst` where it could be
        # mistaken for a finished rendition (a half-length story landing
        # on a child's tonie). Same *goal* as the fetch layer's partial-
        # download guard (boxbutler/fetch/http.py), but a deliberately
        # different naming scheme: http.py uses `target.suffix + ".part"`
        # (".part" last, e.g. "name.mp3.part") because it never re-opens
        # that file with ffmpeg. Here the real extension must stay last
        # (out.part.m4a, not out.m4a.part) so ffmpeg can infer the output
        # muxer from the filename — a name ending in ".part" makes ffmpeg
        # refuse to pick a muxer at all, and reordering these to "match"
        # the fetch layer would silently let it fall back to whatever
        # muxer matches `dst`'s real extension regardless of what codec
        # was actually written, i.e. exactly the corruption this guard
        # exists to prevent. Do not unify the two conventions.
        part = dst.with_name(dst.stem + ".part" + dst.suffix)
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            if mode == RenditionMode.COPY:
                shutil.copyfile(src, part)
                seconds = probe.seconds
            elif mode == RenditionMode.TRIM:
                self._run(trim_args(src, part, spec.cap_seconds))
                seconds = self.probe(part).seconds
            elif mode == RenditionMode.TRANSCODE:
                self._run(transcode_args(src, part, spec.cap_seconds))
                seconds = self.probe(part).seconds
            elif mode == RenditionMode.LOUDNORM:
                measured_run = self._run(loudnorm_measure_args(src, spec.cap_seconds))
                measured = parse_loudnorm_json(measured_run.stderr or "")
                self._run(loudnorm_apply_args(src, part, spec.cap_seconds, measured))
                seconds = self.probe(part).seconds
            else:  # pragma: no cover - choose_mode is exhaustive over RenditionMode
                raise RenderError(f"unhandled rendition mode: {mode!r}")
        except BaseException:
            part.unlink(missing_ok=True)
            raise

        size = part.stat().st_size
        os.replace(part, dst)
        return RenditionInfo(path=dst, seconds=seconds, bytes=size, mode=mode)
