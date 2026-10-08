"""The owner's read-only "Equipment status" view: app/equipment.py, its wiring in
the iaqualink backend (against the FakeCloud built from recorded panel replies),
and how /api/state exposes it. Each test names the bug it would catch."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from iaqualink.system import AqualinkSystem

from app import equipment
from app.backends.base import BackendError, Snapshot
from app.backends.mock import MockBackend
from app.main import create_app
from app.service import PoolService
from tests.test_iaqualink_backend import FakeCloud, make_backend


def run(coro):
    return asyncio.run(coro)


def rows(eq):
    return {r["id"]: r for g in eq["groups"] for r in g["rows"]}


def hexed(text: bytes, prefix: bytes = b"\x00\x01\x7f") -> str:
    return "AQU='70','" + " ".join(f"{b:02X}" for b in prefix + text) + "'"


async def iaqua_service(cloud=None, **kw):
    cloud = cloud or FakeCloud()
    b = await make_backend(cloud)
    s = PoolService(b, poll_seconds=3600, stale_retry_seconds=0, **kw)
    await s._refresh()
    return cloud, b, s


# ---- builder ---------------------------------------------------------------------

@pytest.mark.parametrize("status", ["noflow", "lowsalt", "offline", "Check Cell"])
def test_undocumented_swc_status_is_flagged_and_shown_verbatim(status):
    # Bug: swc status 'noflow' (a cell fault) not flagged, or rewritten so the owner
    # can't look it up.
    r = rows(equipment.build({"swc_info": {"isswcPresent": True, "swcPoolValue": 25, "swcPoolStatus": status}}))
    assert r["swc_status"]["warn"] is True
    assert status in r["swc_status"]["value"]


@pytest.mark.parametrize("status,shown", [("standby", "Standby"), ("running", "Running"),
                                          ("boosting", "Boosting"), ("boostpaused", "Boost paused")])
def test_documented_swc_statuses_are_not_alarms(status, shown):
    # Bug: a normal salt cell state (e.g. "boostpaused") painted as a fault.
    r = rows(equipment.build({"swc_info": {"isswcPresent": True, "swcPoolValue": 25, "swcPoolStatus": status}}))
    assert r["swc_status"] == {"id": "swc_status", "label": "Salt cell", "value": shown, "warn": False}
    assert r["swc_output"]["value"] == "25%"


def test_blank_fields_are_omitted_not_shown_as_empty_rows():
    # Bug: blank salinity/pH/ORP/solar/cover shown as empty rows (or a header with
    # nothing under it).
    eq = equipment.build({"spa_salinity": "", "pool_salinity": "", "orp": "", "ph": "",
                          "solar_heater": "", "cover_pool": "", "spa_pump": "", "pool_pump": "1"})
    assert {g["id"] for g in eq["groups"]} == {"pumps"}
    assert all(r["value"] for r in rows(eq).values())
    assert "solar_heater" not in rows(eq)


def test_chemistry_shown_with_units_when_the_panel_reports_it():
    # Bug: salinity/pH/ORP dropped (or unit-less) on panels that do report them.
    r = rows(equipment.build({"pool_salinity": "3200", "ph": "7.4", "orp": "650", "spa_salinity": ""}))
    assert r["pool_salinity"]["value"] == "3200 ppm"
    assert r["ph"]["value"] == "7.4"
    assert r["orp"]["value"] == "650 mV"
    assert "spa_salinity" not in r


@pytest.mark.parametrize("raw,shown", [("0", "Off"), ("1", "Heating"), ("3", "On, idle")])
def test_heater_codes_in_plain_language(raw, shown):
    # Bug: "3" (enabled, not firing) shown as Heating, or raw codes shown.
    assert rows(equipment.build({"pool_heater": raw}))["pool_heater"]["value"] == shown


def test_missing_home_dict_gives_empty_view_not_a_crash():
    # Bug: equipment block crashes /api/state before the first good get_home reply.
    assert equipment.build(None) == {"groups": []}
    assert equipment.build({"swc_info": "garbage", "heatpump_info": None}) == {"groups": []}


def test_abnormal_states_are_flagged():
    # Bug: freeze protection active, a heat pump alert, or an offline controller
    # rendered like normal rows.
    r = rows(equipment.build({"freeze_protection": "1"}, status="Service", heatpump_alert="5"))
    assert r["freeze_protection"] == {"id": "freeze_protection", "label": "Freeze protection",
                                      "value": "Active", "warn": True}
    assert r["heatpump_alert"]["warn"] is True and r["heatpump_alert"]["value"] == "High pressure (cooling)"
    assert r["status"]["value"] == "Service" and r["status"]["warn"] is True
    ok = rows(equipment.build({"freeze_protection": "0"}, status="Online"))
    assert not any(x["warn"] for x in ok.values())


@pytest.mark.parametrize("raw,covered,value", [("1", True, "Closed (covered)"), ("0", False, "Open (uncovered)"),
                                               ("", None, None), ("7", None, "Unknown (7)")])
def test_cover_reading(raw, covered, value):
    # Bug: an unknown/blank cover reading treated as covered (would show the
    # Spillover hint for no reason), or the reading dropped.
    assert equipment.pool_covered({"cover_pool": raw}) is covered
    assert rows(equipment.build({"cover_pool": raw})).get("cover", {}).get("value") == value


def test_panel_model_decoded_from_trailing_ascii():
    # Bug: model/firmware string lost, or binary junk before it shown.
    assert equipment.panel_model(hexed(b"B0316823 RS-4 Combo")) == "B0316823 RS-4 Combo"
    assert equipment.panel_model(hexed(b"B0316823 RS-4 Combo\x00\x00")) == "B0316823 RS-4 Combo"


@pytest.mark.parametrize("resp", ["AQU=XXXX", "AQU='71','B0316823 RS-4 Combo'", "AQU='70','zz yy'",
                                  hexed(b"\x01\x02"), hexed(b"12 3"), None, 42])
def test_panel_model_skips_anything_it_cannot_decode(resp):
    # Bug: a malformed/other response field shown as a "model" (or crashing).
    assert equipment.panel_model(resp) is None


def test_serial_and_account_never_leak_into_equipment():
    # Bug: serial leaks into equipment (decoded model string or a panel label that
    # happens to contain it).
    serial, user = "ABCD12345678", "owner@example.com"
    eq = equipment.build({"response": hexed(b"RS-4 ABCD12345678"), "relay_count": "4"},
                         aux_on=["Pump ABCD12345678", "owner@example.com notes", "Cleaner"],
                         secrets=[serial, user])
    text = json.dumps(eq)
    assert serial not in text and user not in text
    assert rows(eq)["relays"]["value"] == "4"  # the rest survives


# ---- iaqualink backend + service ------------------------------------------------

def test_fixture_panel_exposed_via_state():
    # Bug: equipment rows not wired from the real get_home reply (swc_info and
    # heatpump_info are ignored by iaqualink-py), or blank fixture fields shown.
    async def go():
        _, _, s = await iaqua_service()
        return s.state()

    st = run(go())
    r = rows(st["equipment"])
    assert r["heatpump"]["value"] == "On, idle"          # fixture: "enabled"
    assert r["swc_status"]["value"] == "offline (possible fault)" and r["swc_status"]["warn"]
    assert r["swc_output"]["value"] == "0%"
    assert r["filter_pump"]["value"] == "On"
    assert r["status"] == {"id": "status", "label": "Controller", "value": "Online", "warn": False}
    assert "spa_mode" not in r and "cover" not in r and "ph" not in r   # blank in fixture
    assert all(x["value"] for x in r.values())
    assert st["pool"]["covered"] is None and st["pool"]["cover_hint"] is False


def test_refresh_for_equipment_sends_no_commands():
    # Bug: the read-only view sends anything other than the usual get_* polls.
    async def go():
        c, _, s = await iaqua_service()
        c.sent.clear()
        await s._refresh()
        s.state()
        return [cmd for cmd, _ in c.sent]

    sent = run(go())
    assert sent and all(cmd.startswith("get_") for cmd in sent), sent


def test_incomplete_reply_does_not_replace_equipment_view():
    # Bug: a get_home with empty system_type (which the library ignores) still
    # overwrote the equipment rows with its partial data.
    async def go():
        c, _, s = await iaqua_service()
        c._home("system_type")["system_type"] = ""
        c._home("swc_info")["swc_info"]["swcPoolStatus"] = "running"
        with pytest.raises(BackendError):
            await s._refresh()
        return s.state()

    assert rows(run(go())["equipment"])["swc_status"]["value"] == "offline (possible fault)"


def test_offline_panel_shows_status_not_stale_rows():
    # Bug: an Offline panel kept showing its last pump/heater states as if current,
    # or the Controller row still said Online.
    async def go():
        c, _, s = await iaqua_service()
        c._home("status")["status"] = "Offline"
        await s._refresh()
        return s.state()

    r = rows(run(go())["equipment"])
    assert set(r) == {"status"}
    assert r["status"]["value"] == "Offline" and r["status"]["warn"] is True


def test_unreachable_cloud_flags_controller_and_keeps_last_rows():
    # Bug: when the cloud can't be reached the view still said "Online".
    async def go():
        c, _, s = await iaqua_service()

        async def down(*a, **k):
            raise TimeoutError

        c.send_request = down
        with pytest.raises(BackendError):
            await s._refresh()
        return s.state()

    r = rows(run(go())["equipment"])
    assert r["status"] == {"id": "status", "label": "Controller", "value": "Not reachable", "warn": True}
    assert r["heatpump"]["value"] == "On, idle"


def test_cover_hint_follows_cover_pool_and_can_be_turned_off():
    # Bug: hint shown while uncovered/unknown, or the POOL_COVER_HINT=0 setting ignored.
    async def go(raw, **kw):
        c = FakeCloud()
        c._home("cover_pool")["cover_pool"] = raw
        _, _, s = await iaqua_service(c, **kw)
        return s.state()

    covered = run(go("1"))
    assert covered["pool"]["covered"] is True and covered["pool"]["cover_hint"] is True
    assert rows(covered["equipment"])["cover"]["value"] == "Closed (covered)"
    assert run(go("0"))["pool"]["cover_hint"] is False
    off = run(go("1", cover_hint=False))
    assert off["pool"]["covered"] is True and off["pool"]["cover_hint"] is False


def test_heatpump_alert_from_library_device_is_flagged():
    # Bug: a heat pump alert the library parsed (from an HPM echo) not shown.
    async def go():
        _, b, s = await iaqua_service()
        b.system._upsert_heatpump({"isHPMPresent": True, "HPMstatus": "on", "HPMmode": "heat",
                                   "isChillAvailable": True, "alert_message": "16"})
        await s._refresh()
        return s.state()

    r = rows(run(go())["equipment"])
    assert r["heatpump_alert"] == {"id": "heatpump_alert", "label": "Heat pump alert",
                                   "value": "Fan motor error", "warn": True}


def test_aux_and_scene_labels_that_are_on_are_listed():
    # Bug: aux circuits / OneTouch scenes running on the panel not shown.
    async def go():
        c = FakeCloud()
        c._aux("aux_1")["state"] = "1"
        next(x for x in c.otc["onetouch_screen"] if "onetouch_6" in x)["onetouch_6"][1]["state"] = "1"
        _, _, s = await iaqua_service(c)
        return s.state()

    r = rows(run(go())["equipment"])
    assert r["aux_on"]["value"] == "Cleaner"
    assert r["scenes_on"]["value"] == "Water Falls"


def test_firmware_and_model_from_get_home():
    # Bug: top-level attached_system_fw_version / the AQU='70' model not picked up.
    async def go():
        c = FakeCloud()
        c.home["attached_system_fw_version"] = "4.39"
        c._home("response")["response"] = hexed(b"B0316823 RS-4 Combo")
        _, _, s = await iaqua_service(c)
        return s.state()

    r = rows(run(go())["equipment"])
    assert r["firmware"]["value"] == "4.39"
    assert r["model"]["value"] == "B0316823 RS-4 Combo"


def test_serial_never_leaks_through_state():
    # Bug: the panel serial reaches /api/state via the equipment view.
    async def go():
        c = FakeCloud()
        c._home("response")["response"] = hexed(b"RS-4 SN1234567")
        c._aux("aux_1")["state"] = "1"
        c._aux("aux_1")["label"] = "SN1234567"
        b = await make_backend(c)
        b.system = AqualinkSystem.from_data(c, {"device_type": "iaqua", "serial_number": "SN1234567", "name": "P"})
        b._watch_parses(b.system)
        s = PoolService(b, poll_seconds=3600)
        await s._refresh()
        return s.state()

    assert "SN1234567" not in json.dumps(run(go()))


def test_equipment_failure_never_breaks_guest_state(monkeypatch):
    # Bug: an exception in the advanced view takes down /api/state for guests.
    def boom(*a, **k):
        raise RuntimeError("bad panel data")

    async def go():
        _, _, s = await iaqua_service()
        monkeypatch.setattr(equipment, "build", boom)
        await s._refresh()
        return s.state()

    st = run(go())
    assert st["connected"] is True and st["equipment"] == {"groups": []}


# ---- mock + HTTP -----------------------------------------------------------------

def test_mock_equipment_is_complete_and_tracks_switches():
    # Bug: the mock (used for UI work) returns no/blank equipment rows, or rows that
    # don't follow its own switches.
    backend = MockBackend()
    with TestClient(create_app(PoolService(backend, poll_seconds=0))) as c:
        st = c.get("/api/state").json()
        r = rows(st["equipment"])
        assert {g["id"] for g in st["equipment"]["groups"]} >= {"cover", "pumps", "salt", "panel"}
        assert all(x["value"] for x in r.values()) and not any(x["warn"] for x in r.values())
        assert r["spa_mode"]["value"] == "Off"
        c.post("/api/mode", json={"mode": "spa"})
        assert rows(c.get("/api/state").json()["equipment"])["spa_mode"]["value"] == "On"


def test_state_shape_without_backend_data():
    # Bug: a fresh service (no refresh yet / backend down) returns no "equipment"
    # key, so the page's section breaks.
    s = PoolService(MockBackend(), poll_seconds=3600)
    s.snap = Snapshot()
    eq = s.state()["equipment"]
    assert eq["groups"][0]["id"] == "panel"
    assert rows(eq)["status"]["value"] == "Not reachable"
