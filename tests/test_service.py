import pytest
from fastapi.testclient import TestClient

from app.backends.mock import MockBackend
from app.main import create_app
from app.service import PoolService


@pytest.fixture
def backend():
    return MockBackend()


@pytest.fixture
def client(backend):
    svc = PoolService(backend, poll_seconds=3600)
    with TestClient(create_app(svc)) as c:
        yield c


def switch_calls(backend):
    return [c[1:] for c in backend.calls if c[0] == "set_switch"]


class TestSpaMode:
    def test_turns_on_pump_then_spa_then_heater_when_all_off(self, client, backend):
        # Bug: spa mode engaged with the pump off (no flow to the heater), or steps
        # sent in an order that fires the heater before the valves move.
        backend.state.filter_pump = False
        r = client.post("/api/mode", json={"mode": "spa"})
        assert r.status_code == 200
        assert switch_calls(backend) == [
            ("filter_pump", True),
            ("spa_mode", True),
            ("spa_heater", True),
        ]
        assert r.json()["mode"] == "spa"

    def test_does_not_toggle_what_is_already_on(self, client, backend):
        # Bug: Jandy commands are toggles, so re-sending "pump on" while the pump
        # is running would switch it OFF.
        backend.state.filter_pump = True
        backend.state.spa_mode = True
        client.post("/api/mode", json={"mode": "spa"})
        assert switch_calls(backend) == [("spa_heater", True)]

    def test_decides_from_fresh_state_not_the_cache(self, client, backend):
        # Bug: the cache says the pump is off (someone turned it on at the panel
        # since the last poll) and we toggle it off.
        backend.state.filter_pump = True  # changed behind the service's back
        client.app.state.svc.snap.filter_pump = False
        client.post("/api/mode", json={"mode": "spa"})
        assert ("filter_pump", True) not in switch_calls(backend)
        assert backend.state.filter_pump is True

    def test_pool_mode_turns_off_spa_heat_and_spa_but_leaves_pump(self, client, backend):
        # Bug: leaving spa mode with the spa heater still enabled, or killing the pump.
        backend.state.spa_mode = backend.state.spa_heater = True
        r = client.post("/api/mode", json={"mode": "pool"})
        assert switch_calls(backend) == [("spa_heater", False), ("spa_mode", False)]
        assert backend.state.filter_pump is True
        assert r.json()["mode"] == "pool"

    def test_bubbles_maps_to_its_own_switch(self, client, backend):
        # Bug: bubbles wired to the wrong switch (checked again at device level in
        # the iaqualink backend tests).
        client.post("/api/spa/bubbles", json={"on": True})
        assert switch_calls(backend) == [("bubbles", True)]


class TestSpaSetpoint:
    def test_rejects_above_103(self, client, backend):
        # Bug: a guest (or a crafted request) setting the spa hotter than 103.
        r = client.post("/api/spa/setpoint", json={"set_temp": 104})
        assert r.status_code == 409
        assert not backend.calls

    def test_accepts_103(self, client, backend):
        # Bug: off-by-one making the advertised max unreachable.
        assert client.post("/api/spa/setpoint", json={"set_temp": 103}).status_code == 200
        assert backend.state.spa_set == 103


class TestPoolSetpoints:
    # Heat is the low set point, chill the high one: heat >= 82, chill <= 92,
    # chill >= heat + 5.
    @pytest.mark.parametrize(
        "heat,chill",
        [(81, 90), (85, 93), (86, 90), (88, 92), (90, 85)],
        ids=["heat-below-82", "chill-above-92", "spread-4", "spread-4-at-ceiling", "old-orientation"],
    )
    def test_rejects_out_of_rules(self, client, backend, heat, chill):
        # Bug: server trusting the sliders, letting heat < 82, chill > 92 or a
        # spread under 5 through -- or still using the old heat-above-chill rule.
        r = client.post("/api/pool/setpoints", json={"heat_set": heat, "chill_set": chill})
        assert r.status_code == 409, r.text
        assert not backend.calls

    @pytest.mark.parametrize("heat,chill", [(82, 87), (87, 92)])
    def test_accepts_extremes(self, client, backend, heat, chill):
        # Bug: off-by-one at the exact limits (5 degree spread at floor and ceiling).
        r = client.post("/api/pool/setpoints", json={"heat_set": heat, "chill_set": chill})
        assert r.status_code == 200, r.text
        assert backend.calls == [("set_pool_setpoints", heat, chill)]

    def test_advertised_ranges_match_rules(self, client):
        # Bug: sliders offered values the server then rejects (ranges not derived
        # from the same limits as the rules).
        p = client.get("/api/state").json()["pool"]
        assert (p["heat_min"], p["heat_max"], p["chill_min"], p["chill_max"]) == (82, 87, 87, 92)

    def test_chill_required_when_supported(self, client, backend):
        # Bug: heat-only request silently leaving chill where it violates the spread.
        r = client.post("/api/pool/setpoints", json={"heat_set": 85})
        assert r.status_code == 409

    def test_heat_only_when_no_chiller(self, client, backend):
        # Bug: controllers without a chiller unable to set the heat set point at all,
        # or the 82 floor not applying to them.
        backend.state.pool_chill_set = None
        client.app.state.svc.snap.pool_chill_set = None
        r = client.post("/api/pool/setpoints", json={"heat_set": 90})
        assert r.status_code == 200
        assert r.json()["pool"]["chill_supported"] is False
        assert client.post("/api/pool/setpoints", json={"heat_set": 81}).status_code == 409


class TestSpilloverWaterFeatures:
    def test_spillover_blocked_while_water_features_on(self, client, backend):
        # Bug: both running at once (the UI greys it out, the server must refuse too).
        backend.state.water_features = True
        r = client.post("/api/pool/spillover", json={"on": True})
        assert r.status_code == 409
        assert not switch_calls(backend)

    def test_water_features_blocked_while_spillover_on(self, client, backend):
        backend.state.spillover = True
        r = client.post("/api/pool/water_features", json={"on": True})
        assert r.status_code == 409
        assert not switch_calls(backend)

    def test_turning_off_is_always_allowed(self, client, backend):
        # Bug: the mutual-exclusion check also blocking the way out of the bad state.
        backend.state.spillover = backend.state.water_features = True
        assert client.post("/api/pool/spillover", json={"on": False}).status_code == 200
        assert client.post("/api/pool/water_features", json={"on": False}).status_code == 200


class TestLight:
    def test_light_on_starts_white(self, client, backend):
        # Bug: light coming back on in the last guest's color instead of white.
        backend.state.light_color = "Red"
        client.post("/api/light", json={"on": True})
        assert backend.calls == [("set_light_color", "White")]
        assert backend.state.light_on

    def test_reported_color_is_white_while_off(self, client, backend):
        # Bug: color control showing a stale color for a light that's off.
        backend.state.light_color = "Red"
        r = client.post("/api/light", json={"on": False})
        assert r.json()["light"]["color"] == "White"

    def test_unknown_color_rejected(self, client, backend):
        r = client.post("/api/light/color", json={"color": "Plaid"})
        assert r.status_code == 409
        assert not backend.calls


def test_air_temp_reported(client):
    # Bug: the new Air Temp field missing from state, so the UI always shows "--".
    assert client.get("/api/state").json()["air_temp"] == 78


def test_controller_down_is_reported_not_crashing():
    # Bug: one failed poll at startup taking the whole web UI down.
    from app.backends.base import BackendError

    class Down(MockBackend):
        async def refresh(self):
            raise BackendError("timeout")

    with TestClient(create_app(PoolService(Down(), poll_seconds=3600))) as c:
        r = c.get("/api/state")
        assert r.status_code == 200
        assert r.json()["connected"] is False
        assert c.post("/api/spa/bubbles", json={"on": True}).status_code == 502


@pytest.mark.parametrize(
    "env",
    [{"POOL_MIN_SPREAD": "-3"}, {"POOL_HEAT_MIN": "95"}, {"POOL_MIN_SPREAD": "20"}, {"SPA_MIN": "110"}],
    ids=["negative-spread", "heat-min-above-max", "spread-too-wide", "spa-inverted"],
)
def test_contradictory_limits_refuse_to_start(monkeypatch, env):
    # Bug: inverted limits silently advertised impossible slider ranges (every set
    # point rejected), and a negative spread let chill go below heat.
    from app.main import limits_from_env

    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(SystemExit):
        limits_from_env()


class TestViewerDrivenPolling:
    """The iAqualink cloud is only polled while someone has the page open."""

    @pytest.fixture
    def clock(self, monkeypatch):
        import app.service as service

        t = {"now": 1000.0}
        monkeypatch.setattr(service.time, "monotonic", lambda: t["now"])
        return t

    def make(self):
        backend = MockBackend()
        refreshes = []
        orig = backend.refresh

        async def counting():
            refreshes.append(1)
            return await orig()

        backend.refresh = counting
        return PoolService(backend, poll_seconds=15, idle_seconds=60), refreshes

    def test_no_polling_while_nobody_is_watching(self, clock):
        # Bug: polling Jandy's cloud around the clock with no page open.
        import asyncio

        svc, refreshes = self.make()

        async def go():
            await svc._refresh()
            refreshes.clear()
            clock["now"] += 3600
            # What one poll-loop tick does:
            if svc._viewed_recently():
                await svc._refresh_if_due()

        asyncio.run(go())
        assert refreshes == []

    def test_first_view_after_idle_gets_fresh_data(self, clock):
        # Bug: opening the page after an hour shows hour-old temperatures until
        # the next background poll.
        import asyncio

        svc, refreshes = self.make()

        async def go():
            await svc._refresh()
            refreshes.clear()
            clock["now"] += 3600
            await svc.viewer_state()

        asyncio.run(go())
        assert refreshes == [1]

    def test_page_polls_do_not_hit_the_cloud_faster_than_poll_seconds(self, clock):
        # Bug: every 5 s page poll (times several phones) becoming a cloud request.
        import asyncio

        svc, refreshes = self.make()

        async def go():
            for _ in range(12):  # one minute of 5 s polls from one phone, two phones
                await svc.viewer_state()
                await svc.viewer_state()
                clock["now"] += 5

        asyncio.run(go())
        assert len(refreshes) == 4  # t=0, 15, 30, 45

    def test_keeps_polling_while_watched(self, clock):
        # Bug: water temp never updating while a guest keeps the page open.
        import asyncio

        svc, refreshes = self.make()

        async def go():
            await svc.viewer_state()
            refreshes.clear()
            clock["now"] += 20
            if svc._viewed_recently():
                await svc._refresh_if_due()

        asyncio.run(go())
        assert refreshes == [1]
