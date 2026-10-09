"""Owner Advanced controls through the real iaqualink-py library against the fake
iAqualink cloud: exact wire commands, the device allowlist, toggle safety and the
salt cell commands. Each test names the bug it catches."""

import asyncio
import copy
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from iaqualink.exception import AqualinkServiceException
from iaqualink.systems.iaqua.device import ICL_EFFECTS, IaquaVSPump

from app.backends.base import BackendError
from app.main import create_app
from app.owner_auth import OwnerGate
from app.service import PoolService
from tests.test_iaqualink_backend import FakeCloud, make_backend

PIN = "246810"


class AdvCloud(FakeCloud):
    """FakeCloud plus the commands only the Advanced controls send: OneTouch scenes,
    solar heater, heat pump enable/mode, ICL zones, aux lights, VSP and salt cell."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.swc = {"serial": "SN1", "device_status": "online", "is_error": False, "response": "success",
                    "poolSWCSP": 30, "spaSWCSP": 15, "boostStatus": "", "boostHrsVal": 24,
                    "remainingBoostHrs": 0, "remainingBoostMins": 0, "boostMode": "pool",
                    "boostDipSwitch": "on"}
        self.vsp = [{"speedid": 1, "speedName": "LO", "speedvalue": 1200, "enabled": "false"},
                    {"speedid": 2, "speedName": "HI", "speedvalue": 3000, "enabled": "false"}]

    def _ot(self, key):
        entry = next(x for x in self.otc["onetouch_screen"] if key in x)[key]
        return next(a for a in entry if "state" in a)

    def _hp(self):
        return self._home("heatpump_info")["heatpump_info"]

    def _hp_echo(self):
        hp = self._hp()
        return {"isHPMPresent": True, "HPMstatus": hp["heatpumpstatus"], "HPMmode": hp["heatpumpmode"],
                "HPMtype": hp["heatpumptype"], "isChillAvailable": hp["isChillAvailable"],
                "response": "success", "is_error": False}

    def _icl(self):
        return self.devs["icl_info_list"][0]

    async def send_request(self, url, method="get", **kw):
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        q.update({k: str(v) for k, v in (kw.get("params") or {}).items()})
        cmd = q.get("command", "?")
        body = None
        if cmd.startswith("set_onetouch_"):
            st = self._ot("onetouch_" + cmd.removeprefix("set_onetouch_"))
            st["state"] = "0" if st["state"] == "1" else "1"
        elif cmd == "set_solar_heater":
            h = self._home("solar_heater")
            h["solar_heater"] = "0" if h["solar_heater"] in ("1", "3") else "1"
        elif cmd == "enable_disable_hpm":
            self._hp()["heatpumpstatus"] = "enabled" if q["on_off_action"] == "on" else "off"
            body = self._hp_echo()
        elif cmd == "switch_hpm_mode":
            self._hp()["heatpumpmode"] = q["hpm_mode"]
            body = self._hp_echo()
        elif cmd == "onoff_iclzone":
            self._icl()["zoneStatus"] = q["on_off_action"]
            body = {"icl_info_list": copy.deepcopy(self.devs["icl_info_list"])}
        elif cmd == "set_iclzone_color":
            if "color_id" in q:
                self._icl()["zoneColor"] = q["color_id"]
                self._icl()["zoneColorVal"] = {v: k for k, v in ICL_EFFECTS.items()}[int(q["color_id"])]
            self._icl()["dim_level"] = q["dim_level"]
            body = {"icl_info_list": copy.deepcopy(self.devs["icl_info_list"])}
        elif cmd == "set_light":
            self._aux("aux_" + q["aux"])["state"] = "0" if q["light"] == "0" else "1"
        elif cmd in ("get_swc_config", "set_swc_config", "control_swc_boost"):
            if cmd == "set_swc_config":
                self.swc.update(poolSWCSP=int(q["poolswcsp"]), spaSWCSP=int(q["spaswcsp"]))
            elif cmd == "control_swc_boost":
                self.swc["boostStatus"] = {"start": "on", "stop": "", "pause": "paused",
                                           "resume": "on"}[q["boostcontrol"]]
            body = self.swc
        elif cmd == "get_vsp_speedauxinfo":
            body = {"vsp_speedInfo": copy.deepcopy(self.vsp), "response": "success"}
        elif cmd == "enable_disable_pump_speedId":
            for p in self.vsp:
                p["enabled"] = "true" if q["on_off_action"] == "on" and str(p["speedid"]) == q["speed_id"] else "false"
            body = {"vsp_speedInfo": [{"speedId": p["speedid"], "status": "Enabled" if p["enabled"] == "true"
                                       else "Disabled"} for p in self.vsp], "status": "success"}
        r = await super().send_request(url, method, **kw)
        if body is not None:
            return httpx.Response(200, json=copy.deepcopy(body), request=r.request)
        return r


def wire(cloud):
    """(command, params) of everything except reads."""
    return [(c, e) for c, e in cloud.sent if not c.startswith("get_")]


def cmds(cloud):
    return [c for c, _ in wire(cloud)]


def owner_client(cloud, **mapping):
    backend = asyncio.run(make_backend(cloud, **mapping))
    svc = PoolService(backend, poll_seconds=3600, stale_retry_seconds=0)
    c = TestClient(create_app(svc, OwnerGate(PIN, b"test-secret")))
    c.__enter__()
    assert c.post("/api/advanced/unlock", json={"pin": PIN}).status_code == 200
    cloud.sent.clear()
    return c


@pytest.fixture
def cloud():
    return AdvCloud()


@pytest.fixture
def client(cloud):
    c = owner_client(cloud)
    yield c
    c.__exit__(None, None, None)


def item(view, key):
    for g in view["switches"]:
        for i in g["items"]:
            if i["key"] == key:
                return i
    return None


# ---- allowlist ---------------------------------------------------------------------

@pytest.mark.parametrize("key", ["aux_2&x", "pool_pump;", "aux_2 ", "../aux_2", "set_aux_2", "aux_2\nset_pool_pump"])
def test_injected_key_is_refused_and_nothing_sent(client, cloud, key):
    # Bug: a key with a suffix reaching the library (or being formatted into a
    # set_* command) would send an arbitrary toggle to the panel.
    r = client.post("/api/advanced/switch", json={"key": key, "on": True})
    assert r.status_code in (409, 422), r.text
    assert wire(cloud) == []


@pytest.mark.parametrize("key", ["aux_99", "spa_thermostat", "pool_temp", "spa_set_point", "heatpump_mode",
                                 "solar_heater", "onetouch_4"])
def test_key_not_in_allowlist_is_refused(client, cloud, key):
    # Bug: any key in the library's device dict accepted (a sensor, a set point, the
    # virtual thermostat, a disabled OneTouch slot, or solar on a panel without it).
    r = client.post("/api/advanced/switch", json={"key": key, "on": True})
    assert r.status_code == 409, r.text
    assert wire(cloud) == []


@pytest.mark.parametrize("key,command", [
    ("aux_1", "set_aux_1"), ("aux_B3", "set_aux_B3"), ("aux_EA", "set_aux_EA"),
    ("pool_heater", "set_pool_heater"), ("spa_heater", "set_spa_heater"),
    ("onetouch_3", "set_onetouch_3"), ("onetouch_6", "set_onetouch_6"),
])
def test_switch_sends_exactly_that_device_command(client, cloud, key, command):
    # Bug: an owner switch driving the wrong relay or scene.
    r = client.post("/api/advanced/switch", json={"key": key, "on": True})
    assert r.status_code == 200, r.text
    assert cmds(cloud) == [command]
    assert item(r.json(), key)["on"] is True


def test_solar_heater_is_listed_and_switchable_only_when_the_panel_has_one(cloud):
    # Bug: solar heater shown (and toggled) on a panel that reports it blank.
    cloud._home("solar_heater")["solar_heater"] = "0"
    c = owner_client(cloud)
    try:
        assert item(c.get("/api/advanced").json(), "solar_heater")["kind"] == "heater"
        cloud.sent.clear()
        c.post("/api/advanced/switch", json={"key": "solar_heater", "on": True})
        assert cmds(cloud) == ["set_solar_heater"]
    finally:
        c.__exit__(None, None, None)


@pytest.mark.parametrize("key", ["spa_set_point", "pool_temp", "spa_thermostat", "heatpump_mode", "solar_heater"])
def test_backend_refuses_non_allowlisted_keys_on_its_own(cloud, key):
    # Bug: the backend trusts the service's check and switches any library device
    # (defense in depth: a future caller may skip the service).
    async def go():
        b = await make_backend(cloud)
        await b.refresh()
        cloud.sent.clear()
        with pytest.raises(BackendError):
            await b.adv_set_switch(key, True)
        assert wire(cloud) == []

    asyncio.run(go())


def test_placeholder_virtual_auxes_are_flagged_but_mapped_ones_are_not(client):
    # Bug: 28 unused "Aux V<n>" slots shown like real circuits, or the one used for
    # Water Features ("Aux V1") hidden away with them.
    v = client.get("/api/advanced").json()
    assert item(v, "aux_5")["placeholder"] is True and item(v, "aux_D8")["placeholder"] is True
    assert item(v, "aux_4")["placeholder"] is False and item(v, "aux_4")["role"] == "water_features"
    assert item(v, "aux_3")["placeholder"] is False  # "Aux3": not a virtual slot
    assert item(v, "aux_2")["role"] == "bubbles"


# ---- toggle safety -----------------------------------------------------------------

def test_already_on_device_is_not_toggled(client, cloud):
    # Bug: set_pool_pump is a toggle; "on" for a running pump would switch it OFF.
    r = client.post("/api/advanced/switch", json={"key": "pool_pump", "on": True})
    assert r.status_code == 200
    assert wire(cloud) == []
    assert cloud._home("pool_pump")["pool_pump"] == "1"


def test_decides_from_fresh_state_not_the_cached_view(client, cloud):
    # Bug: the cached view says aux_1 is off (it was turned on at the panel since),
    # and "on" toggles it off.
    client.get("/api/advanced")
    cloud._aux("aux_1")["state"] = "1"
    client.post("/api/advanced/switch", json={"key": "aux_1", "on": True})
    assert wire(cloud) == []
    assert cloud._aux("aux_1")["state"] == "1"


def test_offline_controller_gets_no_owner_commands(client, cloud):
    # Bug: an Offline reply (all-off snapshot) followed by toggles.
    cloud._home("status")["status"] = "Offline"
    r = client.post("/api/advanced/switch", json={"key": "aux_1", "on": True})
    assert r.status_code == 502
    assert wire(cloud) == []


def test_incomplete_update_refuses_instead_of_toggling_blind(client, cloud):
    # Bug: a NaN aux makes the library keep old aux states; toggling from them
    # flips aux_1 off when it was turned on at the panel.
    cloud._aux("aux_1")["state"] = "1"
    cloud._aux("aux_6")["state"] = "NaN"
    r = client.post("/api/advanced/switch", json={"key": "aux_1", "on": True})
    assert r.status_code == 502
    assert wire(cloud) == []


def lagging(cloud):
    """Device replies keep reporting the aux states from before any command."""
    stale = copy.deepcopy(cloud.devs)
    orig = cloud.send_request

    async def laggy(url, method="get", **kw):
        r = await orig(url, method, **kw)
        cmd = (kw.get("params") or {}).get("command", "")
        if cmd == "get_devices" or cmd.startswith("set_aux"):
            return httpx.Response(200, json=copy.deepcopy(stale), request=r.request)
        return r

    cloud.send_request = laggy


def test_double_tap_with_lagging_cloud_does_not_undo_itself(client, cloud):
    # Bug: the cloud still says aux_1 is off after the first toggle, so the second
    # "on" toggles it back off.
    lagging(cloud)
    assert client.post("/api/advanced/switch", json={"key": "aux_1", "on": True}).status_code == 200
    r = client.post("/api/advanced/switch", json={"key": "aux_1", "on": True})
    assert r.status_code == 200
    assert cmds(cloud) == ["set_aux_1"]
    assert cloud._aux("aux_1")["state"] == "1"
    assert item(r.json(), "aux_1")["on"] is True  # the view shows what was commanded


def test_reverse_while_settling_is_refused_not_silently_dropped(client, cloud):
    # Bug: "off" right after "on" while the cloud lags: the library thinks it's
    # already off and sends nothing, yet the owner is told it worked.
    lagging(cloud)
    client.post("/api/advanced/switch", json={"key": "aux_1", "on": True})
    cloud.sent.clear()
    r = client.post("/api/advanced/switch", json={"key": "aux_1", "on": False})
    assert r.status_code == 409
    assert wire(cloud) == []
    assert cloud._aux("aux_1")["state"] == "1"


def test_guest_and_owner_share_the_settle_window(client, cloud):
    # Bug: a guest turns Bubbles (aux_2) on; the cloud lags; the owner's "on" for
    # aux_2 toggles it back off (and vice versa).
    lagging(cloud)
    client.post("/api/spa/bubbles", json={"on": True})
    r = client.post("/api/advanced/switch", json={"key": "aux_2", "on": True})
    assert r.status_code == 200
    assert cmds(cloud) == ["set_aux_2"]
    client.post("/api/advanced/switch", json={"key": "aux_4", "on": True})  # water features
    cloud.sent.clear()
    client.post("/api/pool/water_features", json={"on": True})
    assert wire(cloud) == []
    assert cloud._aux("aux_4")["state"] == "1"


def test_owner_sees_and_respects_a_guest_light_change_while_cloud_lags(client, cloud):
    # Bug: a guest switched the (ICL) light on; the cloud still says off, so the
    # owner's "off" is silently dropped (the library thinks it's off) yet reported
    # as done, and the owner view shows the light off.
    lagging(cloud)
    stale_icl = copy.deepcopy(cloud.devs["icl_info_list"])
    orig = cloud.send_request

    async def icl_lag(url, method="get", **kw):
        r = await orig(url, method, **kw)
        if (kw.get("params") or {}).get("command") == "get_devices":
            body = r.json()
            body["icl_info_list"] = copy.deepcopy(stale_icl)
            return httpx.Response(200, json=body, request=r.request)
        return r

    cloud.send_request = icl_lag
    assert client.post("/api/light", json={"on": True}).status_code == 200
    v = client.get("/api/advanced").json()
    assert next(x for x in v["lights"] if x["key"] == "icl_zone_1")["on"] is True
    cloud.sent.clear()
    r = client.post("/api/advanced/light", json={"key": "icl_zone_1", "on": False})
    assert r.status_code == 409
    assert wire(cloud) == []


# ---- heat pump ---------------------------------------------------------------------

def test_heat_pump_enable_is_explicit_on_not_a_toggle(client, cloud):
    # Bug: heat pump enable sent as a toggle (or with the wrong action), so the
    # owner's "on" could disable it.
    cloud._hp()["heatpumpstatus"] = "off"
    r = client.post("/api/advanced/heatpump", json={"on": True})
    assert r.status_code == 200, r.text
    assert wire(cloud) == [("enable_disable_hpm", {"on_off_action": "on"})]
    assert r.json()["heatpump"]["on"] is True
    cloud.sent.clear()
    r = client.post("/api/advanced/heatpump", json={"on": False})
    assert wire(cloud) == [("enable_disable_hpm", {"on_off_action": "off"})]
    assert r.json()["heatpump"]["on"] is False


def test_heat_pump_already_enabled_sends_nothing(client, cloud):
    # Bug: re-enabling an enabled heat pump on every tap.
    client.post("/api/advanced/heatpump", json={"on": True})
    assert wire(cloud) == []


def test_heat_pump_mode_select(client, cloud):
    # Bug: mode change sent with the wrong command/parameter, or re-sent when unchanged.
    r = client.post("/api/advanced/heatpump", json={"mode": "chill"})
    assert wire(cloud) == [("switch_hpm_mode", {"hpm_mode": "chill"})]
    assert r.json()["heatpump"]["mode"] == "chill"
    cloud.sent.clear()
    client.post("/api/advanced/heatpump", json={"mode": "chill"})
    assert wire(cloud) == []
    assert client.post("/api/advanced/heatpump", json={"mode": "cool"}).status_code == 422


def test_heat_pump_mode_refused_without_chill(cloud):
    # Bug: a chill command sent to a heat-only heat pump.
    cloud._hp()["isChillAvailable"] = False
    c = owner_client(cloud)
    try:
        assert c.get("/api/advanced").json()["heatpump"]["modes"] == []
        assert c.post("/api/advanced/heatpump", json={"mode": "chill"}).status_code == 409
        assert wire(cloud) == []
    finally:
        c.__exit__(None, None, None)


# ---- set points --------------------------------------------------------------------

def test_owner_set_points_use_panel_range_not_guest_limits(client, cloud):
    # Bug: guest limits (spa 103, chill 92) applied to the owner, or the panel's own
    # 34-104 range not enforced.
    r = client.post("/api/advanced/setpoints", json={"spa": 104, "pool_heat": 70, "pool_chill": 99})
    assert r.status_code == 200, r.text
    sent = wire(cloud)
    assert ("setpoint_hpm_temp", {"spaheatsetpointtemp": "104"}) in sent
    assert ("setpoint_hpm_temp", {"poolheatsetpointtemp": "70"}) in sent
    assert ("setpoint_hpm_temp", {"poolchillsetpointtemp": "99"}) in sent
    assert not any(c == "set_temps" for c, _ in sent)
    sp = r.json()["setpoints"]
    assert (sp["spa"]["value"], sp["pool_heat"]["value"], sp["pool_chill"]["value"]) == (104, 70, 99)
    assert (sp["spa"]["min"], sp["spa"]["max"]) == (34, 104)


@pytest.mark.parametrize("body", [{"spa": 105}, {"spa": 33}, {"pool_heat": 33}, {"pool_chill": 105}])
def test_out_of_panel_range_refused(client, cloud, body):
    # Bug: values outside what the panel accepts sent anyway.
    assert client.post("/api/advanced/setpoints", json=body).status_code == 409
    assert wire(cloud) == []


@pytest.mark.parametrize("body", [{"pool_heat": 90}, {"pool_chill": 84}, {"pool_heat": 95, "pool_chill": 94}])
def test_chill_must_stay_above_heat(client, cloud, body):
    # Bug: the owner sets heat >= chill (fixture: heat 84, chill 90), so the heat
    # pump heats and chills against itself.
    assert client.post("/api/advanced/setpoints", json=body).status_code == 409
    assert wire(cloud) == []


def test_spread_of_one_is_allowed(client, cloud):
    # Bug: the guest 5-degree spread applied to the owner.
    assert client.post("/api/advanced/setpoints", json={"pool_heat": 89}).status_code == 200
    assert wire(cloud) == [("setpoint_hpm_temp", {"poolheatsetpointtemp": "89"})]


@pytest.mark.parametrize(
    "start,target,failing",
    [((88, 89), (60, 61), "poolchillsetpointtemp"), ((88, 89), (60, 61), "poolheatsetpointtemp"),
     ((60, 61), (98, 99), "poolchillsetpointtemp"), ((60, 61), (98, 99), "poolheatsetpointtemp")],
    ids=["lower-chill-fails", "lower-heat-fails", "raise-chill-fails", "raise-heat-fails"],
)
def test_failed_owner_write_cannot_invert_heat_and_chill(cloud, start, target, failing):
    # Bug: owner set point writes in the wrong order, so a failure of the second
    # leaves chill below heat on the panel.
    c = owner_client(cloud)
    try:
        assert c.post("/api/advanced/setpoints",
                      json={"pool_heat": start[0], "pool_chill": start[1]}).status_code == 200
        orig = cloud.send_request

        async def boom(url, method="get", **kw):
            if failing in (kw.get("params") or {}):
                raise AqualinkServiceException("Unexpected response: 500")
            return await orig(url, method, **kw)

        cloud.send_request = boom
        r = c.post("/api/advanced/setpoints", json={"pool_heat": target[0], "pool_chill": target[1]})
        assert r.status_code == 502
        heat = int(cloud._home("pool_set_point")["pool_set_point"])
        chill = int(cloud._home("pool_chill_set_point")["pool_chill_set_point"])
        assert chill > heat, (heat, chill)
    finally:
        c.__exit__(None, None, None)


def test_guest_limits_unchanged_while_owner_unlocked(client, cloud):
    # Bug: the owner's wider limits leaking into the guest endpoints.
    assert client.post("/api/spa/setpoint", json={"set_temp": 104}).status_code == 409
    assert client.post("/api/pool/setpoints", json={"heat_set": 85, "chill_set": 86}).status_code == 409
    assert wire(cloud) == []
    s = client.get("/api/state").json()
    assert s["spa"]["set_max"] == 103 and s["pool"]["min_spread"] == 5


# ---- lights ------------------------------------------------------------------------

def test_icl_effect_turns_zone_on_then_sets_any_effect(client, cloud):
    # Bug: an off ICL zone only gets a color (stays dark), or only the guest colors
    # are allowed.
    r = client.post("/api/advanced/light", json={"key": "icl_zone_1", "effect": "Ruby Red"})
    assert r.status_code == 200, r.text
    assert wire(cloud) == [("onoff_iclzone", {"zone_id": "1", "on_off_action": "on"}),
                           ("set_iclzone_color", {"zone_id": "1", "color_id": "8", "dim_level": "100"})]
    light = r.json()["lights"][0]
    assert light["on"] is True and light["effect"] == "Ruby Red"


def test_icl_brightness(client, cloud):
    # Bug: brightness sent with the wrong command or unvalidated.
    assert client.post("/api/advanced/light", json={"key": "icl_zone_1", "brightness": 55}).status_code == 200
    assert wire(cloud) == [("set_iclzone_color", {"zone_id": "1", "dim_level": "55"})]
    cloud.sent.clear()
    assert client.post("/api/advanced/light", json={"key": "icl_zone_1", "brightness": 52}).status_code == 409
    assert client.post("/api/advanced/light", json={"key": "icl_zone_1", "brightness": 0}).status_code == 409
    assert wire(cloud) == []


@pytest.mark.parametrize("body", [
    {"key": "icl_zone_1", "effect": "Off"},
    {"key": "icl_zone_1", "effect": "Plaid"},
    {"key": "icl_zone_1", "on": False, "effect": "Violet"},
    {"key": "aux_1", "effect": "Violet"},  # a plain aux, not a light
    {"key": "icl_zone_1"},
])
def test_bad_light_requests_send_nothing(client, cloud, body):
    # Bug: an unknown effect, "Off" as an effect, or an effect for a non-light
    # reaching the panel.
    assert client.post("/api/advanced/light", json=body).status_code == 409
    assert wire(cloud) == []


def test_icl_on_off_is_explicit(client, cloud):
    # Bug: ICL zone on/off sent through an aux toggle.
    client.post("/api/advanced/light", json={"key": "icl_zone_1", "on": True})
    client.post("/api/advanced/light", json={"key": "icl_zone_1", "on": False})
    assert wire(cloud) == [("onoff_iclzone", {"zone_id": "1", "on_off_action": "on"}),
                           ("onoff_iclzone", {"zone_id": "1", "on_off_action": "off"})]


def _make_aux1(cloud, typ, subtype):
    for a in next(x for x in cloud.devs["devices_screen"] if "aux_1" in x)["aux_1"]:
        if "type" in a:
            a["type"] = typ
        if "subtype" in a:
            a["subtype"] = subtype


def test_relay_color_light_effect_wire_command(cloud):
    # Bug: relay color light effect sent with the wrong id/subtype.
    _make_aux1(cloud, "2", "4")  # Jandy LED WaterColors
    c = owner_client(cloud)
    try:
        light = next(x for x in c.get("/api/advanced").json()["lights"] if x["key"] == "aux_1")
        assert light["type"] == "color" and "Disco Tech" in light["effects"] and "Off" not in light["effects"]
        cloud.sent.clear()
        r = c.post("/api/advanced/light", json={"key": "aux_1", "effect": "Violet"})
        assert wire(cloud) == [("set_light", {"aux": "1", "light": "9", "subtype": "4"})]
        assert next(x for x in r.json()["lights"] if x["key"] == "aux_1")["effect"] == "Violet"
    finally:
        c.__exit__(None, None, None)


def test_dimmable_light_brightness_steps(cloud):
    # Bug: dimmer levels other than 25% steps sent (the panel only takes 0/25/50/75/100).
    _make_aux1(cloud, "1", "0")
    c = owner_client(cloud)
    try:
        assert c.post("/api/advanced/light", json={"key": "aux_1", "brightness": 30}).status_code == 409
        assert c.post("/api/advanced/light", json={"key": "aux_1", "brightness": 75}).status_code == 200
        assert wire(cloud) == [("set_light", {"aux": "1", "light": "75"})]
    finally:
        c.__exit__(None, None, None)


# ---- variable-speed pump -----------------------------------------------------------

def test_no_vsp_on_this_panel_means_none_shown_and_none_sent(client, cloud):
    # Bug: a fake VSP control shown for a panel without one, and commands sent to it.
    assert client.get("/api/advanced").json()["vsp"] == []
    assert client.post("/api/advanced/vsp", json={"key": "vsp_pump_1", "preset": "HI"}).status_code == 409
    assert wire(cloud) == []


def test_vsp_preset_and_stop_wire_commands(cloud):
    # Bug: VSP speed sent with the wrong speed id/slot, or stop sent as a speed.
    backend = asyncio.run(make_backend(cloud))
    svc = PoolService(backend, poll_seconds=3600, stale_retry_seconds=0)

    async def add_pump():
        pump = IaquaVSPump(backend.system, {"name": "vsp_pump_1", "state": "0", "label": "Main", "slot_id": "1"})
        await pump.fetch_speed()
        backend.system.devices["vsp_pump_1"] = pump

    asyncio.run(add_pump())
    with TestClient(create_app(svc, OwnerGate(PIN, b"s"))) as c:
        c.post("/api/advanced/unlock", json={"pin": PIN})
        v = c.get("/api/advanced").json()["vsp"]
        assert v == [{"key": "vsp_pump_1", "label": "Main", "on": False, "role": None,
                      "presets": ["LO", "HI"], "preset": None}]
        cloud.sent.clear()
        r = c.post("/api/advanced/vsp", json={"key": "vsp_pump_1", "preset": "HI"})
        assert r.status_code == 200, r.text
        assert wire(cloud) == [("enable_disable_pump_speedId",
                                {"slot_id": "1", "speed_id": "2", "on_off_action": "on"})]
        assert r.json()["vsp"][0]["preset"] == "HI"
        cloud.sent.clear()
        assert c.post("/api/advanced/vsp", json={"key": "vsp_pump_1", "preset": "TURBO"}).status_code == 409
        c.post("/api/advanced/vsp", json={"key": "vsp_pump_1", "on": False})
        assert wire(cloud) == [("enable_disable_pump_speedId",
                                {"slot_id": "1", "speed_id": "1", "on_off_action": "off"})]


# ---- salt cell ---------------------------------------------------------------------

def swc_calls(cloud):
    return [(c, e) for c, e in cloud.sent if "swc" in c]


def test_salt_config_read_only_for_the_advanced_view_and_cached(client, cloud):
    # Bug: get_swc_config on every guest poll (extra cloud traffic for every viewer),
    # or on every Advanced poll.
    client.get("/api/state")
    assert swc_calls(cloud) == []
    v = client.get("/api/advanced").json()
    client.get("/api/advanced")
    assert swc_calls(cloud) == [("get_swc_config", {})]
    assert v["salt"]["config"]["pool_pct"] == 30 and v["salt"]["config"]["spa_pct"] == 15
    assert v["salt"]["verified"] is False


def test_salt_set_pool_keeps_current_spa(client, cloud):
    # Bug: set_swc_config sent with only one value (the other zeroed by the panel)
    # or with a stale one.
    cloud.swc["spaSWCSP"] = 20
    r = client.post("/api/advanced/salt", json={"pool_pct": 40})
    assert r.status_code == 200, r.text
    assert swc_calls(cloud) == [("get_swc_config", {}), ("set_swc_config", {"poolswcsp": "40", "spaswcsp": "20"})]
    assert r.json()["salt"]["config"]["pool_pct"] == 40


@pytest.mark.parametrize("body", [{"pool_pct": 101}, {"spa_pct": -1}, {"pool_pct": "50"}, {"pool_pct": 5.5},
                                  {"pool_pct": 50, "extra": 1}])
def test_salt_values_validated(client, cloud, body):
    # Bug: out-of-range or non-integer output sent to the salt cell.
    assert client.post("/api/advanced/salt", json=body).status_code == 422
    assert swc_calls(cloud) == []


def test_salt_boost_start_wire_command(client, cloud):
    # Bug: boost started with wrong parameter names/values.
    r = client.post("/api/advanced/salt/boost", json={"action": "start", "hours": 5, "mode": "spillover"})
    assert r.status_code == 200, r.text
    assert swc_calls(cloud)[-1] == ("control_swc_boost",
                                    {"boosthrs": "5", "boostmode": "spillover", "boostcontrol": "start"})
    assert r.json()["salt"]["config"]["boost"]["status"] == "on"


def test_salt_boost_state_machine(client, cloud):
    # Bug: "start" re-sent while a boost runs, "resume" while not paused, etc.
    cloud.swc["boostStatus"] = "on"
    assert client.post("/api/advanced/salt/boost", json={"action": "start", "hours": 5}).status_code == 409
    assert client.post("/api/advanced/salt/boost", json={"action": "resume"}).status_code == 409
    assert [c for c, _ in swc_calls(cloud)].count("control_swc_boost") == 0
    assert client.post("/api/advanced/salt/boost", json={"action": "pause"}).status_code == 200
    assert swc_calls(cloud)[-1] == ("control_swc_boost",
                                    {"boosthrs": "24", "boostmode": "pool", "boostcontrol": "pause"})
    assert client.post("/api/advanced/salt/boost", json={"action": "stop"}).status_code == 200
    assert swc_calls(cloud)[-1] == ("control_swc_boost",
                                    {"boosthrs": "24", "boostmode": "pool", "boostcontrol": "stop"})


@pytest.mark.parametrize("body", [{"action": "start", "hours": 25}, {"action": "start", "hours": 0},
                                  {"action": "start", "hours": 3, "mode": "spa"}, {"action": "reboot"}])
def test_salt_boost_params_validated(client, cloud, body):
    # Bug: a boost longer than 24 h, zero hours, or an unknown mode/action sent.
    assert client.post("/api/advanced/salt/boost", json=body).status_code == 422
    assert swc_calls(cloud) == []


def test_salt_boost_refused_when_dip_switch_disables_it(client, cloud):
    # Bug: boost started on a cell whose DIP switch has boost disabled.
    cloud.swc["boostDipSwitch"] = "off"
    assert client.post("/api/advanced/salt/boost", json={"action": "start", "hours": 2}).status_code == 409
    assert [c for c, _ in swc_calls(cloud)] == ["get_swc_config"]


@pytest.mark.parametrize("reply", [
    {"is_error": True},
    {"response": "SECRET-detail SN1"},
    {"device_status": "offline"},
    {"poolSWCSP": None, "spaSWCSP": None},
], ids=["is_error", "response", "offline", "no-setpoints"])
def test_salt_error_reply_is_a_generic_failure(client, cloud, reply):
    # Bug: an error/offline/empty reply treated as a valid config (then used to fill
    # in the other value), or its text echoed to the client.
    cloud.swc.update(reply)
    r = client.post("/api/advanced/salt", json={"pool_pct": 40})
    assert r.status_code == 502
    assert "SECRET" not in r.text and "SN1" not in r.text
    assert [c for c, _ in swc_calls(cloud)] == ["get_swc_config"]


def test_no_salt_cell_means_no_salt_commands(cloud):
    # Bug: salt commands sent to a panel without a salt cell.
    cloud._home("swc_info")["swc_info"]["isswcPresent"] = False
    c = owner_client(cloud)
    try:
        assert c.get("/api/advanced").json()["salt"] is None
        assert c.post("/api/advanced/salt", json={"pool_pct": 40}).status_code == 409
        assert swc_calls(cloud) == []
    finally:
        c.__exit__(None, None, None)


# ---- errors / exposure -------------------------------------------------------------

def test_library_error_text_never_reaches_the_owner_page(client, cloud):
    # Bug: exception text with session IDs/serials echoed in a 502 body.
    orig = cloud.send_request

    async def leaky(url, method="get", **kw):
        if (kw.get("params") or {}).get("command", "").startswith("set_aux"):
            raise AqualinkServiceException("GET https://p-api/x?sessionID=SECRET123&serial=SN1")
        return await orig(url, method, **kw)

    cloud.send_request = leaky
    r = client.post("/api/advanced/switch", json={"key": "aux_1", "on": True})
    assert r.status_code == 502
    assert "SECRET123" not in r.text and "SN1" not in r.text


def test_state_reports_only_the_gate_status(client):
    # Bug: device controls leaking into the guest /api/state.
    s = client.get("/api/state").json()
    assert s["advanced"] == {"enabled": True, "unlocked": True}
    assert "switches" not in str(s)
