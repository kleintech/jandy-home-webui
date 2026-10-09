"""Owner gate for the Advanced controls (the LAN has no other auth). Each test names
the bug it catches."""

import logging

import pytest
from fastapi.testclient import TestClient

from app.backends.mock import MockBackend
from app.main import create_app
from app.owner_auth import COOKIE, TOKEN_TTL, OwnerGate
from app.service import PoolService

PIN = "48151623"


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def make(gate, base_url="http://testserver"):
    backend = MockBackend()
    c = TestClient(create_app(PoolService(backend, poll_seconds=3600), gate), base_url=base_url)
    c.__enter__()
    return c, backend


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def gate(clock):
    return OwnerGate(PIN, b"secret", clock=clock, monotonic=clock)


@pytest.fixture
def app(gate):
    c, backend = make(gate)
    yield c, backend
    c.__exit__(None, None, None)


def writes(backend):
    return [c for c in backend.calls if c[0] != "salt_config"]


def unlock(c, pin=PIN):
    return c.post("/api/advanced/unlock", json={"pin": pin})


# ---- gate off ----------------------------------------------------------------------

@pytest.mark.parametrize("pin", [None, "", "123", "1234567890123", "12ab", " 1234x"])
def test_without_a_valid_owner_pin_advanced_is_off(pin):
    # Bug: Advanced reachable (or unlockable) with no PIN configured, or with a
    # malformed one that a typo turned into something guessable.
    c, backend = make(OwnerGate(pin, b"s"))
    try:
        for r in (c.get("/api/advanced"),
                  c.post("/api/advanced/switch", json={"key": "aux_4", "on": True}),
                  unlock(c, pin or "1234")):
            assert r.status_code == 403
            assert r.json() == {"detail": "Advanced controls are off: set OWNER_PIN"}
        assert writes(backend) == []
        assert c.get("/api/state").json()["advanced"] == {"enabled": False, "unlocked": False}
    finally:
        c.__exit__(None, None, None)


def test_owner_pin_from_env(monkeypatch):
    # Bug: OWNER_PIN/OWNER_SECRET not read, so the section can never be enabled, or
    # a cookie signed before a restart is rejected even with OWNER_SECRET set.
    monkeypatch.setenv("OWNER_PIN", "246802")
    monkeypatch.setenv("OWNER_SECRET", "a" * 32)
    a, b = OwnerGate.from_env(), OwnerGate.from_env()
    assert a.enabled and a.check_pin("246802") and not a.check_pin("246803")
    assert b.verify(a.issue())
    monkeypatch.delenv("OWNER_SECRET")
    assert not OwnerGate.from_env().verify(OwnerGate.from_env().issue())


# ---- locked ------------------------------------------------------------------------

def test_locked_endpoints_need_a_session(app):
    # Bug: an Advanced route registered without the owner dependency.
    c, backend = app
    for method, path, body in [
        ("get", "/api/advanced", None),
        ("post", "/api/advanced/switch", {"key": "aux_4", "on": True}),
        ("post", "/api/advanced/heatpump", {"on": False}),
        ("post", "/api/advanced/setpoints", {"spa": 104}),
        ("post", "/api/advanced/light", {"key": "aux_1", "on": True}),
        ("post", "/api/advanced/vsp", {"key": "vsp_pump_1", "on": True}),
        ("post", "/api/advanced/salt", {"pool_pct": 50}),
        ("post", "/api/advanced/salt/boost", {"action": "stop"}),
    ]:
        r = c.request(method, path, json=body)
        assert r.status_code == 401, (path, r.status_code)
    assert backend.calls == []
    assert c.get("/api/state").json()["advanced"] == {"enabled": True, "unlocked": False}


def test_every_advanced_route_is_gated(app):
    # Bug: a route added later under /api/advanced without the owner dependency.
    c, _ = app
    open_paths = {"/api/advanced/unlock", "/api/advanced/lock"}
    for route in c.app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/api/advanced") and path not in open_paths:
            for m in route.methods:
                assert c.request(m, path, json={}).status_code == 401, path


# ---- unlock ------------------------------------------------------------------------

def test_good_pin_sets_a_strict_httponly_cookie_that_works(app):
    # Bug: cookie readable by scripts, sent cross-site, or not long-lived.
    c, backend = app
    r = unlock(c)
    assert r.status_code == 200 and r.json() == {"unlocked": True}
    sc = r.headers["set-cookie"].lower()
    assert "httponly" in sc and "samesite=strict" in sc and f"max-age={TOKEN_TTL}" in sc
    assert "secure" not in sc  # plain http: a Secure cookie would never come back
    assert c.get("/api/advanced").status_code == 200
    assert c.get("/api/state").json()["advanced"] == {"enabled": True, "unlocked": True}
    assert c.post("/api/advanced/switch", json={"key": "aux_4", "on": True}).status_code == 200
    assert ("adv_set_switch", "aux_4", True) in backend.calls


def test_cookie_is_secure_behind_an_https_proxy(gate):
    # Bug: the session cookie sent over plain http when the app sits behind TLS
    # termination (Traefik/Cloudflare say so in X-Forwarded-Proto).
    c, _ = make(gate)
    try:
        r = c.post("/api/advanced/unlock", json={"pin": PIN}, headers={"X-Forwarded-Proto": "https"})
        assert "secure" in r.headers["set-cookie"].lower()
    finally:
        c.__exit__(None, None, None)


def test_wrong_pin_is_401_and_logged_without_the_pin(app, caplog):
    # Bug: wrong PIN accepted, or the attempted PIN written to the log.
    c, _ = app
    with caplog.at_level(logging.INFO):
        r = unlock(c, "99998888")
    assert r.status_code == 401 and "set-cookie" not in r.headers
    assert c.get("/api/advanced").status_code == 401
    assert "unlock failed" in caplog.text and "99998888" not in caplog.text


def test_pin_prefix_or_padding_is_not_accepted(app):
    # Bug: a comparison that accepts a prefix of the PIN or ignores whitespace.
    c, _ = app
    for pin in (PIN[:-1], PIN + "0", f" {PIN}", f"{PIN}\n"):
        assert unlock(c, pin).status_code == 401


def test_unlock_is_rate_limited_per_client_even_for_the_right_pin(app, clock):
    # Bug: unlimited guessing (a 4-digit PIN falls in minutes), or the right PIN
    # still checked during lockout (which keeps confirming guesses).
    c, _ = app
    for _ in range(5):
        assert unlock(c, "00000000").status_code == 401
    r = unlock(c)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    assert "set-cookie" not in r.headers
    clock.t += 301
    assert unlock(c).status_code == 200


def test_spoofed_forwarded_for_does_not_reset_the_limit(app):
    # Bug: X-Forwarded-For trusted from any peer, so a new fake address per attempt
    # sidesteps the per-client limit.
    c, _ = app
    for _ in range(5):
        unlock(c, "00000000")
    r = c.post("/api/advanced/unlock", json={"pin": PIN}, headers={"X-Forwarded-For": "10.9.9.9"})
    assert r.status_code == 429


def test_global_limit_across_clients(clock):
    # Bug: many source addresses (via a trusted proxy) share the guessing with no
    # overall cap.
    gate = OwnerGate(PIN, b"s", clock=clock, monotonic=clock)
    # TestClient always connects from one peer; stand in for distinct client
    # addresses (client_id's own parsing is tested separately below).
    gate.client_id = lambda request: request.headers.get("x-client", "x")
    c, _ = make(gate)
    try:
        for i in range(10):
            r = c.post("/api/advanced/unlock", json={"pin": "00000000"}, headers={"x-client": f"c{i}"})
            assert r.status_code == 401
        r = c.post("/api/advanced/unlock", json={"pin": PIN}, headers={"x-client": "fresh"})
        assert r.status_code == 429
    finally:
        c.__exit__(None, None, None)


def test_forwarded_for_used_only_from_trusted_proxies():
    # Bug: per-client limit keyed on the proxy (all users share it) when a trusted
    # proxy is configured, or on a header any client can forge when one isn't.
    from starlette.requests import Request

    def req(peer, xff):
        return Request({"type": "http", "client": (peer, 1), "headers": [(b"x-forwarded-for", xff.encode())]})

    g = OwnerGate(PIN, b"s", trusted_proxies="10.42.0.0/16")
    assert g.client_id(req("10.42.0.7", "203.0.113.5, 10.42.0.9")) == "203.0.113.5"
    assert g.client_id(req("192.168.1.20", "203.0.113.5")) == "192.168.1.20"
    assert g.client_id(req("10.42.0.7", "not-an-ip")) == "10.42.0.7"
    assert OwnerGate(PIN, b"s").client_id(req("10.42.0.7", "203.0.113.5")) == "10.42.0.7"


# ---- cookies -----------------------------------------------------------------------

def _set(c, value):
    c.cookies.clear()
    c.cookies.set(COOKIE, value)


def test_tampered_cookie_rejected(app, gate):
    # Bug: signature not checked, or only a prefix of it, so an edited expiry or a
    # forged token passes.
    c, _ = app
    unlock(c)
    good = c.cookies.get(COOKIE)
    v, exp, nonce, sig = good.split(".")
    for bad in [f"{v}.{int(exp) + 999999}.{nonce}.{sig}",
                f"{v}.{exp}.{nonce}.{sig[:-1]}{'0' if sig[-1] != '0' else '1'}",
                f"{v}.{exp}.{nonce}.", f"{v}.{exp}.{nonce}", "garbage", f"v2.{exp}.{nonce}.{sig}",
                good + "." + sig]:
        _set(c, bad)
        assert c.get("/api/advanced").status_code == 401, bad
    _set(c, good)
    assert c.get("/api/advanced").status_code == 200


def test_non_ascii_cookie_is_rejected_not_a_crash(gate):
    # Bug: hmac.compare_digest raises TypeError on non-ASCII text, so a crafted
    # cookie turned every Advanced request (and /api/state) into a 500.
    assert gate.verify("v1.9999999999.\u00e9\u00e9.abc") is False
    assert gate.verify("v1.9999999999.ab." + "\u00e9" * 64) is False


def test_expired_cookie_rejected(app, clock):
    # Bug: tokens that never expire.
    c, _ = app
    unlock(c)
    clock.t += TOKEN_TTL - 60
    assert c.get("/api/advanced").status_code == 200
    clock.t += 120
    assert c.get("/api/advanced").status_code == 401


def test_cookie_from_another_secret_or_old_pin_rejected(app, clock):
    # Bug: tokens not bound to the server secret, or still valid after the owner
    # changes the PIN.
    c, _ = app
    _set(c, OwnerGate(PIN, b"other-secret", clock=clock).issue())
    assert c.get("/api/advanced").status_code == 401
    _set(c, OwnerGate("11112222", b"secret", clock=clock).issue())
    assert c.get("/api/advanced").status_code == 401
    _set(c, OwnerGate(PIN, b"secret", clock=clock).issue())
    assert c.get("/api/advanced").status_code == 200


def test_lock_clears_the_session(app):
    # Bug: "Lock" leaves the browser unlocked.
    c, _ = app
    unlock(c)
    r = c.post("/api/advanced/lock", json={})
    assert r.status_code == 200 and r.json() == {"unlocked": False}
    assert COOKIE in r.headers["set-cookie"] and "max-age=0" in r.headers["set-cookie"].lower()
    assert c.get("/api/advanced").status_code == 401
    assert c.get("/api/state").json()["advanced"]["unlocked"] is False


def test_advanced_responses_are_not_cached(app):
    # Bug: an owner view cached by a proxy/browser and shown after locking.
    c, _ = app
    unlock(c)
    assert c.get("/api/advanced").headers["cache-control"] == "no-store"


# ---- mock backend end to end -------------------------------------------------------

def test_mock_owner_round_trip(app):
    # Bug: the mock (used for UI work) missing an Advanced method, so the UI can't
    # be built against it.
    c, backend = app
    unlock(c)
    v = c.get("/api/advanced").json()
    assert {g["id"] for g in v["switches"]} == {"pumps", "aux", "scenes"}
    assert v["salt"]["config"]["pool_pct"] == 25 and v["vsp"] == []
    for path, body in [("/api/advanced/heatpump", {"on": False, "mode": "chill"}),
                       ("/api/advanced/setpoints", {"spa": 104, "pool_heat": 80, "pool_chill": 81}),
                       ("/api/advanced/light", {"key": "aux_1", "effect": "Violet"}),
                       ("/api/advanced/salt", {"spa_pct": 30}),
                       ("/api/advanced/salt/boost", {"action": "start", "hours": 3, "mode": "pool"})]:
        r = c.post(path, json=body)
        assert r.status_code == 200, (path, r.text)
    v = r.json()
    assert v["heatpump"]["on"] is False and v["heatpump"]["mode"] == "chill"
    assert v["setpoints"]["spa"]["value"] == 104
    assert v["salt"]["config"]["spa_pct"] == 30 and v["salt"]["config"]["boost"]["status"] == "on"
    assert next(x for x in v["lights"] if x["key"] == "aux_1")["effect"] == "Violet"


# ---- PIN and secret strength ---------------------------------------------------------

@pytest.mark.parametrize("pin", ["1234", "12345", "١٢٣٤٥٦", "１２３４５６", "1234567890123"])
def test_short_or_non_ascii_pins_turn_the_gate_off(pin, caplog):
    # Bug: a 4-5 digit PIN (10^4 guesses) or Unicode digits (\d matches Arabic-Indic
    # and full-width digits, which a phone keypad can't type) accepted as the PIN.
    with caplog.at_level(logging.ERROR):
        gate = OwnerGate(pin, b"s")
    assert not gate.enabled
    assert "OWNER_PIN must be 6-12 digits" in caplog.text


def test_six_digit_pin_is_accepted():
    # Bug: the length floor raised past what the README documents (6).
    assert OwnerGate("246802", b"s").enabled


def test_short_owner_secret_is_ignored(monkeypatch, caplog):
    # Bug: OWNER_SECRET=abc used as the cookie signing key; a guessable key lets
    # anyone forge an owner cookie offline.
    monkeypatch.setenv("OWNER_PIN", "246802")
    monkeypatch.setenv("OWNER_SECRET", "a" * 31)
    with caplog.at_level(logging.WARNING):
        a, b = OwnerGate.from_env(), OwnerGate.from_env()
    assert "OWNER_SECRET is shorter than 32" in caplog.text
    assert not b.verify(a.issue())  # random per process, not the weak secret
    assert not OwnerGate("246802", b"a" * 31).verify(a.issue())


# ---- guest view of the settings -------------------------------------------------------

def _weather_client(gate):
    from app.config_store import ConfigStore, Limits
    store = ConfigStore(None, Limits(), weather={"zip": "12345", "country": "us", "lat": 40.1,
                                                 "lon": -75.2, "label": "Home"})
    c = TestClient(create_app(PoolService(MockBackend(), poll_seconds=3600, config=store), gate))
    c.__enter__()
    return c


def test_guests_get_only_the_weather_label(gate):
    # Bug: GET /api/config (public) handing every guest the house's zip code and
    # coordinates.
    c = _weather_client(gate)
    try:
        assert c.get("/api/config").json()["weather"] == {"label": "Home"}
        unlock(c)
        w = c.get("/api/config").json()["weather"]
        assert w["zip"] == "12345" and w["lat"] == 40.1 and w["lon"] == -75.2
    finally:
        c.__exit__(None, None, None)


def test_owner_save_and_reset_answers_carry_the_full_weather_location(gate):
    # Bug: PUT /api/config or POST /api/config/reset answering with the guest
    # (redacted) copy; the page adopts that answer as its draft, so the next Save
    # would drop the zip and coordinates.
    c = _weather_client(gate)
    try:
        unlock(c)
        doc = c.get("/api/config").json()
        saved = c.put("/api/config", json=doc).json()
        assert saved["weather"]["zip"] == "12345" and saved["weather"]["lat"] == 40.1
        assert "redacted" not in saved
        reset = c.post("/api/config/reset", json={}).json()
        assert reset["weather"]["zip"] == "12345" and reset["weather"]["lon"] == -75.2
        assert "redacted" not in reset
    finally:
        c.__exit__(None, None, None)


def test_guest_copy_of_the_settings_cant_be_saved_back(gate):
    # Bug: a page that loaded the redacted (guest) settings, then unlocked and saved,
    # silently wiped the weather location.
    c = _weather_client(gate)
    try:
        guest_doc = c.get("/api/config").json()
        unlock(c)
        r = c.put("/api/config", json=guest_doc)
        assert r.status_code == 409
        assert c.get("/api/config").json()["weather"]["zip"] == "12345"
    finally:
        c.__exit__(None, None, None)


# ---- cross-site requests ----------------------------------------------------------------

def _owner_writes(c):
    doc = c.get("/api/config").json()
    return [("post", "/api/advanced/unlock", {"pin": PIN}),
            ("post", "/api/advanced/lock", {}),
            ("post", "/api/advanced/switch", {"key": "aux_4", "on": True}),
            ("put", "/api/config", doc),
            ("post", "/api/config/reset", {})]


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
    {"Origin": "http://evil.example"},
    {"Origin": "https://testserver"},       # same host, other scheme
    {"Origin": "http://testserver:8080"},   # same host, other port
    {"Origin": "null"},
])
def test_cross_site_owner_writes_are_refused(app, headers):
    # Bug: a page on another site (or a sibling subdomain) driving owner writes from
    # an unlocked owner's browser: unlock/lock (login CSRF), switching devices,
    # rewriting or resetting the settings.
    c, backend = app
    unlock(c)
    before = c.get("/api/config").json()
    for method, path, body in _owner_writes(c):
        r = c.request(method, path, json=body, headers=headers)
        assert r.status_code == 403, (path, headers, r.status_code)
    assert writes(backend) == []
    assert c.get("/api/config").json() == before
    assert c.get("/api/advanced").status_code == 200  # still unlocked: lock was refused too


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "same-origin", "Origin": "http://testserver"},
    {"Sec-Fetch-Site": "none"},
    {"Origin": "https://pool.example", "Host": "pool.example", "X-Forwarded-Proto": "https"},
    {"Origin": "https://pool.example", "X-Forwarded-Host": "pool.example", "X-Forwarded-Proto": "https"},
])
def test_same_origin_owner_writes_still_work(app, headers):
    # Bug: the CSRF check refusing the app's own page, directly or behind Traefik
    # (TLS terminated: X-Forwarded-Proto https, Host passed through).
    c, backend = app
    unlock(c)  # plain http here: a Secure cookie (X-Forwarded-Proto https) wouldn't come back
    r = c.post("/api/advanced/switch", json={"key": "aux_4", "on": True}, headers=headers)
    assert r.status_code == 200, r.text
    assert c.post("/api/config/reset", json={}, headers=headers).status_code == 200
    assert c.post("/api/advanced/lock", json={}, headers=headers).status_code == 200
    assert c.post("/api/advanced/unlock", json={"pin": PIN}, headers=headers).status_code == 200


@pytest.mark.parametrize("ctype", [None, "text/plain", "application/x-www-form-urlencoded",
                                   "multipart/form-data; boundary=x"])
def test_owner_writes_must_be_json(app, ctype):
    # Bug: a cross-site <form> (which can only send these types, with no preflight)
    # posting to reset, lock or unlock, which take no JSON fields worth checking.
    c, backend = app
    unlock(c)
    h = {} if ctype is None else {"content-type": ctype}
    for path, body in [("/api/config/reset", ""), ("/api/advanced/lock", ""),
                       ("/api/advanced/unlock", f'{{"pin": "{PIN}"}}'),
                       ("/api/advanced/switch", '{"key": "aux_4", "on": true}')]:
        r = c.post(path, content=body, headers=h)
        assert r.status_code == 415, (path, ctype, r.status_code)
    assert writes(backend) == []
    assert c.get("/api/advanced").status_code == 200


def test_openapi_schema_is_not_served(app):
    # Bug: /openapi.json listing every owner route and body shape to anyone.
    c, _ = app
    assert c.get("/openapi.json").status_code == 404


# ---- container -------------------------------------------------------------------------

def test_container_does_not_trust_forwarded_headers_from_every_peer():
    # Bug: uvicorn --forwarded-allow-ips='*' lets any LAN client set X-Forwarded-For,
    # making request.client.host (the PIN rate-limit key, the logged address)
    # attacker-controlled. uvicorn's default trusts only 127.0.0.1 (or
    # $FORWARDED_ALLOW_IPS); OWNER_TRUSTED_PROXIES handles real proxies itself.
    from pathlib import Path
    dockerfile = (Path(__file__).parent.parent / "Dockerfile").read_text()
    cmd = next(line for line in dockerfile.splitlines() if line.startswith("CMD"))
    assert "forwarded-allow-ips" not in cmd
    assert "uvicorn app.main:app" in cmd
