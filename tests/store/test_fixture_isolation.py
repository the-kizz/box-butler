"""Proves the `store` fixture's template-copy optimisation does not leak
state between tests (test-suite-speed task).

The `store` fixture (tests/conftest.py) now builds each test's database by
copying a shared, pre-migrated template file rather than migrating from
scratch. That's only safe if every test still gets an independent copy: if
the copy ever aliased the template (or another test's file), a row written
in one test would leak into the next. These two tests run in file order and
would fail if that were happening.
"""


def test_a_writes_a_library(store):
    store.libraries.create("leaked-if-fixture-is-shared")
    assert [lib.name for lib in store.libraries.list()] == ["leaked-if-fixture-is-shared"]


def test_b_does_not_see_previous_tests_data(store):
    # A fresh `store` fixture instance must start from the same empty
    # (but migrated) state as every other test, regardless of what
    # test_a_writes_a_library above did to its own copy.
    assert store.libraries.list() == []
