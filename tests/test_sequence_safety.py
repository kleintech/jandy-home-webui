"""Hot Tub sequences, the settle overlays and guest toggle limits against a lagging or
surprising controller (FakeCloud through the real iaqualink-py library, or the mock).
Each test names the bug it catches."""

import asyncio
import copy
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import advanced
from app.backends.base import StaleData
from app.backends.mock import MockBackend
from app.config_store import ConfigStore, Limits
from app.main import create_app
from app.owner_auth import OwnerGate
from app.service import PoolService, RuleError
from tests.test_advanced_iaqualink import AdvCloud, cmds, lagging
from tests.test_iaqualink_backend import make_backend

PIN = "13572468"


def owner(cloud, tmp_path, **mapping):
    backend = asyncio.run(make_backend(cloud, **mapping))
    svc = PoolService(backend, poll_seconds=3600, stale_retry_seconds=0,
                      config=ConfigStore(tmp_path / "c.json", Limits(), weather={}))
    c = TestClient(create_app(svc, OwnerGate(PIN, b"secret")))
    c.__enter__()
    assert c.post("/api/advanced/unlock", json={"pin": PIN}).status_code == 200
    cloud.sent.clear()
    return c, svc


@pytest.fixture
def cloud():
    return AdvCloud()


def save(c, **changes):
    d = c.get("/api/config").json()
    d.update(changes)
    r = c.put("/api/config", json=d)
    return r


def side_effect(cloud, command, fn):
    """Run fn() after the cloud handles `command` (what a OneTouch macro does)."""
    orig = cloud.send_request

    async def send(url, method="get", **kw):
        r = await orig(url, method, **kw)
        if (kw.get("params") or {}).get("command") == command:
            fn()
        return r

    cloud.send_request = send


def stale_home(cloud, also=()):
    """get_home (and the given commands' replies) keep reporting the home screen from
    before any command: the cloud lags."""
    frozen = copy.deepcopy(cloud.home)
    orig = cloud.send_request

    async def send(url, method="get", **kw):
        r = await orig(url, method, **kw)
        cmd = (kw.get("params") or {}).get("command", "")
        if cmd == "get_home" or cmd in also:
            return httpx.Response(200, json=copy.deepcopy(frozen), request=r.request)
        return r

    cloud.send_request = send


# ---- B1: each step decides from the state after the previous one --------------------

def test_hot_tub_on_scene_then_spa_pump_does_not_toggle_the_pump_back_off(cloud, tmp_path):
    # Bug: a OneTouch scene step turns spa mode on at the panel; the next step
    # "spa_pump on" decided from the state read before the sequence (off) and sent
    # the toggle, switching spa mode back OFF.
    c, _ = owner(cloud, tmp_path)
    side_effect(cloud, "set_onetouch_3", lambda: cloud._home("spa_pump").update(spa_pump="1"))
    assert save(c, hot_tub_on=[{"action": "scene", "key": "onetouch_3", "on": True},
                               {"action": "switch", "key": "spa_pump", "on": True}]).status_code == 200
    cloud.sent.clear()
    r = c.post("/api/mode", json={"mode": "spa"})
    assert r.status_code == 200, r.text
    assert cmds(cloud) == ["set_onetouch_3"]
    assert cloud._home("spa_pump")["spa_pump"] == "1"


def test_hot_tub_off_scene_then_heater_off_does_not_turn_the_heater_back_on(cloud, tmp_path):
    # Bug: the scene turns the spa heater off; the "spa_heater off" step decided from
    # the pre-sequence state (on) and toggled the heater back ON.
    c, _ = owner(cloud, tmp_path)
    cloud._home("spa_heater")["spa_heater"] = "1"
    cloud._ot("onetouch_3")["state"] = "1"
    side_effect(cloud, "set_onetouch_3", lambda: cloud._home("spa_heater").update(spa_heater="0"))
    assert save(c, hot_tub_off=[{"action": "scene", "key": "onetouch_3", "on": False},
                                {"action": "switch", "key": "spa_heater", "on": False}]).status_code == 200
    cloud.sent.clear()
    r = c.post("/api/mode", json={"mode": "pool"})
    assert r.status_code == 200, r.text
    assert cmds(cloud) == ["set_onetouch_3"]
    assert cloud._home("spa_heater")["spa_heater"] == "0"


@pytest.mark.parametrize("failure", ["stale", "offline"])
def test_sequence_stops_when_the_state_after_a_step_cant_be_read(failure):
    # Bug: after a step the controller sends an incomplete update (or drops offline)
    # and the next steps run blind on the old state, toggling things the wrong way.
    backend = MockBackend()
    backend.state.filter_pump = False
    orig = backend.refresh

    async def refresh():
        snap = await orig()
        if any(x[0] == "set_switch" for x in backend.calls):
            if failure == "stale":
                raise StaleData("incomplete")
            snap.connected = False
        return snap

    backend.refresh = refresh
    with TestClient(create_app(PoolService(backend, poll_seconds=3600, stale_retry_seconds=0))) as c:
        r = c.post("/api/mode", json={"mode": "spa"})
    assert r.status_code == 409
    assert r.json()["detail"] == ("Hot Tub On stopped after step 1 of 4 (Filter pump on): the controller "
                                  "didn't report its state; the steps after it weren't run")
    assert [x for x in backend.calls if x[0] == "set_switch"] == [("set_switch", "filter_pump", True)]


# ---- B2: a guest reversal while the first command settles is refused ----------------

def test_guest_toggle_reversed_while_settling_is_refused_not_dropped(cloud, tmp_path):
    # Bug: Bubbles on, the cloud still says off, Bubbles off -> the library thinks
    # it's already off and sends nothing, the page says OK, the blower keeps running.
    c, _ = owner(cloud, tmp_path)
    lagging(cloud)
    assert c.post("/api/toggle", json={"id": "bubbles", "on": True}).status_code == 200
    cloud.sent.clear()
    r = c.post("/api/toggle", json={"id": "bubbles", "on": False})
    assert r.status_code == 409
    assert r.json()["detail"] == "Bubbles is still changing; try again in a few seconds"
    assert cmds(cloud) == []


def test_hot_tub_off_right_after_on_is_refused_not_reported_ok(cloud, tmp_path):
    # Bug: Hot Tub Off within the settle window, cloud lagging: every "off" step was
    # silently skipped and the page said OK with the heater still on.
    c, _ = owner(cloud, tmp_path)
    stale_home(cloud, also=("set_spa_pump", "set_spa_heater", "set_pool_pump"))
    assert c.post("/api/mode", json={"mode": "spa"}).status_code == 200
    cloud.sent.clear()
    r = c.post("/api/mode", json={"mode": "pool"})
    assert r.status_code == 409
    assert "Spa heater is still changing" in r.json()["detail"]
    assert "step 1 of 2" in r.json()["detail"]
    assert cmds(cloud) == []


# ---- B3: the owner's single set point checks the spread against what we wrote -------

def test_owner_single_setpoint_checks_spread_against_the_value_just_written(cloud, tmp_path):
    # Bug: chill lowered to 85, the cloud still reports 90, then heat raised to 88
    # alone: validated against chill 90 and sent, leaving heat 88 above chill 85.
    c, _ = owner(cloud, tmp_path)
    stale_home(cloud)
    r = c.post("/api/advanced/setpoints", json={"pool_chill": 85})
    assert r.status_code == 200
    assert r.json()["setpoints"]["pool_chill"]["value"] == 85  # shown as commanded
    r = c.post("/api/advanced/setpoints", json={"pool_heat": 88})
    assert r.status_code == 409
    assert r.json()["detail"] == "chill must be at least 1 degree above heat"
    assert cloud._home("pool_set_point")["pool_set_point"] == "84"


# ---- B4: every step is checked before anything is sent --------------------------------

def test_light_step_with_unknown_effect_is_caught_before_any_step_runs(cloud, tmp_path):
    # Bug: saved while the controller was unreachable (effects unchecked), Hot Tub On
    # ran step 1 (spa mode on) and only then failed on the light's effect.
    c, svc = owner(cloud, tmp_path)
    svc.snap.connected = False
    assert save(c, hot_tub_on=[{"action": "switch", "key": "spa_pump", "on": True},
                               {"action": "light", "key": "icl_zone_1", "on": True,
                                "effect": "Nope"}]).status_code == 200
    cloud.sent.clear()
    r = c.post("/api/mode", json={"mode": "spa"})
    assert r.status_code == 409
    assert "step 2 of 2" in r.json()["detail"] and "no effect 'Nope'" in r.json()["detail"]
    assert cmds(cloud) == []


def test_light_step_on_a_plain_aux_is_caught_before_any_step_runs(cloud, tmp_path):
    # Bug: a "light" step saved offline for aux_3 (an aux, not a light) passed the
    # old pre-pass (it only asked whether the key exists), so step 1 ran and step 2
    # switched the aux.
    c, svc = owner(cloud, tmp_path)
    svc.snap.connected = False
    assert save(c, hot_tub_on=[{"action": "switch", "key": "spa_pump", "on": True},
                               {"action": "light", "key": "aux_3", "on": True}]).status_code == 200
    cloud.sent.clear()
    r = c.post("/api/mode", json={"mode": "spa"})
    assert r.status_code == 409
    assert "can't be used as a light" in r.json()["detail"]
    assert cmds(cloud) == []


@pytest.mark.parametrize("change", ["gone", "now_a_scene"])
def test_device_gone_or_changed_mid_sequence_stops_before_its_step(tmp_path, change):
    # Bug: the panel stops reporting a device, or reports it as another kind (a
    # switch step's aux is now a OneTouch scene), after step 1; step 2 was still
    # sent, decided from the snapshot read before the sequence.
    backend = MockBackend()
    store = ConfigStore(tmp_path / "c.json", Limits(), weather={})
    svc = PoolService(backend, poll_seconds=3600, stale_retry_seconds=0, config=store)
    orig = backend._advanced

    def advanced_view():
        a = orig()
        if any(x[0] == "set_switch" for x in backend.calls):
            if change == "gone":
                a["devices"].pop("aux_4")
            else:
                a["devices"]["aux_4"] = {**a["devices"]["aux_4"], "kind": "scene"}
        return a

    backend._advanced = advanced_view
    backend.state.spa_mode = False
    with TestClient(create_app(svc, OwnerGate(PIN, b"secret"))) as c:
        assert c.post("/api/advanced/unlock", json={"pin": PIN}).status_code == 200
        assert save(c, hot_tub_on=[{"action": "switch", "key": "spa_pump", "on": True},
                                   {"action": "switch", "key": "aux_4", "on": True}]).status_code == 200
        r = c.post("/api/mode", json={"mode": "spa"})
    assert r.status_code == 409
    assert r.json()["detail"].startswith("Hot Tub On stopped at step 2 of 2 (")
    assert r.json()["detail"].endswith("the steps after it weren't run")
    assert ("adv_set_switch", "aux_4", True) not in backend.calls


# ---- B5: guest toggles never drive pumps or heaters ---------------------------------

def test_guest_toggle_on_a_device_mapped_to_the_filter_pump_is_refused_at_save(cloud, tmp_path):
    # Bug: JANDY_FILTER_PUMP_DEVICE=aux_1 and a guest toggle on aux_1 saved: any guest
    # could switch the filter pump off.
    c, _ = owner(cloud, tmp_path, filter_pump="aux_1")
    r = save(c, guest_toggles=[{"id": "x", "key": "aux_1", "label": "X", "modes": ["pool"],
                                "conflicts": []}])
    assert r.status_code == 422
    assert r.json()["detail"] == "guest_toggles.0.key: aux_1 runs the filter pump; guests can't switch it"


def test_guest_toggle_saved_before_the_device_became_the_pump_is_refused(cloud, tmp_path):
    # Bug: settings saved with a toggle on aux_1, then the env map made aux_1 the
    # filter pump; the toggle still switched it.
    path = tmp_path / "c.json"
    path.write_text(json.dumps({
        "version": 1, "limits": {"spa_min": 80, "spa_max": 103, "pool_heat_min": 82, "pool_heat_max": 92,
                                 "pool_chill_max": 92, "min_spread": 5},
        "hot_tub_on": [], "hot_tub_off": [], "weather": {},
        "guest_toggles": [{"id": "x", "key": "aux_1", "label": "X", "modes": ["pool"], "conflicts": []}]}))
    backend = asyncio.run(make_backend(cloud, filter_pump="aux_1"))
    svc = PoolService(backend, poll_seconds=3600, config=ConfigStore(path, Limits(), weather={}))
    with TestClient(create_app(svc, OwnerGate(PIN, b"secret"))) as c:
        cloud.sent.clear()
        r = c.post("/api/toggle", json={"id": "x", "on": True})
    assert r.status_code == 409
    assert r.json()["detail"] == "X runs the filter pump; guests can't switch it"
    assert cmds(cloud) == []


def test_scene_as_guest_toggle_is_allowed_with_a_warning(cloud, tmp_path):
    # Bug: the owner not told that a OneTouch scene toggle lets guests run whatever
    # the scene switches (pumps, heaters).
    c, _ = owner(cloud, tmp_path)
    r = save(c, guest_toggles=[{"id": "falls", "key": "onetouch_6", "label": "Falls", "modes": ["pool"],
                                "conflicts": []},
                               {"id": "air", "key": "aux_2", "label": "Air", "modes": ["spa"],
                                "conflicts": []}])
    assert r.status_code == 200, r.text
    assert r.json()["warnings"] == [
        "Falls: OneTouch scenes can switch pumps and heaters; guests will be able to run it"]


# ---- B7: guest set points use the limits current when the command runs ---------------

def test_guest_setpoint_uses_limits_saved_while_it_waited_for_the_lock():
    # Bug: the limits were read before waiting for the lock; a save that lowered
    # spa_max meanwhile didn't apply, and 103 was sent.
    async def go():
        backend = MockBackend()
        svc = PoolService(backend, poll_seconds=3600, stale_retry_seconds=0)
        await svc.start()
        try:
            async with svc._lock:  # another command is running
                task = asyncio.create_task(svc.set_spa_setpoint(103))
                await asyncio.sleep(0)
                cfg = svc.app_config()
                new = cfg.model_copy(deep=True)
                new.limits.spa_max = 100
                svc.config.save(new, cfg.version)
            with pytest.raises(RuleError, match="between 80 and 100"):
                await task
            assert not [x for x in backend.calls if x[0] == "set_spa_setpoint"]
        finally:
            await svc.close()

    asyncio.run(go())


# ---- B8/B9: salt boost ----------------------------------------------------------------

def test_boost_stop_carries_the_cells_hours_and_mode():
    # Bug: stop/pause/resume sent without boosthrs/boostmode, which the reference
    # lists for every control_swc_boost.
    assert advanced.boost_params("stop", 12, "spillover") == {
        "boosthrs": "12", "boostmode": "spillover", "boostcontrol": "stop"}
    # Unknown values are left out, never invented, so a boost stays stoppable.
    assert advanced.boost_params("stop", None, None) == {"boostcontrol": "stop"}


@pytest.mark.parametrize("status", [None, {}, [], False, 0])
def test_non_string_boost_status_is_unknown_and_refuses_start(status, tmp_path):
    # Bug: boostStatus null/{}/false read as "" = off, so "start" was sent to a cell
    # whose boost state we don't actually know.
    cloud = AdvCloud()
    cloud.swc["boostStatus"] = status
    assert advanced.parse_swc_config(cloud.swc)["boost"]["status"] == "unknown"
    c, _ = owner(cloud, tmp_path)
    r = c.post("/api/advanced/salt/boost", json={"action": "start", "hours": 2, "mode": "pool"})
    assert r.status_code == 409
    assert "control_swc_boost" not in cmds(cloud)
