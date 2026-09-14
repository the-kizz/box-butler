"""Web auth tests (Task 8; spec §5 auth, §10.18 web).

No test touches the network, a real tonie, or ffmpeg — this exercises the
FastAPI app in-process via TestClient against a temp-file store and the
placeholder FakeSink/FakeRunner from conftest.py.
"""
import pytest

PROTECTED = ["/", "/libraries", "/history", "/runs", "/settings", "/api/export"]


@pytest.mark.parametrize("path", PROTECTED)
def test_every_route_requires_login(client, path):
    r = client.get(path)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_login_sets_httponly_samesite_cookie(client):
    r = client.post("/login", data={"username": "admin", "password": "correct horse"})
    c = r.headers["set-cookie"].lower()
    assert "httponly" in c and "samesite=lax" in c and "secure" not in c  # secure only over TLS (settings)


def test_wrong_password_rejected(client):
    r = client.post("/login", data={"username": "admin", "password": "nope"})
    assert r.status_code == 401


def test_login_rate_limited(client):
    for _ in range(5):
        client.post("/login", data={"username": "admin", "password": "nope"})
    r = client.post("/login", data={"username": "admin", "password": "correct horse"})
    assert r.status_code == 429


def test_successful_logins_never_count_towards_the_limit(client):
    # Final review, Major 2: 8 correct logins in a row used to produce
    # [303]*5 then 429s — a correct password got refused. Only failed
    # attempts may count now.
    for _ in range(8):
        r = client.post("/login", data={"username": "admin", "password": "correct horse"})
        assert r.status_code == 303


def test_successful_login_clears_the_failure_count(client):
    # A run of wrong passwords followed by the correct one must not leave
    # a lingering count that later combines with a fresh mistake to lock
    # the operator out earlier than a full fresh window's worth.
    for _ in range(4):
        client.post("/login", data={"username": "admin", "password": "nope"})
    ok = client.post("/login", data={"username": "admin", "password": "correct horse"})
    assert ok.status_code == 303

    # The bucket was reset by the success above, so this single wrong
    # guess is only the first failure of a fresh window, not the 5th.
    r = client.post("/login", data={"username": "admin", "password": "nope"})
    assert r.status_code == 401


def test_healthz_is_public(client):
    assert client.get("/healthz").status_code == 200


def test_password_change_requires_current(auth):
    r = auth.post("/settings/password", data={"current": "wrong", "new": "x" * 12, "confirm": "x" * 12})
    assert r.status_code == 400
    r = auth.post("/settings/password", data={"current": "correct horse", "new": "x" * 12, "confirm": "x" * 12})
    assert r.status_code == 303
    auth.post("/logout")
    assert auth.post("/login", data={"username": "admin", "password": "x" * 12}).status_code == 303


def test_no_trusted_header_bypass(client):
    r = client.get("/", headers={"Remote-User": "admin", "X-Forwarded-User": "admin"})
    assert r.status_code == 303  # §5: never rely on a proxy header


def test_login_pays_argon2_cost_for_unknown_and_known_usernames_alike(client, monkeypatch):
    """Regression for the timing oracle: Python's `and` short-circuits, so a
    naive `username == admin_user and verify_password(...)` only runs the
    ~200ms argon2 verify when the username already matches — an attacker can
    enumerate valid usernames from response latency alone, even though the
    status code and message are identical (spec: login must never distinguish
    "no such user" from "wrong password", and latency is a channel too).

    Asserted as a call-count invariant (not wall-clock time) so it's
    deterministic under CI/load: verify_password must run exactly once for
    an unknown username and exactly once for a known one with a wrong
    password — never skipped for the unknown-user case.
    """
    import boxbutler.web.auth as auth_mod

    calls = []
    real_verify = auth_mod.verify_password

    def spy(hash_, password):
        calls.append(hash_)
        return real_verify(hash_, password)

    monkeypatch.setattr(auth_mod, "verify_password", spy)

    client.post("/login", data={"username": "no-such-user", "password": "whatever"})
    assert len(calls) == 1, "verify_password must be called even for an unknown username"

    client.post("/login", data={"username": "admin", "password": "wrong"})
    assert len(calls) == 2, "verify_password must be called for a known username with a wrong password"

    # The unknown-user call must not have been run against the real admin
    # hash (that would leak whether the hash comparison, not just calling
    # the function, was skipped) — it should have used some other value.
    assert calls[0] != calls[1]
