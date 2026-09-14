import shutil, subprocess, pytest
from pathlib import Path
from boxbutler.audio.ffmpeg import FfmpegRenderer
from boxbutler.audio.protocol import RenderSpec
from boxbutler.domain.models import RenditionMode

pytestmark = pytest.mark.ffmpeg


@pytest.fixture(scope="session")
def fixtures(tmp_path_factory):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not on PATH")
    d = tmp_path_factory.mktemp("audio")
    # 12 s AAC 44.1k stereo sine; 3 s opus in webm; 5 s mp3 — generated, never committed
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=12", "-ar", "44100", "-ac", "2", "-c:a", "aac", str(d / "long.m4a")], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-c:a", "libopus", str(d / "short.webm")], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=5", "-c:a", "libmp3lame", str(d / "mid.mp3")], check=True)
    return d


def test_trim_is_stream_copy_and_within_tolerance(fixtures, tmp_path):
    r = FfmpegRenderer()
    info = r.render(fixtures / "long.m4a", tmp_path / "out.m4a", RenderSpec(8))
    assert info.mode == RenditionMode.TRIM and abs(info.seconds - 8) < 0.5 and r.probe(info.path).codec == "aac"


def test_transcode_webm_to_aac(fixtures, tmp_path):
    info = FfmpegRenderer().render(fixtures / "short.webm", tmp_path / "out.m4a", RenderSpec(8))
    assert info.mode == RenditionMode.TRANSCODE and FfmpegRenderer().probe(info.path).codec == "aac"


def test_loudnorm_two_pass_produces_capped_output(fixtures, tmp_path):
    info = FfmpegRenderer().render(fixtures / "long.m4a", tmp_path / "out.m4a", RenderSpec(6, loudnorm=True))
    assert info.mode == RenditionMode.LOUDNORM and abs(info.seconds - 6) < 0.5


def test_copy_mode_leaves_bytes_identical(fixtures, tmp_path):
    src = fixtures / "mid.mp3"
    info = FfmpegRenderer().render(src, tmp_path / "out.mp3", RenderSpec(8))
    assert info.mode == RenditionMode.COPY and (tmp_path / "out.mp3").read_bytes() == src.read_bytes()
