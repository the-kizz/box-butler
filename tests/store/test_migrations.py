"""Migration tests (Task 6, brief step 1).

R5 (controller ruling): chapter_record.sink_chapter_id and item.playlist_source_id
are created in THIS initial migration, not added later — nothing has shipped yet,
so there is no data to migrate. Verified here via PRAGMA table_info.
"""
from boxbutler.store.db import connect, migrate, Store


def test_migrate_creates_all_tables(tmp_path):
    conn = connect(tmp_path / "bb.sqlite")
    v = migrate(conn)
    names = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert {"library", "item", "assignment", "rendition", "run", "run_event", "chapter_record",
            "chapter_history", "settings", "schema_version", "source_file",
            "swap_marker"} <= names
    assert v == 3


def test_migrate_is_idempotent(tmp_path):
    conn = connect(tmp_path / "bb.sqlite")
    assert migrate(conn) == migrate(conn) == 3


def test_foreign_keys_on(tmp_path):
    s = Store.open(tmp_path / "bb.sqlite")
    assert s.conn.execute("pragma foreign_keys").fetchone()[0] == 1


def test_foreign_key_violation_raises(tmp_path):
    import sqlite3
    import pytest

    conn = connect(tmp_path / "bb.sqlite")
    migrate(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO item (id, library_id, position, kind, source_ref, source_key, title, added_at) "
            "VALUES ('i1', 'missing-lib', 0, 'youtube', 'x', 'x', 'x', datetime('now'))"
        )


def test_migration_0002_source_file_shape(tmp_path):
    """Task 21: the source-file cache index. item_id is the primary key (one
    source file per item) and cache_path is UNIQUE (two items must never
    claim the same cached bytes, or evicting one deletes the other's)."""
    conn = connect(tmp_path / "bb.sqlite")
    migrate(conn)
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(source_file)")}
    assert set(cols) == {"item_id", "cache_path", "bytes", "fetched_at", "last_used_at"}
    assert cols["item_id"][5] == 1          # pk
    assert cols["cache_path"][3] == 1       # NOT NULL
    indexes = list(conn.execute("PRAGMA index_list(source_file)"))
    assert any(r[2] for r in indexes), "cache_path UNIQUE index missing"


def test_r5_sink_chapter_id_and_playlist_source_id_columns_exist(tmp_path):
    conn = connect(tmp_path / "bb.sqlite")
    migrate(conn)
    item_cols = {r[1] for r in conn.execute("PRAGMA table_info(item)")}
    chapter_cols = {r[1] for r in conn.execute("PRAGMA table_info(chapter_record)")}
    assert "playlist_source_id" in item_cols
    assert "sink_chapter_id" in chapter_cols


def test_r2_assignment_has_staged_json_not_staged_path(tmp_path):
    conn = connect(tmp_path / "bb.sqlite")
    migrate(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(assignment)")}
    assert "staged_json" in cols
    assert "staged_path" not in cols
    assert "staged_titles_json" not in cols


def test_failed_migration_leaves_no_partial_schema(tmp_path, monkeypatch):
    """Review fix round 1 (Important 1): a migration script that fails partway
    (e.g. a duplicate CREATE TABLE) must leave the database with no tables
    from that script and no schema_version row for it — not some tables
    created and the rest missing, which a retry then can't recover from
    (no IF NOT EXISTS in the DDL).
    """
    import sqlite3

    import pytest

    db_path = tmp_path / "bb.sqlite"
    conn = connect(db_path)

    migrations_dir = tmp_path / "fake_migrations"
    migrations_dir.mkdir()
    bad_sql = (
        "BEGIN;\n"
        "CREATE TABLE ok_table (x TEXT);\n"
        "CREATE TABLE ok_table (x TEXT);\n"  # duplicate -> fails partway through the transaction
        "COMMIT;\n"
    )
    (migrations_dir / "0001_bad.sql").write_text(bad_sql)

    import boxbutler.store.db as db_mod

    class _FakeMigrationsDir:
        def __init__(self, path):
            self._path = path

        def iterdir(self):
            return list(self._path.iterdir())

    class _FakeFiles:
        def joinpath(self, name):
            assert name == "migrations"
            return _FakeMigrationsDir(migrations_dir)

    monkeypatch.setattr(db_mod.resources, "files", lambda _pkg: _FakeFiles())

    with pytest.raises(sqlite3.Error):
        db_mod.migrate(conn)

    assert conn.in_transaction is False
    names = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert "ok_table" not in names
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
    assert 1 not in applied
