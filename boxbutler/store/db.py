"""SQLite connection and migration runner (Task 6; spec §3.1).

`connect()` opens a connection in autocommit mode (`isolation_level=None`) with
foreign keys enforced and WAL journaling, row access by column name.
`migrate()` applies `migrations/NNNN_*.sql` files in order, recording each
applied version in `schema_version` so re-running is a no-op. A fresh
database and a re-migrated one end up identical.

Thread safety (Task 8 fix round 1, Important 2): FastAPI dispatches sync
path/dependency functions to a threadpool, so a `Store` built once (e.g. a
pytest fixture, or `create_app`) is legitimately called from a different
thread on every request — and a single admin can easily generate
*concurrent* requests (two tabs, an htmx poll landing mid-click, a browser
prefetch). A single `sqlite3.Connection` is not safe for concurrent use:
merely disabling Python's same-thread guard (`check_same_thread=False`)
stops the immediate `ProgrammingError` but leaves interleaved
`execute()`/`fetchone()` calls from different threads free to corrupt each
other's cursor state (confirmed: `tests/store/test_concurrency.py` raised
`InterfaceError`/`IndexError`/spurious `ValueError` under concurrent load
with only that flag set).

The fix here is a thread-local connection per thread, not a global lock:
`connect()` returns a `_ThreadLocalConnection` proxy that lazily opens one
real `sqlite3.Connection` per thread (via `threading.local`) to the same
database file, with the same pragmas as before. Each thread then always
talks to its own connection/cursor state, so no two threads ever share one
cursor. SQLite's own WAL-mode file locking (already enabled below) handles
serializing concurrent writers across those connections — `busy_timeout`
is set so a writer that loses that race waits instead of raising
`OperationalError: database is locked`.

`busy_timeout` is 15000ms, not SQLite's tiny default (0, i.e. fail
immediately) or the 5000ms this originally shipped with: a full-suite run
on the production host (a 16-core media server also running Plex/nzbget/the
*arr stack, sitting at load average ~13 throughout) hit `database is
locked` from `test_concurrent_writes_from_many_threads_do_not_raise_or_corrupt`
even though the same test passes reliably in isolation — 5s was
measurably insufficient under real contention on that hardware. This is a
single-admin app with small, brief writes (a handful of short INSERT/UPDATE
statements per request), so a longer wait costs nothing in the common case
where the lock clears in milliseconds, and only matters when the
alternative is an outright failed run or an HTTP 500. 15s is a ceiling, not
a target: a request that actually waits that long is still a bad user
experience, so this is a mitigation for host-load spikes, not a licence to
ignore lock contention if it starts happening routinely. This was chosen
over a
`threading.Lock` around `Store`/repo method bodies because it needed no
changes to `repos.py` (every repo only ever calls `self._conn.execute(...)`
/ `executescript(...)`, which the proxy transparently delegates to the
calling thread's own connection) and it lets concurrent reads proceed in
parallel rather than serializing everything through one Python lock.
"""
from __future__ import annotations

import sqlite3
import threading
from importlib import resources
from pathlib import Path


class _ThreadLocalConnection:
    """Proxies a `sqlite3.Connection`, giving each thread its own real
    connection to the same database file (see module docstring).

    Only attribute access is proxied (`__getattr__`) — every caller in this
    codebase only ever calls `.execute()` / `.executescript()` and reads
    `.in_transaction`, all of which sqlite3.Connection exposes as normal
    attributes, so a transparent proxy is enough; nothing here ever does
    `conn.<attr> = ...` from outside `_open()`.
    """

    def __init__(self, path: str):
        self._path = path
        self._local = threading.local()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        # See module docstring: 15s, not the 5s this originally shipped
        # with — measured insufficient under real contention on a loaded
        # 16-core host (load ~13).
        conn.execute("PRAGMA busy_timeout = 15000")
        return conn

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._open()
            self._local.conn = conn
        return conn

    def __getattr__(self, name: str):
        return getattr(self._conn(), name)


def connect(path: str | Path) -> sqlite3.Connection:
    # Returns a _ThreadLocalConnection, typed as sqlite3.Connection since it
    # proxies the same interface (see class docstring above).
    return _ThreadLocalConnection(str(path))  # type: ignore[return-value]


def migrate(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
    migrations_dir = resources.files("boxbutler.store").joinpath("migrations")
    files = sorted(
        (p for p in migrations_dir.iterdir() if p.name.endswith(".sql")),
        key=lambda p: p.name,
    )
    for f in files:
        version = int(f.name.split("_", 1)[0])
        if version in applied:
            continue
        # Review fix round 1 (Important 1): conn.execute("BEGIN") followed by
        # executescript() is broken — executescript() implicitly COMMITs any
        # pending transaction before it runs, so the explicit BEGIN never
        # covers the script body (confirmed: sqlite3.OperationalError "cannot
        # commit - no transaction is active" from the matching COMMIT). The
        # working fix is a literal `BEGIN;` inside the script text itself —
        # executescript() hands the whole script straight to SQLite, so a
        # BEGIN that's part of the script text starts a real transaction that
        # covers every following statement. Each migration file (by
        # convention, see 0001_initial.sql) opens with `BEGIN;` and has no
        # trailing COMMIT; migrate() appends the schema_version INSERT and
        # the COMMIT here, so recording the applied version is inside the
        # very same transaction as the DDL — a fresh database and a migrated
        # one end up identical, and a partial failure records nothing.
        script = f.read_text()
        script += (
            "\nINSERT INTO schema_version (version, applied_at) "
            f"VALUES ({version}, datetime('now'));\nCOMMIT;\n"
        )
        try:
            conn.executescript(script)
        except sqlite3.Error:
            # The failed statement never reached COMMIT, so the transaction
            # opened by the script's own BEGIN is still open on this
            # connection (uncommitted, invisible to any other connection).
            # Roll it back explicitly so this connection doesn't carry a
            # half-finished migration into later work, and so a retry starts
            # clean rather than failing again with "table already exists".
            conn.execute("ROLLBACK")
            raise
        applied.add(version)
    return max(applied, default=0)


class Store:
    def __init__(self, conn: sqlite3.Connection):
        from .repos import (
            AssignmentRepo,
            ChapterRecordRepo,
            ItemRepo,
            LibraryRepo,
            RenditionRepo,
            RunRepo,
            SettingsRepo,
            SourceFileRepo,
            SwapMarkerRepo,
        )

        self.conn = conn
        self.libraries = LibraryRepo(conn)
        self.items = ItemRepo(conn)
        self.assignments = AssignmentRepo(conn)
        self.renditions = RenditionRepo(conn)
        self.runs = RunRepo(conn)
        self.chapters = ChapterRecordRepo(conn)
        self.settings = SettingsRepo(conn)
        # Task 21: the cache index for fetched source files (migration 0002) —
        # the input side of the render step, as `renditions` is the output side.
        self.sources = SourceFileRepo(conn)
        # Final safety review C3: the write-ahead record of a clear that is
        # about to happen (migration 0003). Written before `sink.clear()`,
        # deleted on COMMIT; a surviving row means a process died mid-swap.
        self.swap_markers = SwapMarkerRepo(conn)

    @classmethod
    def open(cls, path: str | Path) -> "Store":
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = connect(path)
        migrate(conn)
        return cls(conn)
