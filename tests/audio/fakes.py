"""In-memory fake `RendererProtocol` for tests above the audio layer.

Never touches ffmpeg/ffprobe. `probe()` returns a fixed AAC 44.1k stereo
shape (seconds looked up by filename, or 6000.0 if not given — deliberately
above the 5395s default cap so callers exercise TRIM/fit logic unless they
ask for something else). `render()` writes a small placeholder file so
callers checking `.exists()` / size don't need a real audio file.
"""
from __future__ import annotations

from pathlib import Path

from boxbutler.audio.protocol import ProbeResult, RenderError, RenderSpec, RenditionInfo
from boxbutler.domain.models import RenditionMode


class FakeRenderer:
    def __init__(self, durations: dict[str, float] | None = None, fail: set[str] | None = None):
        # Public, mutable: callers above the audio layer (Task 21's
        # orchestrator tests) inject a failure or a duration *after* the
        # fixture is built, once they know the rendition filename the
        # orchestrator will choose.
        self.durations = durations or {}
        self.fail = fail or set()
        self.calls: list[tuple[str, str]] = []

    def probe(self, path: Path) -> ProbeResult:
        seconds = self.durations.get(path.name, 6000.0)
        return ProbeResult(seconds, "aac", 44100, 2, 1000, "mov,mp4,m4a", {})

    def render(self, src: Path, dst: Path, spec: RenderSpec) -> RenditionInfo:
        self.calls.append(("render", src.name))
        if src.name in self.fail:
            raise RenderError(f"fake render failure for {src.name}")
        probe = self.probe(src)
        seconds = min(probe.seconds, spec.cap_seconds)
        dst.write_bytes(b"rendition")
        mode = RenditionMode.LOUDNORM if spec.loudnorm else RenditionMode.TRIM
        return RenditionInfo(path=dst, seconds=seconds, bytes=len(b"rendition"), mode=mode)
