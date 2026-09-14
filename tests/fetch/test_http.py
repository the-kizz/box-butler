import httpx
from boxbutler.domain.models import Item, ItemKind
from boxbutler.fetch.http import HttpFetcher

def test_downloads_to_cache_name_and_is_idempotent(tmp_path):
    hits = []
    def handler(req): hits.append(req.url); return httpx.Response(200, content=b"mp3bytes", headers={"content-type": "audio/mpeg"})
    f = HttpFetcher(httpx.Client(transport=httpx.MockTransport(handler)))
    item = Item("i", "L", 0, ItemKind.RSS, "https://example.invalid/ep1.mp3", "guid-1", "Episode 1")
    p = f.fetch(item, tmp_path)
    assert p.name == "Episode 1 [guid-1].mp3" and p.read_bytes() == b"mp3bytes"
    assert f.fetch(item, tmp_path) == p and len(hits) == 1

def test_http_error_is_item_unavailable(tmp_path):
    from boxbutler.fetch.protocol import ItemUnavailable
    import pytest
    f = HttpFetcher(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    with pytest.raises(ItemUnavailable):
        f.fetch(Item("i", "L", 0, ItemKind.URL, "https://example.invalid/x.mp3", "k", "T"), tmp_path)

def test_short_body_against_content_length_is_refused_and_not_cached(tmp_path):
    """A truncated download is not a successful fetch (final safety review, M2).

    `fetch` used to stream the body and `os.replace` it into place without
    ever comparing what it wrote against what the server said it was sending.
    The short file then passed the `exists() and st_size > 0` cache check on
    every later run, and the rendition re-verified against its *own* probe
    forever -- so the store said 3600s, 900s was on disk, and the run
    reported success. Permanent, and self-reinforcing.
    """
    from boxbutler.fetch.protocol import ItemUnavailable
    import pytest
    def handler(req):
        # 8 bytes promised, 3 delivered.
        return httpx.Response(200, content=b"mp3", headers={"content-type": "audio/mpeg", "content-length": "8"})
    f = HttpFetcher(httpx.Client(transport=httpx.MockTransport(handler)))
    item = Item("i", "L", 0, ItemKind.RSS, "https://example.invalid/ep1.mp3", "guid-1", "Episode 1")
    with pytest.raises(ItemUnavailable) as e:
        f.fetch(item, tmp_path)
    assert "short read" in str(e.value)
    # Nothing incomplete was promoted into the cache, and no `.part` was left
    # behind for a later run to trip over.
    assert list(tmp_path.iterdir()) == []

def test_absent_content_length_is_recorded_as_unknown_not_as_agreement(tmp_path):
    """No stated length means the byte count proved nothing -- said so (M2)."""
    # An iterator body is sent chunked, so the response carries no
    # `Content-Length` at all -- the connection-close-delimited shape a
    # misbehaving CDN proxy produces, and the one httpx cannot complain
    # about.
    def handler(req):
        return httpx.Response(200, content=iter([b"mp3", b"bytes"]), headers={"content-type": "audio/mpeg"})
    f = HttpFetcher(httpx.Client(transport=httpx.MockTransport(handler)))
    item = Item("i", "L", 0, ItemKind.RSS, "https://example.invalid/ep2.mp3", "guid-2", "Episode 2")
    assert "content-length" not in httpx.Response(200, content=iter([b"x"])).headers
    assert f.fetch(item, tmp_path).read_bytes() == b"mp3bytes"
    assert f.last_length_unverified is True

def test_exact_content_length_is_a_normal_fetch(tmp_path):
    def handler(req):
        return httpx.Response(200, content=b"mp3bytes", headers={"content-type": "audio/mpeg", "content-length": "8"})
    f = HttpFetcher(httpx.Client(transport=httpx.MockTransport(handler)))
    item = Item("i", "L", 0, ItemKind.RSS, "https://example.invalid/ep3.mp3", "guid-3", "Episode 3")
    assert f.fetch(item, tmp_path).read_bytes() == b"mp3bytes"
    assert f.last_length_unverified is False

def test_a_compressed_body_is_not_compared_against_the_encoded_length(tmp_path):
    """`Content-Length` describes the encoded body; httpx hands over the
    decoded one. Comparing them would fail a perfectly good download, so the
    stated length is treated as unknown rather than as a mismatch (M2)."""
    import gzip
    payload = b"mp3bytes-and-more"
    body = gzip.compress(payload)
    def handler(req):
        return httpx.Response(200, content=body, headers={
            "content-type": "audio/mpeg", "content-encoding": "gzip",
            "content-length": str(len(body)),
        })
    f = HttpFetcher(httpx.Client(transport=httpx.MockTransport(handler)))
    item = Item("i", "L", 0, ItemKind.RSS, "https://example.invalid/ep4.mp3", "guid-4", "Episode 4")
    assert f.fetch(item, tmp_path).read_bytes() == payload
    assert f.last_length_unverified is True
