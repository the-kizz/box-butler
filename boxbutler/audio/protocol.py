"""`RendererProtocol` — the seam between the ingest pipeline and ffmpeg
(spec §3.4, §3.5).

Nothing above this layer may know whether a rendition was produced by a
stream-copy trim, a transcode, a two-pass loudnorm, or not touched at all
(`RenditionMode.COPY`). Everything above talks to `RendererProtocol.probe()`
/ `.render()`.

Loudness normalisation is **off by default** and is opt-in per item — this
is not an oversight (see the long docstring in `boxbutler/audio/ffmpeg.py`
for the measurement that proves it out). `choose_mode` below is the single
place that decides what a given source + spec combination gets, so the
policy lives in one, testable function rather than being re-derived at
every call site.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from boxbutler.domain.models import RenditionMode

# The sink's 12 accepted formats (measured, spec §3.4): anything in this
# list can ship untranscoded. This is what makes the operator's own .m4b
# audiobooks and .flac albums cheap — they never touch ffmpeg unless they
# also need trimming or loudnorm.
SINK_ACCEPTS: tuple[str, ...] = (
    "aac", "aif", "aiff", "flac", "mp3", "m4a", "m4b",
    "wav", "oga", "ogg", "opus", "wma",
)

# Codecs ffprobe reports that correspond to one of the accepted extensions
# above. A container can carry an accepted extension token (e.g. "mp3") yet
# hold a codec the sink can't actually play (unusual, but cheap to guard);
# both container *and* codec must check out for anything other than
# TRANSCODE.
_ACCEPTED_CODECS: frozenset[str] = frozenset({
    "aac", "mp3", "flac", "opus", "vorbis",
    "pcm_s16le", "pcm_s16be", "pcm_f32le", "pcm_s24le",
    "alac", "wmav1", "wmav2", "wmapro",
})

# ffprobe's `container` field is a comma-joined list of format names (see
# `_container_accepted` above); this maps that raw container string to the
# single filename extension a resolver/fetcher should use when it needs to
# name a file for the source's own format (e.g. `UploadSource`/Task 17's
# folder scanner deciding what a discovered file "is"). Anything not in
# this map isn't one of the sink's accepted containers.
CONTAINER_EXT: dict[str, str] = {
    "mov,mp4,m4a,3gp,3g2,mj2": "m4a",
    "mp3": "mp3",
    "flac": "flac",
    "ogg": "ogg",
    "wav": "wav",
    "asf": "wma",
    "aiff": "aiff",
}


@dataclass(frozen=True)
class ProbeResult:
    seconds: float
    codec: str
    sample_rate: int
    channels: int
    bytes: int
    container: str
    tags: dict[str, str]


@dataclass(frozen=True)
class RenderSpec:
    cap_seconds: int
    loudnorm: bool = False
    accepts: tuple[str, ...] = SINK_ACCEPTS


@dataclass(frozen=True)
class RenditionInfo:
    path: Path
    seconds: float
    bytes: int
    mode: RenditionMode


class RenderError(Exception):
    """ffmpeg/ffprobe failed. Message is the tail of stderr."""


class RendererProtocol(Protocol):
    def probe(self, path: Path) -> ProbeResult:
        """Inspect a cached source file without altering it."""
        ...

    def render(self, src: Path, dst: Path, spec: RenderSpec) -> RenditionInfo:
        """Produce a rendition of `src` at `dst` per `spec`.

        Must never leave a partial/truncated file at `dst`: a killed
        renderer should leave nothing usable there at all.
        """
        ...


def _container_accepted(container: str, accepts: tuple[str, ...]) -> bool:
    tokens = {t.strip().lower() for t in container.split(",") if t.strip()}
    return bool(tokens & set(accepts))


def choose_mode(probe: ProbeResult, spec: RenderSpec) -> RenditionMode:
    """Decide how a source becomes a rendition (spec §3.4/§3.5).

    Order matters:
    1. An explicit loudnorm request always wins — the operator asked for it
       on this item, so it happens regardless of what would otherwise be a
       free stream copy.
    2. A container/codec the sink can't accept always needs a transcode,
       no matter the duration.
    3. Otherwise: already short enough and already an accepted format ->
       ship the source completely untouched (no ffmpeg invocation at all).
    4. Otherwise: too long, but a shape ffmpeg can trim without
       re-encoding -> stream-copy trim.
    """
    if spec.loudnorm:
        return RenditionMode.LOUDNORM
    container_ok = _container_accepted(probe.container, spec.accepts)
    codec_ok = probe.codec.lower() in _ACCEPTED_CODECS
    if not (container_ok and codec_ok):
        return RenditionMode.TRANSCODE
    if probe.seconds <= spec.cap_seconds:
        return RenditionMode.COPY
    return RenditionMode.TRIM


def rendition_name(item_title: str, source_key: str, spec: RenderSpec, mode: RenditionMode) -> str:
    """Name for a rendition that sits beside its source in the cache.

    Only meaningful for the ffmpeg-producing modes (TRIM/TRANSCODE/
    LOUDNORM), which always emit AAC in an .m4a container regardless of the
    source's own extension. COPY mode ships the source file itself — its
    own `cache_name()` (spec §3.5) is the path to use, not this one.
    """
    from boxbutler.domain.cache_name import sanitise_title

    return f"{sanitise_title(item_title)} [{source_key}].{mode}{spec.cap_seconds}.m4a"
