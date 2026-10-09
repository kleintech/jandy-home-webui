"""Runtime settings (app/config_store.py, PUT/GET /api/config, POST /api/toggle):
defaults that reproduce the env behaviour, atomic persistence, validation, the owner
gate, and live application to limits, toggles, the Hot Tub sequences and the weather
location. Each test names the bug it catches."""

import asyncio
import copy
import json
import os

import pytest
from fastapi.testclient import TestClient

from app import config_store, weather
from app.backends.mock import MockBackend
from app.config_store import ConfigStore, Limits
from app.main import create_app
from app.owner_auth import OwnerGate
from app.service import PoolService
from tests.test_advanced_iaqualink import AdvCloud, wire
from tests.test_iaqualink_backend import make_backend

PIN = "13572468"


def unlocked(c):
    assert c.post("/api/advanced/unlock", json={"pin": PIN}).status_code == 200
    return c


@pytest.fixture
def path(tmp_path):
    return tmp_path / "data" / "config.json"


@pytest.fixture
def backend():
    return MockBackend()


@pytest.fixture
def client(backend, path):
    svc = PoolService(backend, poll_seconds=3600, config=ConfigStore(path, Limits(), weather={}))
    with TestClient(create_app(svc, OwnerGate(PIN, b"secret"))) as c:
        yield unlocked(c)


def doc(c):
    return c.get("/api/config").json()


def put(c, d):
    return c.put("/api/config", json=d)


def switch_calls(backend):
    return [x[1:] for x in backend.calls if x[0] == "set_switch"]


# ---- defaults ------------------------------------------------------------------------

def test_defaults_reproduce_the_env_limits_and_spa_cap(monkeypatch):
    # Bug: a fresh install (no saved file) ignoring SPA_MAX & co. and running on the
    # built-in limits, or a Hot Tub On whose spa cap no longer follows SPA_MAX.
    monkeypatch.setenv("JANDY_BACKEND", "mock")
    monkeypatch.setenv("SPA_MAX", "100")
    monkeypatch.setenv("POOL_HEAT_MIN", "83")
    with TestClient(create_app()) as c:
        d = doc(c)
        assert d["version"] == 0
        assert d["limits"] == {"spa_min": 80, "spa_max": 100, "pool_heat_min": 83, "pool_heat_max": 92,
                               "pool_chill_max": 92, "min_spread": 5}
        assert d["hot_tub_on"] == [
            {"action": "switch", "key": "pool_pump", "on": True},
            {"action": "switch", "key": "spa_pump", "on": True},
            {"action": "spa_setpoint_max", "value": 100},
            {"action": "switch", "key": "spa_heater", "on": True},
        ]
        assert d["hot_tub_off"] == [{"action": "switch", "key": "spa_heater", "on": False},
                                    {"action": "switch", "key": "spa_pump", "on": False}]
        assert c.post("/api/spa/setpoint", json={"set_temp": 101}).status_code == 409


def test_default_toggles_come_from_the_device_map(backend, client):
    # Bug: default guest toggles not driving the JANDY_*_DEVICE devices, or losing the
    # spillover/water features interlock.
    d = doc(client)
    assert [(t["id"], t["key"], t["label"], t["modes"], t["conflicts"]) for t in d["guest_toggles"]] == [
        ("bubbles", "aux_2", "Bubbles", ["spa"], []),
        ("spillover", "onetouch_1", "Spillover", ["pool"], ["water_features"]),
        ("water_features", "aux_3", "Water Features", ["pool"], ["spillover"]),
    ]


def test_default_toggles_follow_the_panel_labels_through_the_real_backend():
    # Bug: defaults built from the DeviceMap's labels ("Aux V1") instead of the keys
    # they resolve to, or a toggle offered for a device the panel doesn't have
    # (the fixture panel has no "Spillover").
    cloud = AdvCloud()
    backend = asyncio.run(make_backend(cloud))
    with TestClient(create_app(PoolService(backend, poll_seconds=3600))) as c:
        ids = {t["id"]: t["key"] for t in doc(c)["guest_toggles"]}
    assert ids == {"bubbles": "aux_2", "water_features": "aux_4"}


def test_saving_the_defaults_back_changes_nothing_the_guests_see(backend, client):
    # Bug: the defaults document failing its own validation (the settings page could
    # never save), or saving it changing what Hot Tub On sends.
    before = client.get("/api/state").json()
    r = put(client, doc(client))
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 1
    after = client.get("/api/state").json()
    for k in ("spa", "pool", "toggles", "mode"):
        assert after[k] == before[k]
    backend.state.filter_pump = False
    client.post("/api/mode", json={"mode": "spa"})
    assert switch_calls(backend) == [("filter_pump", True), ("spa_mode", True), ("spa_heater", True)]


# ---- persistence ---------------------------------------------------------------------

def edited(c, **changes):
    d = copy.deepcopy(doc(c))
    for k, v in changes.items():
        d[k] = v
    return d


def test_saved_settings_survive_a_restart(client, path):
    # Bug: settings applied in memory but not written (lost on the next pod restart),
    # or written in a shape the loader rejects.
    d = edited(client)
    d["limits"]["spa_max"] = 100
    d["guest_toggles"][0]["label"] = "Jets"
    assert put(client, d).status_code == 200
    on_disk = json.loads(path.read_text())
    assert on_disk["version"] == 1 and on_disk["limits"]["spa_max"] == 100
    again = ConfigStore(path, Limits(), weather={})
    assert again.saved and again.version == 1
    cfg = again.current({})
    assert cfg.limits.spa_max == 100 and cfg.guest_toggles[0].label == "Jets"


def test_failed_write_leaves_the_old_file_and_no_temp_files(client, path, monkeypatch):
    # Bug: writing the file in place, so a crash (or full disk) mid-write leaves a
    # truncated config that the next start ignores; or temp files piling up.
    d = edited(client)
    d["limits"]["spa_max"] = 101
    assert put(client, d).status_code == 200
    good = path.read_text()

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(config_store.os, "replace", boom)
    d = edited(client)
    d["limits"]["spa_max"] = 99
    r = put(client, d)
    assert r.status_code == 503 and r.json()["detail"] == "Settings storage isn't available"
    assert path.read_text() == good
    assert sorted(os.listdir(path.parent)) == ["config.json"]
    # Not applied either: the live config is still the saved one.
    assert doc(client)["limits"]["spa_max"] == 101


def test_write_is_fsynced_before_the_rename(path, monkeypatch):
    # Bug: renaming a file whose data is still only in the page cache; a power cut
    # then leaves an empty config.json.
    events = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(config_store.os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(config_store.os, "replace", lambda a, b: (events.append("replace"), real_replace(a, b))[1])
    store = ConfigStore(path, Limits(), weather={})
    store.save(store.current({}), 0)
    assert events[:2] == ["fsync", "replace"]


@pytest.mark.parametrize("content", ["{not json", json.dumps({"version": 3, "limits": "x"}), "[]",
                                     json.dumps({"version": 3, "main_page": {}, "limits": {
                                         "spa_min": 104, "spa_max": 90, "pool_heat_min": 82,
                                         "pool_heat_max": 92, "pool_chill_max": 92, "min_spread": 5},
                                         "hot_tub_on": [], "hot_tub_off": [], "guest_toggles": [],
                                         "weather": {}})],
                         ids=["not-json", "wrong-shape", "not-an-object", "inverted-limits"])
def test_corrupt_or_invalid_file_falls_back_to_defaults(path, content, backend, caplog):
    # Bug: a damaged config.json crashing the app at start (guests locked out), or
    # an invalid one (inverted limits) being applied.
    path.parent.mkdir(parents=True)
    path.write_text(content)
    svc = PoolService(backend, poll_seconds=3600, config=ConfigStore(path, Limits(), weather={}))
    with TestClient(create_app(svc)) as c:
        assert c.get("/api/state").status_code == 200
        d = doc(c)
    assert d["limits"]["spa_max"] == 103 and d["version"] == 0
    assert "invalid settings file" in caplog.text


def test_unwritable_storage_is_503_and_the_app_keeps_working(backend, tmp_path):
    # Bug: a missing/read-only volume turning a save into a 500 (or a crash), or
    # the unsaved change being applied anyway and lost on restart.
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    svc = PoolService(backend, poll_seconds=3600,
                      config=ConfigStore(blocker / "config.json", Limits(), weather={}))
    with TestClient(create_app(svc, OwnerGate(PIN, b"secret"))) as c:
        unlocked(c)
        d = doc(c)
        d["limits"]["spa_max"] = 100
        r = put(c, d)
        assert r.status_code == 503
        assert r.json()["detail"] == "Settings storage isn't available"
        assert doc(c)["limits"]["spa_max"] == 103
        assert c.post("/api/spa/setpoint", json={"set_temp": 103}).status_code == 200


def test_stale_version_is_409_and_not_applied(client):
    # Bug: two owners (or two tabs) editing at once, the second silently undoing the
    # first's changes.
    a = edited(client)
    b = edited(client)
    a["limits"]["spa_max"] = 101
    b["limits"]["spa_max"] = 99
    assert put(client, a).status_code == 200
    r = put(client, b)
    assert r.status_code == 409
    assert r.json()["detail"] == "Settings changed elsewhere; reload"
    assert doc(client)["limits"]["spa_max"] == 101


def test_reset_deletes_the_file_and_returns_defaults(client, path):
    # Bug: reset only in memory (the old file comes back on restart), or the version
    # going back to an earlier number so open pages don't notice.
    d = edited(client)
    d["limits"]["spa_max"] = 100
    put(client, d)
    r = client.post("/api/config/reset", json={})
    assert r.status_code == 200
    assert r.json()["limits"]["spa_max"] == 103
    assert r.json()["version"] == 2
    assert not path.exists()
    assert client.get("/api/state").json()["config_version"] == 2


# ---- validation ------------------------------------------------------------------------

def _set(d, dotted, value):
    *parents, last = dotted.split(".")
    for p in parents:
        d = d[int(p)] if p.isdigit() else d[p]
    if isinstance(d, list):
        d[int(last)] = value
    else:
        d[last] = value


@pytest.mark.parametrize("field,value,needle", [
    ("limits.spa_min", 104, "limits.spa_min > limits.spa_max"),
    ("limits.spa_max", 110, "limits.spa_max"),
    ("limits.min_spread", 15, "limits.pool_heat_min + limits.min_spread > limits.pool_chill_max"),
    ("limits.pool_heat_min", "82", "limits.pool_heat_min"),
    ("hot_tub_on.0.key", "aux_99", "hot_tub_on.0.key"),
    ("hot_tub_on.0", {"action": "explode", "key": "aux_2", "on": True}, "hot_tub_on.0"),
    ("hot_tub_on.0", {"action": "scene", "key": "aux_2", "on": True}, "hot_tub_on.0.key"),
    ("hot_tub_on.0", {"action": "light", "key": "aux_1", "on": True, "effect": "Plaid"}, "hot_tub_on.0.effect"),
    ("hot_tub_on.0", {"action": "switch", "key": "onetouch_2", "on": True}, "hot_tub_on.0.key"),
    ("hot_tub_off", [{"action": "switch", "key": "spa_heater", "on": False}] * 13, "hot_tub_off"),
    ("guest_toggles.1.id", "bubbles", "duplicate id"),
    ("guest_toggles.1.id", "Bad Id!", "guest_toggles.1.id"),
    ("guest_toggles.0.conflicts", ["ghost"], "guest_toggles.0.conflicts"),
    ("guest_toggles.0.modes", [], "guest_toggles.0.modes"),
    ("guest_toggles.0.label", "x" * 31, "guest_toggles.0.label"),
    ("guest_toggles.0.key", "pool_heater", "guest_toggles.0.key"),
    ("weather.lat", 34.2, "weather.lat"),
], ids=["spa-inverted", "spa-above-panel", "spread-too-wide", "string-number", "unknown-key",
        "unknown-action", "scene-on-aux", "unknown-effect", "switch-on-scene", "too-many-steps",
        "duplicate-toggle-id", "bad-toggle-id", "dangling-conflict", "no-modes", "long-label",
        "toggle-on-heater", "lat-without-lon"])
def test_invalid_settings_are_422_naming_the_field_and_nothing_changes(client, path, field, value, needle):
    # Bug: a bad document saved and applied (inverted limits, a sequence step the
    # panel can't run, a conflict pointing nowhere), or an error the page can't show.
    d = edited(client)
    _set(d, field, value)
    r = put(client, d)
    assert r.status_code == 422, r.text
    assert isinstance(r.json()["detail"], str) and needle in r.json()["detail"]
    assert doc(client)["version"] == 0 and not path.exists()


def test_conflicts_are_saved_in_both_directions(client):
    # Bug: A blocks B but B doesn't block A, so turning them on in the other order
    # puts both on.
    d = edited(client)
    for t in d["guest_toggles"]:
        t["conflicts"] = []
    d["guest_toggles"][0]["conflicts"] = ["water_features"]
    saved = put(client, d).json()
    by_id = {t["id"]: t["conflicts"] for t in saved["guest_toggles"]}
    assert by_id == {"bubbles": ["water_features"], "spillover": [], "water_features": ["bubbles"]}


# ---- owner gate --------------------------------------------------------------------------

@pytest.mark.parametrize("method,url", [("put", "/api/config"), ("post", "/api/config/reset")])
def test_writes_need_the_owner(backend, path, method, url):
    # Bug: any guest on the LAN (or a crafted request) rewriting the limits and the
    # Hot Tub sequence.
    svc = PoolService(backend, poll_seconds=3600, config=ConfigStore(path, Limits(), weather={}))
    with TestClient(create_app(svc, OwnerGate(PIN, b"secret"))) as c:
        body = doc(c)
        body["limits"]["spa_max"] = 104
        r = getattr(c, method)(url, json=body if method == "put" else {})
        assert r.status_code == 401
        assert doc(c)["limits"]["spa_max"] == 103
    svc2 = PoolService(MockBackend(), poll_seconds=3600, config=ConfigStore(path, Limits(), weather={}))
    with TestClient(create_app(svc2, OwnerGate(None))) as c:
        r = getattr(c, method)(url, json=body if method == "put" else {})
        assert r.status_code == 403
    assert not path.exists()


def test_get_config_is_public(backend, path):
    # Bug: the guest page unable to render (it needs main_page / toggles) until an
    # owner unlocks.
    svc = PoolService(backend, poll_seconds=3600, config=ConfigStore(path, Limits(), weather={}))
    with TestClient(create_app(svc, OwnerGate(PIN, b"secret"))) as c:
        assert c.get("/api/config").status_code == 200


# ---- live application -------------------------------------------------------------------

def test_new_limits_apply_to_guest_requests_at_once(backend, client):
    # Bug: limits read once at start-up, so a lowered spa max only takes effect after
    # a restart (and the sliders advertise the old range meanwhile).
    d = edited(client)
    d["limits"]["spa_max"] = 100
    assert put(client, d).status_code == 200
    assert client.post("/api/spa/setpoint", json={"set_temp": 101}).status_code == 409
    assert client.post("/api/spa/setpoint", json={"set_temp": 100}).status_code == 200
    s = client.get("/api/state").json()
    assert s["spa"]["set_max"] == 100 and s["config_version"] == 1


def test_renamed_toggle_shows_in_state(client):
    # Bug: the guest page still showing the old label after the owner renames it.
    d = edited(client)
    d["guest_toggles"][0]["label"] = "Jets"
    put(client, d)
    t = client.get("/api/state").json()["toggles"][0]
    assert (t["id"], t["label"], t["modes"]) == ("bubbles", "Jets", ["spa"])


def test_old_endpoints_follow_the_toggle_ids(backend, client):
    # Bug: the old /api/pool/spillover still driving the env device after the owner
    # pointed the Spillover toggle elsewhere, or still working once it was removed.
    d = edited(client)
    d["guest_toggles"][1]["key"] = "aux_5"
    put(client, d)
    assert client.post("/api/pool/spillover", json={"on": True}).status_code == 200
    assert ("adv_set_switch", "aux_5", True) in backend.calls
    assert not any(c[0] == "set_switch" for c in backend.calls)
    d = edited(client)
    d["guest_toggles"] = [t for t in d["guest_toggles"] if t["id"] != "bubbles"]
    put(client, d)
    assert client.post("/api/spa/bubbles", json={"on": True}).status_code == 404
    assert client.post("/api/toggle", json={"id": "bubbles", "on": True}).status_code == 404


def test_generic_toggle_conflict_is_checked_against_fresh_state(backend, client):
    # Bug: conflicts checked against the cached snapshot (the other device was
    # switched on at the panel since the last poll) or only in the page.
    d = edited(client)
    d["guest_toggles"] = [
        {"id": "cleaner", "key": "aux_4", "label": "Cleaner", "modes": ["pool"], "conflicts": ["v2"]},
        {"id": "v2", "key": "aux_5", "label": "Aux Two", "modes": ["pool", "spa"], "conflicts": []},
    ]
    assert put(client, d).status_code == 200
    backend.extra["aux_5"] = True  # behind the service's back; cache says off
    r = client.post("/api/toggle", json={"id": "cleaner", "on": True})
    assert r.status_code == 409
    assert r.json()["detail"] == "turn off Aux Two before Cleaner"
    assert not [c for c in backend.calls if c[0] == "adv_set_switch"]
    # Turning off is always allowed, and the state names the blocker.
    s = client.post("/api/toggle", json={"id": "v2", "on": False}).json()
    assert backend.calls[-1] == ("adv_set_switch", "aux_5", False)
    assert {t["id"]: t["blocked_by"] for t in s["toggles"]} == {"cleaner": None, "v2": None}


def test_generic_toggle_does_not_resend_a_toggle_for_a_device_already_there(backend, client):
    # Bug: Jandy commands are toggles; sending "on" to a device that is already on
    # turns it OFF.
    d = edited(client)
    d["guest_toggles"] = [{"id": "v2", "key": "aux_5", "label": "V2", "modes": ["pool"], "conflicts": []}]
    put(client, d)
    backend.extra["aux_5"] = True
    client.post("/api/toggle", json={"id": "v2", "on": True})
    assert not [c for c in backend.calls if c[0] == "adv_set_switch"]


# ---- Hot Tub sequences on the real library (exact wire commands) -------------------------

@pytest.fixture
def cloud():
    return AdvCloud()


@pytest.fixture
def owner(cloud, path):
    backend = asyncio.run(make_backend(cloud))
    svc = PoolService(backend, poll_seconds=3600, stale_retry_seconds=0,
                      config=ConfigStore(path, Limits(), weather={}))
    c = TestClient(create_app(svc, OwnerGate(PIN, b"secret")))
    c.__enter__()
    unlocked(c)
    cloud.sent.clear()
    yield c
    c.__exit__(None, None, None)


def cmds(cloud):
    return [c for c, _ in wire(cloud)]


def test_custom_hot_tub_on_sends_exactly_its_steps(owner, cloud):
    # Bug: the configured sequence not used (still the hard-coded one), steps out of
    # order, an already-on device toggled off, a scene re-toggled, or the light step
    # not reaching the panel.
    d = edited(owner)
    d["hot_tub_on"] = [
        {"action": "switch", "key": "pool_pump", "on": True},         # already on: nothing
        {"action": "scene", "key": "onetouch_3", "on": True},
        {"action": "light", "key": "icl_zone_1", "on": True, "effect": "Sky Blue"},
        {"action": "switch", "key": "spa_pump", "on": True},
        {"action": "spa_setpoint_max", "value": 98},
        {"action": "switch", "key": "spa_heater", "on": True},
    ]
    assert put(owner, d).status_code == 200, put(owner, d).text
    cloud.sent.clear()
    r = owner.post("/api/mode", json={"mode": "spa"})
    assert r.status_code == 200, r.text
    # (The fixture panel has a heat pump, so set points go through setpoint_hpm_temp.)
    assert cmds(cloud) == ["set_onetouch_3", "onoff_iclzone", "set_iclzone_color", "set_spa_pump",
                           "setpoint_hpm_temp", "set_spa_heater"]
    assert ("setpoint_hpm_temp", {"spaheatsetpointtemp": "98"}) in wire(cloud)
    assert r.json()["mode"] == "spa"
    # Run again: everything is already there, so nothing is sent (no toggling back).
    cloud.sent.clear()
    owner.post("/api/mode", json={"mode": "spa"})
    assert cmds(cloud) == []


def test_owner_light_step_uses_the_panel_effect(owner, cloud):
    # Bug: a light step naming a panel effect that isn't one of the guest colors
    # (here the ICL's own "Alpine White") rejected or sent to the wrong light.
    d = edited(owner)
    d["hot_tub_off"] = [{"action": "light", "key": "icl_zone_1", "on": True, "effect": "Alpine White"}]
    assert put(owner, d).status_code == 200
    cloud.sent.clear()
    assert owner.post("/api/mode", json={"mode": "pool"}).status_code == 200
    assert cmds(cloud) == ["onoff_iclzone", "set_iclzone_color"]


def test_spa_cap_step_lowers_only_when_above_and_never_past_the_guest_max(owner, cloud):
    # Bug: the cap raising a lower set point, or a step value above the guest limit
    # pushing the spa past SPA_MAX.
    d = edited(owner)
    d["limits"]["spa_max"] = 99
    d["hot_tub_on"] = [{"action": "spa_setpoint_max", "value": 104}]
    put(owner, d)
    cloud.sent.clear()
    owner.post("/api/mode", json={"mode": "spa"})
    assert wire(cloud) == [("setpoint_hpm_temp", {"spaheatsetpointtemp": "99"})]  # panel was at 100
    cloud.sent.clear()
    owner.post("/api/mode", json={"mode": "spa"})
    assert wire(cloud) == []
    cloud._home("spa_set_point")["spa_set_point"] = "90"  # below the cap: left alone
    owner.post("/api/mode", json={"mode": "spa"})
    assert wire(cloud) == []


def test_failing_step_stops_the_sequence_and_says_which(owner, cloud, caplog):
    # Bug: a failed step skipped and the rest run anyway (heater on without the spa
    # valves moved), or the library's error text reaching the guest page.
    from iaqualink.exception import AqualinkServiceException

    real = cloud.send_request

    async def failing(url, method="get", **kw):
        if "set_spa_pump" in url or (kw.get("params") or {}).get("command") == "set_spa_pump":
            raise AqualinkServiceException("secret-session-xyz exploded")
        return await real(url, method, **kw)

    cloud.send_request = failing
    r = owner.post("/api/mode", json={"mode": "spa"})
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert "Hot Tub On stopped at step 2 of 4" in detail and "Spa mode on" in detail
    assert "secret-session" not in detail
    assert "set_spa_heater" not in cmds(cloud)
    assert "secret-session" in caplog.text  # the detail is in the log


def test_device_removed_from_the_panel_refuses_before_sending_anything(backend, client, monkeypatch):
    # Bug: a sequence started, then stopped half way at a device that no longer
    # exists, leaving the spa half on.
    from app.backends import mock

    d = edited(client)
    d["hot_tub_on"] = [{"action": "switch", "key": "spa_pump", "on": True},
                       {"action": "switch", "key": "aux_6", "on": True}]
    assert put(client, d).status_code == 200
    monkeypatch.setattr(mock, "DEVICES", [x for x in mock.DEVICES if x[0] != "aux_6"])
    r = client.post("/api/mode", json={"mode": "spa"})
    assert r.status_code == 409
    assert "step 2" in r.json()["detail"]
    assert not [c for c in backend.calls if c[0] in ("set_switch", "adv_set_switch")]


def test_generic_toggle_on_a_scene_sends_the_onetouch_command(owner, cloud):
    # Bug: a guest toggle mapped onto a OneTouch scene sending an aux command, or
    # re-toggling a scene that is already on.
    d = edited(owner)
    d["guest_toggles"] = [{"id": "falls", "key": "onetouch_6", "label": "Falls", "modes": ["pool"],
                           "conflicts": []}]
    assert put(owner, d).status_code == 200, put(owner, d).text
    cloud.sent.clear()
    r = owner.post("/api/toggle", json={"id": "falls", "on": True})
    assert r.status_code == 200
    assert cmds(cloud) == ["set_onetouch_6"]
    assert r.json()["toggles"][0]["on"] is True
    cloud.sent.clear()
    owner.post("/api/toggle", json={"id": "falls", "on": True})
    assert cmds(cloud) == []


# ---- weather location --------------------------------------------------------------------

def test_weather_location_change_drops_the_cached_forecast(backend, client, monkeypatch):
    # Bug: the weather card keeps showing the old place's forecast (cached for 10
    # minutes) after the owner changes the location, or keeps using the env one.
    from tests.test_weather import FIXTURE, Clock, Fetcher

    raw = json.loads(FIXTURE.read_text())
    fetched = []
    base = Fetcher(raw)

    async def recording(cfg):
        fetched.append((cfg.lat, cfg.lon, cfg.label))
        return await base(cfg)

    monkeypatch.setattr(weather, "_service", weather.WeatherService(
        weather.Config(lat=34.2, lon=-77.8, label="Old"), fetcher=recording, clock=Clock()))
    assert client.get("/api/weather").json()["available"] is True
    client.get("/api/weather")
    assert fetched == [(34.2, -77.8, "Old")]  # cached
    d = edited(client)
    d["weather"] = {"zip": None, "country": "us", "lat": 40.0, "lon": -75.0, "label": "New"}
    assert put(client, d).status_code == 200
    body = client.get("/api/weather").json()
    assert fetched[-1] == (40.0, -75.0, "New")
    assert body["location"] == "New"


# ---- generic toggles keep the toggle safety ------------------------------------------------

def _two_toggles(owner):
    d = edited(owner)
    d["guest_toggles"] = [
        {"id": "cleaner", "key": "aux_1", "label": "Cleaner", "modes": ["pool"], "conflicts": ["extra"]},
        {"id": "extra", "key": "aux_EA", "label": "Extra", "modes": ["pool"], "conflicts": []},
    ]
    assert put(owner, d).status_code == 200


def test_generic_toggle_double_tap_with_lagging_cloud_sends_once(owner, cloud):
    # Bug: the cloud still reports the old state after the first tap, so a second
    # tap (or a second guest) sends the toggle again and switches it back off.
    from tests.test_advanced_iaqualink import lagging

    _two_toggles(owner)
    lagging(cloud)
    cloud.sent.clear()
    assert owner.post("/api/toggle", json={"id": "cleaner", "on": True}).status_code == 200
    r = owner.post("/api/toggle", json={"id": "cleaner", "on": True})
    assert r.status_code == 200
    assert cmds(cloud) == ["set_aux_1"]
    assert cloud._aux("aux_1")["state"] == "1"
    t = {x["id"]: x for x in r.json()["toggles"]}
    assert t["cleaner"]["on"] is True and t["extra"]["blocked_by"] == "Cleaner"
    # The conflict sees the commanded state too, though the cloud still says off.
    r = owner.post("/api/toggle", json={"id": "extra", "on": True})
    assert r.status_code == 409 and cmds(cloud) == ["set_aux_1"]


@pytest.mark.parametrize("breakage", ["incomplete", "offline"])
def test_generic_toggle_refused_on_stale_or_offline_data(owner, cloud, breakage):
    # Bug: a toggle decided from an update the library ignored (or an Offline
    # panel's all-off reading), sending the command the wrong way.
    _two_toggles(owner)
    if breakage == "incomplete":
        cloud._home("system_type")["system_type"] = ""
    else:
        cloud._home("status")["status"] = "Offline"
    cloud.sent.clear()
    r = owner.post("/api/toggle", json={"id": "cleaner", "on": True})
    assert r.status_code == 502
    assert cmds(cloud) == []


def test_generic_toggle_reverse_while_settling_is_refused(owner, cloud):
    # Bug: "off" right after "on" while the cloud lags: the library still thinks the
    # device is off and sends nothing, yet the guest is told it worked.
    from tests.test_advanced_iaqualink import lagging

    _two_toggles(owner)
    lagging(cloud)
    owner.post("/api/toggle", json={"id": "cleaner", "on": True})
    cloud.sent.clear()
    r = owner.post("/api/toggle", json={"id": "cleaner", "on": False})
    assert r.status_code == 409
    assert cmds(cloud) == [] and cloud._aux("aux_1")["state"] == "1"


def test_store_refuses_a_stale_save_on_its_own(path):
    # Bug: the store trusting its caller's version check, so any other caller (or a
    # refactor of the route) can overwrite a newer document.
    store = ConfigStore(path, Limits(), weather={})
    first = store.current({})
    store.save(first, 0)
    with pytest.raises(config_store.StaleVersion):
        store.save(first, 0)
    assert store.version == 1


def test_zero_spread_is_refused_in_settings_and_env(client, monkeypatch):
    # Bug: min_spread 0 let a guest set heat == chill, so the heat pump heats and
    # chills against itself around one temperature.
    d = edited(client)
    d["limits"]["min_spread"] = 0
    r = put(client, d)
    assert r.status_code == 422
    assert r.json()["detail"] == "limits.min_spread: must be between 1 and 70"
    from app.main import limits_from_env
    monkeypatch.setenv("POOL_MIN_SPREAD", "0")
    with pytest.raises(SystemExit, match="POOL_MIN_SPREAD < 1"):
        limits_from_env()
