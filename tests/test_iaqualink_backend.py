"""The real iaqualink-py library against a fake iAqualink cloud built from recorded
panel responses (tests/fixtures/iaqua). Catches wrong device mapping and wrong wire
commands — the class of bug that toggles the wrong thing on a real pool."""

import copy
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from iaqualink.client import AqualinkClient
from iaqualink.system import AqualinkSystem

from app.backends.iaqualink import DeviceMap, IAqualinkBackend
from app.main import create_app
from app.service import PoolService

FX = Path(__file__).parent / "fixtures" / "iaqua"
HOME_TOGGLES = {"pool_pump", "spa_pump", "spa_heater", "pool_heater"}


class FakeCloud(AqualinkClient):
    """Stateful: set_* commands flip the state the next get_home/get_devices reports,
    like the panel does."""

    def __init__(self, chill: bool = True):
        super().__init__("u", "p")
        self.client_id, self.id_token, self.country = "S", "t", "us"
        self.home = json.loads((FX / "session_get_home.json").read_text())
        self.devs = json.loads((FX / "session_get_devices.json").read_text())
        self.otc = json.loads((FX / "session_get_onetouch.json").read_text())
        for x in self.home["home_screen"]:
            if "system_type" in x:
                x["system_type"] = "0"  # pool + spa
            if "spa_temp" in x:
                x["spa_temp"] = "99"
            if "spa_set_point" in x:
                x["spa_set_point"] = "100"
            if "pool_set_point" in x:
                x["pool_set_point"] = "88"
            if "pool_chill_set_point" in x and chill:
                x["pool_chill_set_point"] = "83"
            if "heatpump_info" in x:
                x["heatpump_info"].update(isChillAvailable=chill, heatpumptype="2-wired", heatpumpstatus="enabled")
        self.sent: list[tuple[str, dict]] = []

    def _home(self, key):
        return next(x for x in self.home["home_screen"] if key in x)

    def _aux(self, key):
        entry = next(x for x in self.devs["devices_screen"] if key in x)[key]
        return next(a for a in entry if "state" in a)

    async def send_request(self, url, method="get", **kw):
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        q.update({k: str(v) for k, v in (kw.get("params") or {}).items()})
        cmd = q.get("command", "?")
        extra = {k: v for k, v in q.items() if k not in ("actionID", "command", "serial", "sessionID", "country")}
        self.sent.append((cmd, extra))
        name = cmd.removeprefix("set_")
        if name in HOME_TOGGLES:
            h = self._home(name)
            h[name] = "0" if h[name] in ("1", "3") else "1"
        elif cmd.startswith("set_aux_"):
            a = self._aux("aux_" + cmd.removeprefix("set_aux_"))
            a["state"] = "0" if a["state"] == "1" else "1"
        elif cmd == "set_temps":
            if "temp1" in extra:
                self._home("spa_set_point")["spa_set_point"] = extra["temp1"]
            if "temp2" in extra:
                self._home("pool_set_point")["pool_set_point"] = extra["temp2"]
        elif cmd == "setpoint_hpm_temp":
            for param, key in [("poolheatsetpointtemp", "pool_set_point"),
                               ("spaheatsetpointtemp", "spa_set_point"),
                               ("poolchillsetpointtemp", "pool_chill_set_point")]:
                if param in extra:
                    self._home(key)[key] = extra[param]
        if cmd == "get_onetouch" or cmd.startswith("set_onetouch"):
            body = self.otc
        elif cmd == "get_devices" or cmd.startswith("set_aux") or cmd == "set_light":
            body = self.devs
        elif cmd == "setpoint_hpm_temp":
            body = {"response": "success", "isHPMPresent": True, "isChillAvailable": True}
        else:
            body = self.home
        return httpx.Response(200, json=copy.deepcopy(body), request=httpx.Request(method, url))


async def make_backend(cloud: FakeCloud, **mapping) -> IAqualinkBackend:
    b = IAqualinkBackend("u", "p", DeviceMap(**mapping))
    b.client = cloud
    b.system = AqualinkSystem.from_data(cloud, {"device_type": "iaqua", "serial_number": "SN1", "name": "Pool"})
    return b


@pytest.fixture
def cloud():
    return FakeCloud()


@pytest.fixture
def client(cloud):
    import asyncio

    backend = asyncio.run(make_backend(cloud))
    with TestClient(create_app(PoolService(backend, poll_seconds=3600))) as c:
        cloud.sent.clear()
        yield c


def commands(cloud):
    return [c for c, _ in cloud.sent if not c.startswith("get_")]


def test_state_maps_fixture_devices(client):
    # Bug: wrong device keys -> temps/set points/toggles silently read as empty.
    s = client.get("/api/state").json()
    assert s["connected"] is True
    assert s["mode"] == "pool"
    assert s["spa"]["current_temp"] == 99 and s["spa"]["set_temp"] == 100
    assert s["pool"]["current_temp"] == 89 and s["pool"]["heat_set"] == 88
    assert s["pool"]["chill_set"] == 83 and s["pool"]["chill_supported"] is True
    # The fixture has no "Spillover" device.
    assert s["pool"]["spillover_available"] is False


def test_spa_mode_with_pump_running_only_toggles_spa_and_heater(client, cloud):
    # Bug: re-sending set_pool_pump (a toggle) while the pump runs turns it OFF.
    r = client.post("/api/mode", json={"mode": "spa"})
    assert r.status_code == 200, r.text
    assert commands(cloud) == ["set_spa_pump", "set_spa_heater"]
    assert r.json()["mode"] == "spa"


def test_spa_mode_starts_pump_when_off(client, cloud):
    # Bug: spa mode without starting the filter pump first.
    cloud._home("pool_pump")["pool_pump"] = "0"
    client.post("/api/mode", json={"mode": "spa"})
    assert commands(cloud) == ["set_pool_pump", "set_spa_pump", "set_spa_heater"]


def test_back_to_pool_mode(client, cloud):
    client.post("/api/mode", json={"mode": "spa"})
    cloud.sent.clear()
    r = client.post("/api/mode", json={"mode": "pool"})
    assert commands(cloud) == ["set_spa_heater", "set_spa_pump"]
    assert r.json()["mode"] == "pool"


def test_bubbles_is_aux_2(client, cloud):
    # Bug: bubbles driving some other relay.
    r = client.post("/api/spa/bubbles", json={"on": True})
    assert commands(cloud) == ["set_aux_2"]
    assert r.json()["spa"]["bubbles"] is True


def test_water_features_found_by_label_aux_v1(client, cloud):
    # Bug: "Aux V1" assumed to be a key; in this panel it is aux_4.
    r = client.post("/api/pool/water_features", json={"on": True})
    assert commands(cloud) == ["set_aux_4"]
    assert r.json()["pool"]["water_features"] is True


def test_chill_write_uses_hpm_command_not_set_temps(client, cloud):
    # Bug (iaqualink 0.7.0, flz/iaqualink-py#274): chill written via set_temps
    # temp2, overwriting the pool HEAT set point.
    r = client.post("/api/pool/setpoints", json={"heat_set": 90, "chill_set": 84})
    assert r.status_code == 200, r.text
    sent = [(c, e) for c, e in cloud.sent if not c.startswith("get_")]
    assert ("setpoint_hpm_temp", {"poolchillsetpointtemp": "84"}) in sent
    assert not any(c == "set_temps" for c, _ in sent)
    assert r.json()["pool"]["heat_set"] == 90
    assert r.json()["pool"]["chill_set"] == 84


def test_spillover_by_onetouch_label(cloud):
    # Bug: spillover implemented as a OneTouch scene on the panel can't be found.
    import asyncio

    backend = asyncio.run(make_backend(cloud, spillover="Water Falls"))
    with TestClient(create_app(PoolService(backend, poll_seconds=3600))) as c:
        cloud.sent.clear()
        c.post("/api/pool/spillover", json={"on": True})
    assert commands(cloud) == ["set_onetouch_6"]


def test_light_colors_present_white_first(client):
    # Bug: the panel's white is named "Alpine White"/"Cloud White"; the UI's default
    # "White" must map onto it, and be the first chip.
    colors = client.get("/api/state").json()["light"]["colors"]
    assert colors[0] == "White"
    assert "Alpine White" not in colors and "Off" not in colors
