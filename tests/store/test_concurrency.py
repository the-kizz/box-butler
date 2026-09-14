"""Concurrent-access safety for a single Store (Task 8 fix round 1, Important 2).

`check_same_thread=False` (added when the web app's threadpool-dispatched
routes first touched the store) only disables sqlite3's same-thread guard —
it does not make one `sqlite3.Connection` safe for *concurrent* use from
several threads at once. A single admin can easily generate concurrent
requests (two tabs, an htmx poll landing mid-click, a browser prefetch), so
this asserts that doesn't raise or corrupt state.

Busy-timeout note (found in a full-suite run on the production host, a
16-core media server at load average ~13): the first version of this test
asserted zero `OperationalError: database is locked` across 80 unsynchronised
writes, full stop. That is not actually the contract we need — SQLite's
`busy_timeout` is a race against an arbitrarily loaded host, and no finite
timeout can promise it never loses that race on someone else's hardware
under someone else's load. Bumping `busy_timeout` from 5000ms to 15000ms
(see `boxbutler/store/db.py`) fixes the *measured* failure, but a test that
still hard-fails on a single lock error is one bad host-load day away from
being flaky again for the same underlying reason.

What this test asserts instead: a transient "database is locked" is not a
correctness bug by itself, so a single write retries past one and keeps
going — proving contention *resolves* rather than wedging or corrupting
anything. What it still refuses to tolerate, with no retry and no excuse:
any other exception shape (interleaved-cursor corruption looked like
`InterfaceError`/`IndexError`/spurious `ValueError` before the thread-local
connection fix — see the module this fixture exercises), a write that never
lands, a duplicate id, or a duplicate/lost name. Those are the actual
correctness properties; "SQLite never once had to wait for a lock" was
never one of them.
"""
import sqlite3
import threading

from boxbutler.store.db import Store

_LOCK_RETRY_ATTEMPTS = 5


def _create_with_retry(store: Store, name: str) -> None:
    """Create a library, retrying past a transient 'database is locked'.

    `busy_timeout` already makes SQLite itself wait (up to 15s) before
    raising this, so a retry here is only ever reached after that full
    internal wait already elapsed once — i.e. this is for the rare case of
    an even more heavily loaded host, not a substitute for the pragma. Any
    exception other than "database is locked" propagates immediately: it is
    not lock contention and retrying would hide a real bug.
    """
    for attempt in range(_LOCK_RETRY_ATTEMPTS):
        try:
            store.libraries.create(name)
            return
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc) or attempt == _LOCK_RETRY_ATTEMPTS - 1:
                raise


def test_concurrent_writes_from_many_threads_do_not_raise_or_corrupt(tmp_path):
    store = Store.open(tmp_path / "bb.sqlite")

    n_threads = 8
    writes_per_thread = 10
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def worker(t: int) -> None:
        try:
            for i in range(writes_per_thread):
                _create_with_retry(store, f"lib-{t}-{i}")
        except BaseException as exc:  # noqa: BLE001 - we want to see anything, not just sqlite3.Error
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=30)

    assert not errors, f"concurrent writes raised (non-lock, or lock that never resolved): {errors!r}"

    libs = store.libraries.list()
    names = {lib.name for lib in libs}
    ids = {lib.id for lib in libs}
    assert len(libs) == n_threads * writes_per_thread
    assert len(names) == n_threads * writes_per_thread  # every write landed, none lost or overwritten
    assert len(ids) == n_threads * writes_per_thread  # no id collisions/corruption


def test_concurrent_reads_and_writes_do_not_raise(tmp_path):
    store = Store.open(tmp_path / "bb.sqlite")
    store.libraries.create("seed")

    errors: list[BaseException] = []
    errors_lock = threading.Lock()
    stop = threading.Event()

    def reader() -> None:
        try:
            while not stop.is_set():
                store.libraries.list()
        except BaseException as exc:  # noqa: BLE001
            with errors_lock:
                errors.append(exc)

    def writer(t: int) -> None:
        try:
            for i in range(20):
                _create_with_retry(store, f"w-{t}-{i}")
        except BaseException as exc:  # noqa: BLE001
            with errors_lock:
                errors.append(exc)

    readers = [threading.Thread(target=reader) for _ in range(4)]
    writers = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
    for th in readers + writers:
        th.start()
    for th in writers:
        th.join(timeout=30)
    stop.set()
    for th in readers:
        th.join(timeout=30)

    assert not errors, f"concurrent read/write raised: {errors!r}"
