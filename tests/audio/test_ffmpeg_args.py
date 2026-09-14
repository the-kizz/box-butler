import subprocess, json
from pathlib import Path
import pytest

from boxbutler.audio.protocol import ProbeResult, RenderError, RenderSpec, choose_mode, rendition_name
from boxbutler.audio.ffmpeg import (
    FfmpegRenderer,
    trim_args,
    transcode_args,
    loudnorm_measure_args,
    loudnorm_apply_args,
    parse_loudnorm_json,
)
from boxbutler.domain.models import RenditionMode

AAC = ProbeResult(9010.0, "aac", 44100, 2, 100, "mov,mp4,m4a", {})
ACCEPTS = ("aac","aif","aiff","flac","mp3","m4a","m4b","wav","oga","ogg","opus","wma")


def test_stream_copy_trim_preferred_for_aac_source():
    assert choose_mode(AAC, RenderSpec(5340, accepts=ACCEPTS)) == RenditionMode.TRIM


def test_short_accepted_source_ships_as_is():
    assert choose_mode(ProbeResult(1000.0, "mp3", 44100, 2, 1, "mp3", {}), RenderSpec(5340, accepts=ACCEPTS)) == RenditionMode.COPY


def test_unaccepted_container_transcodes():
    webm = ProbeResult(1000.0, "opus", 48000, 2, 1, "matroska,webm", {})
    assert choose_mode(webm, RenderSpec(5340, accepts=ACCEPTS)) == RenditionMode.TRANSCODE


def test_loudnorm_wins_when_asked():
    assert choose_mode(AAC, RenderSpec(5340, loudnorm=True, accepts=ACCEPTS)) == RenditionMode.LOUDNORM


def test_trim_args_are_stream_copy():
    a = trim_args(Path("s.m4a"), Path("d.m4a"), 5340)
    assert a[0] == "ffmpeg" and "-c" in a and a[a.index("-c") + 1] == "copy" and a[a.index("-t") + 1] == "5340"


def test_transcode_args_target_aac_44k_stereo():
    a = transcode_args(Path("s.webm"), Path("d.m4a"), 5340)
    assert a[a.index("-c:a") + 1] == "aac" and a[a.index("-ar") + 1] == "44100" and a[a.index("-ac") + 1] == "2"


def test_loudnorm_measure_args_are_json_and_capped():
    a = loudnorm_measure_args(Path("s.m4a"), 5340)
    assert a[0] == "ffmpeg"
    assert a[a.index("-t") + 1] == "5340"
    af = a[a.index("-af") + 1]
    assert "loudnorm=I=-16:TP=-1.5:LRA=11" in af
    assert "print_format=json" in af


def test_loudnorm_apply_args_thread_measured_values_and_are_capped():
    measured = {
        "input_i": "-24.44",
        "input_tp": "-3.5",
        "input_lra": "21.3",
        "input_thresh": "-34.9",
        "target_offset": "0.7",
    }
    a = loudnorm_apply_args(Path("s.m4a"), Path("d.m4a"), 5340, measured)
    assert a[0] == "ffmpeg"
    assert a[a.index("-t") + 1] == "5340"
    af = a[a.index("-af") + 1]
    assert "loudnorm=I=-16:TP=-1.5:LRA=11" in af
    assert "linear=true" in af
    assert "measured_I=-24.44" in af
    assert "measured_TP=-3.5" in af
    assert "measured_LRA=21.3" in af
    assert "measured_thresh=-34.9" in af
    assert "offset=0.7" in af
    assert a[a.index("-c:a") + 1] == "aac" and a[a.index("-ar") + 1] == "44100" and a[a.index("-ac") + 1] == "2"


def test_parse_loudnorm_json_takes_last_block():
    err = 'noise {"a":1}\n[Parsed_loudnorm] {\n "input_i" : "-24.44",\n "input_lra" : "21.3"\n}\n'
    assert parse_loudnorm_json(err)["input_i"] == "-24.44"


def test_rendition_name_sits_beside_source():
    assert rendition_name("Story 🍝", "vid1", RenderSpec(5340), RenditionMode.TRIM) == "Story [vid1].trim5340.m4a"


def test_probe_parses_ffprobe_json():
    payload = {"format": {"duration": "9010.2", "size": "150", "format_name": "mov,mp4,m4a", "tags": {"title": "T"}},
               "streams": [{"codec_type": "audio", "codec_name": "aac", "sample_rate": "44100", "channels": 2}]}
    r = FfmpegRenderer(runner=lambda a, **k: subprocess.CompletedProcess(a, 0, json.dumps(payload), ""))
    p = r.probe(Path("x.m4a"))
    assert (p.seconds, p.codec, p.sample_rate, p.channels, p.tags["title"]) == (9010.2, "aac", 44100, 2, "T")


def _ffprobe_runner(stdout: str):
    return lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout, "")


def test_probe_fails_closed_when_duration_missing():
    payload = {"format": {"size": "150", "format_name": "mp3", "tags": {}},
               "streams": [{"codec_type": "audio", "codec_name": "mp3", "sample_rate": "44100", "channels": 2}]}
    r = FfmpegRenderer(runner=_ffprobe_runner(json.dumps(payload)))
    with pytest.raises(RenderError):
        r.probe(Path("x.mp3"))


def test_probe_fails_closed_when_duration_not_numeric():
    payload = {"format": {"duration": "N/A", "size": "150", "format_name": "mp3", "tags": {}},
               "streams": [{"codec_type": "audio", "codec_name": "mp3", "sample_rate": "44100", "channels": 2}]}
    r = FfmpegRenderer(runner=_ffprobe_runner(json.dumps(payload)))
    with pytest.raises(RenderError):
        r.probe(Path("x.mp3"))


def test_probe_fails_closed_when_duration_zero():
    payload = {"format": {"duration": "0", "size": "150", "format_name": "mp3", "tags": {}},
               "streams": [{"codec_type": "audio", "codec_name": "mp3", "sample_rate": "44100", "channels": 2}]}
    r = FfmpegRenderer(runner=_ffprobe_runner(json.dumps(payload)))
    with pytest.raises(RenderError):
        r.probe(Path("x.mp3"))


def test_probe_fails_closed_on_empty_stdout():
    r = FfmpegRenderer(runner=_ffprobe_runner(""))
    with pytest.raises(RenderError):
        r.probe(Path("x.mp3"))


def test_killed_ffmpeg_leaves_no_usable_output(tmp_path):
    # A killed/failing ffmpeg must never leave a plausible-looking finished
    # file at `dst` — that's the half-length-story-on-a-child's-tonie risk.
    # The fake runner mimics a process that got partway through writing
    # output before it was killed (nonzero exit, but bytes already on
    # disk at the target path) to prove the guard covers that case too,
    # not just "ffmpeg wrote nothing".
    probe_payload = {
        "format": {"duration": "9010.0", "size": "100", "format_name": "mov,mp4,m4a", "tags": {}},
        "streams": [{"codec_type": "audio", "codec_name": "aac", "sample_rate": "44100", "channels": 2}],
    }

    def fake_runner(args, **kwargs):
        if args[0] == "ffprobe":
            return subprocess.CompletedProcess(args, 0, json.dumps(probe_payload), "")
        Path(args[-1]).write_bytes(b"PARTIAL-GARBAGE-FROM-KILLED-PROCESS")
        return subprocess.CompletedProcess(args, 1, "", "Killed")

    src = tmp_path / "s.m4a"
    src.write_bytes(b"source")
    dst = tmp_path / "out.m4a"

    renderer = FfmpegRenderer(runner=fake_runner)
    try:
        renderer.render(src, dst, RenderSpec(10))
        assert False, "expected RenderError"
    except RenderError:
        pass

    assert not dst.exists()
    leftovers = [p for p in tmp_path.iterdir() if p.name not in (src.name,)]
    assert leftovers == [], f"partial file left behind: {leftovers}"
