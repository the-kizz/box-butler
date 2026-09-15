"""The folder picker browses and creates — and cannot leave the media root.

This is the one place this feature lets a caller-supplied path reach the
filesystem, so its confinement is tested here directly rather than inferred
from `resolve_within`'s own unit tests: a route that forgot to call
`resolve_within` would still pass those.
"""
from __future__ import annotations

from pathlib import Path


def test_picker_lists_the_media_root_subfolders(auth, media_root):
    (media_root / "Bedtime").mkdir()
    (media_root / "Car Trips").mkdir()

    r = auth.get("/libraries/folders")

    assert r.status_code == 200
    assert "Bedtime" in r.text and "Car Trips" in r.text


def test_picker_descends_into_a_subfolder(auth, media_root):
    (media_root / "Bedtime" / "Wombat Tales").mkdir(parents=True)

    r = auth.get("/libraries/folders", params={"path": "Bedtime"})

    assert r.status_code == 200
    assert "Wombat Tales" in r.text
    assert "Up one level" in r.text


def test_picker_offers_to_create_a_folder_named_after_the_library(auth, media_root):
    r = auth.get("/libraries/folders", params={"name": "Wombat Tales"})
    assert r.status_code == 200
    assert "Wombat Tales" in r.text
    # Offered, not created — the picker suggests, the create route acts.
    assert not (media_root / "Wombat Tales").exists()


def test_picker_refuses_to_leave_the_media_root(auth, media_root):
    outside = media_root.parent / "outside"
    outside.mkdir()
    (outside / "secret").mkdir()
    (media_root / "escape").symlink_to(outside, target_is_directory=True)

    for attempt in ("..", "../outside", "Bedtime/../..", "/etc", str(outside), "escape"):
        r = auth.get("/libraries/folders", params={"path": attempt})
        assert r.status_code == 400, f"{attempt!r} was not refused"
        assert "secret" not in r.text
        assert "passwd" not in r.text


def test_creating_a_library_makes_its_folder_under_the_media_root(auth, store, media_root):
    r = auth.post("/libraries", data={"name": "Wombat Tales", "mode": "serial"})
    assert r.status_code == 303

    lib = next(l for l in store.libraries.list() if l.name == "Wombat Tales")
    assert lib.folder_path == str(media_root / "Wombat Tales")
    assert Path(lib.folder_path).is_dir()


def test_creating_a_library_in_a_chosen_subfolder(auth, store, media_root):
    (media_root / "Existing").mkdir()
    r = auth.post(
        "/libraries", data={"name": "Wombat Tales", "mode": "serial", "folder_path": "Existing"}
    )
    assert r.status_code == 303
    lib = next(l for l in store.libraries.list() if l.name == "Wombat Tales")
    assert lib.folder_path == str(media_root / "Existing")


def test_creating_a_library_cannot_point_outside_the_media_root(auth, store, media_root):
    outside = media_root.parent / "outside"
    outside.mkdir()

    for attempt in ("../outside", "/etc", str(outside)):
        r = auth.post(
            "/libraries", data={"name": f"Bad {attempt}", "mode": "single", "folder_path": attempt}
        )
        # Refused with a message, never a library pointing at the host.
        assert r.status_code == 303
        assert "/libraries?announce=" in r.headers["location"]

    assert store.libraries.list() == [] or all(
        str(media_root) == str(Path(l.folder_path).parent) or not l.folder_path
        for l in store.libraries.list()
    )
    assert list(outside.iterdir()) == []
