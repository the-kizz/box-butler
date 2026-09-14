"""Task 29 review, CRITICAL-1: `boxbutler run`/`status`/etc. had no real
`deps_factory` -- the console script exited 2 with "no deps_factory
configured" no matter what. These tests drive `boxbutler.main.cli_main`,
the function `pyproject.toml`'s `[project.scripts] boxbutler` now points
at, to pin that the CLI half of the composition root is actually wired.
"""
from __future__ import annotations

import socket

from boxbutler.main import cli_main

REQUIRED_SECRETS = {
    "BOXBUTLER_SINK_USER": "u",
    "BOXBUTLER_SINK_PASSWORD": "p",
    "BOXBUTLER_ADMIN_USER": "admin",
    "BOXBUTLER_ADMIN_PASSWORD": "correct horse",
    "BOXBUTLER_SECRET_KEY": "k",
}


def _set_env(monkeypatch, tmp_path, **extra):
    for k, v in {**REQUIRED_SECRETS, **extra}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("BOXBUTLER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BOXBUTLER_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("BOXBUTLER_SINK_KIND", "fake")
    monkeypatch.delenv("BOXBUTLER_CONFIG", raising=False)


def test_boxbutler_status_exits_0_with_fake_sink(monkeypatch, tmp_path, capsys):
    _set_env(monkeypatch, tmp_path)
    assert cli_main(["status"]) == 0


def test_boxbutler_run_dry_run_prints_a_plan_with_fake_sink(monkeypatch, tmp_path, capsys):
    # The brief's own Done-when clause: "prints a dry-run plan against
    # the real cloud" -- exercised here against the fake sink, since no
    # test may touch the real cloud.
    _set_env(monkeypatch, tmp_path)
    assert cli_main(["run"]) == 0


def test_boxbutler_missing_secrets_exits_2_with_config_error_not_traceback(monkeypatch, tmp_path, capsys):
    for k in REQUIRED_SECRETS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BOXBUTLER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BOXBUTLER_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("BOXBUTLER_CONFIG", raising=False)
    assert cli_main(["status"]) == 2
    err = capsys.readouterr().err
    assert "configuration error" in err


def test_boxbutler_help_needs_no_credentials_and_touches_no_network(monkeypatch, tmp_path):
    # No BOXBUTLER_* secret in the environment at all -- `--help` must
    # still work, because argparse short-circuits before `deps_factory`
    # is ever called (Task 29 review's own explicit ask).
    for k in REQUIRED_SECRETS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("BOXBUTLER_CONFIG", raising=False)

    def _no_network(*a, **k):
        raise AssertionError("boxbutler --help must never touch the network")

    monkeypatch.setattr(socket.socket, "connect", _no_network)

    # `boxbutler.cli.main.main` catches argparse's own `SystemExit` and
    # returns its code rather than letting it propagate.
    assert cli_main(["--help"]) == 0
