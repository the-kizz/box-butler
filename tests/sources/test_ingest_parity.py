import io, dataclasses
from boxbutler.domain.models import Item, ItemKind
from boxbutler.sources.protocol import ResolvedItem
from boxbutler.sources.ingest import Ingestor

class Stub:
    def __init__(self, kind, key): self.kind, self.key = kind, key
    def matches(self, ref): return ref.startswith(f"{self.kind}:")
    def resolve(self, ref): return [ResolvedItem(self.kind, ref, self.key, "Title 🍝 " + "x" * 200)]

def test_every_resolver_yields_equivalent_items(store, tmp_path):
    lib = store.libraries.create("L")
    kinds = [ItemKind.YOUTUBE, ItemKind.PLAYLIST_ENTRY, ItemKind.RSS, ItemKind.URL]
    ing = Ingestor(store, [Stub(k, f"key-{k}") for k in kinds])
    items = [ing.add_ref(lib.id, f"{k}:ref")[0] for k in kinds]
    for it in items:
        assert isinstance(it, Item) and it.library_id == lib.id and it.enabled and it.state == "ok"
        assert "🍝" not in it.title and len(it.title) <= 110              # sanitised the same way everywhere
    assert [i.position for i in items] == [0, 1, 2, 3]

def test_upload_in_accepts_format_ships_without_reencoding_and_others_transcode():
    # decided by choose_mode (Task 14), asserted here for the ingest story (§10.14)
    from boxbutler.audio.protocol import ProbeResult, RenderSpec, choose_mode
    from boxbutler.domain.models import RenditionMode
    acc = ("aac","aif","aiff","flac","mp3","m4a","m4b","wav","oga","ogg","opus","wma")
    assert choose_mode(ProbeResult(600, "mp3", 44100, 2, 1, "mp3", {}), RenderSpec(5340, accepts=acc)) == RenditionMode.COPY
    assert choose_mode(ProbeResult(600, "wmav2", 44100, 2, 1, "asf", {}), RenderSpec(5340, accepts=acc)) == RenditionMode.TRANSCODE
