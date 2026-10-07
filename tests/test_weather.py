"""Weather parsing, summary and caching, against a recorded Open-Meteo response.

The fixture (tests/fixtures/weather/open_meteo.json) is a real response for zip 28411
recorded 2026-10-07 ~13:45 EDT: a dry, clear afternoon peaking near 76°F.
"""

import asyncio
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import weather
from app.weather import Config, WeatherService, build_payload, describe, summarize

FIXTURE = Path(__file__).parent / "fixtures" / "weather" / "open_meteo.json"
# 2026-10-07 17:50 UTC == 13:50 EDT, a few minutes after the fixture was recorded.
NOW_UTC = datetime(2026, 10, 7, 17, 50, tzinfo=timezone.utc).timestamp()
NOW_LOCAL = datetime(2026, 10, 7, 13, 50)
# The fixture's location; the app itself has no built-in location.
CFG = Config(lat=34.3033, lon=-77.8039, label="Wilmington, NC")


@pytest.fixture
def raw():
    return json.loads(FIXTURE.read_text())


def payload(raw, now=NOW_LOCAL):
    return build_payload(raw, CFG, now, datetime.now(timezone.utc), False)


def set_series(raw, key, var, values_by_time):
    """Overwrite one variable at the given local 'HH:MM' times on 2026-10-07."""
    block = raw[key]
    for hhmm, v in values_by_time.items():
        block[var][block["time"].index(f"2026-10-07T{hhmm}")] = v


class Clock:
    def __init__(self, t=NOW_UTC):
        self.t = t

    def __call__(self):
        return self.t


class Fetcher:
    def __init__(self, raw):
        self.raw = raw
        self.calls = 0
        self.fail = False
        self.delay = 0.0

    async def __call__(self, cfg):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("network down")
        return copy.deepcopy(self.raw)


# ---------------------------------------------------------------- window slicing

class TestWindow:
    def test_starts_at_current_quarter_hour_and_spans_six_hours(self, raw):
        # Bug: window starts at midnight / the first row of the response (showing the
        # past), or stops short of / overshoots now+6h.
        p = payload(raw)
        times = [h["time"] for h in p["hourly"]]
        assert p["step_minutes"] == 15
        assert times[0] == "2026-10-07T13:45-04:00"
        assert times[-1] == "2026-10-07T19:45-04:00"
        assert len(times) == 25

    def test_service_slices_in_forecast_timezone_not_utc(self, raw):
        # Bug: comparing the API's naive local times against UTC "now" shifts the
        # window 4 hours into the future (would start at 17:45 here).
        svc = WeatherService(CFG, fetcher=Fetcher(raw), clock=Clock())
        p = asyncio.run(svc.get())
        assert p["hourly"][0]["time"] == "2026-10-07T13:45-04:00"

    def test_falls_back_to_hourly_when_15_minute_data_has_gaps(self, raw):
        # Bug: a null in the 15-minute series (Open-Meteo returns nulls outside model
        # coverage) crashes the build or draws a broken chart instead of using hourly.
        set_series(raw, "minutely_15", "precipitation_probability", {"15:00": None})
        p = payload(raw)
        assert p["step_minutes"] == 60
        assert p["hourly"][0]["time"] == "2026-10-07T13:00-04:00"
        assert p["hourly"][-1]["time"] == "2026-10-07T19:00-04:00"

    def test_falls_back_to_hourly_when_15_minute_block_missing(self, raw):
        # Bug: KeyError when the API omits minutely_15 entirely.
        del raw["minutely_15"]
        assert payload(raw)["step_minutes"] == 60

    def test_unusable_response_raises(self, raw):
        # Bug: a response with no forecast rows yields available:true with an empty
        # chart instead of being rejected (and the last good data kept).
        del raw["minutely_15"]
        del raw["hourly"]
        with pytest.raises(ValueError):
            payload(raw)


# ---------------------------------------------------------------- current + summary

class TestCurrentAndSummary:
    def test_current_conditions_mapped_from_response(self, raw):
        # Bug: wrong field mapped (e.g. apparent temp shown as temp, wind in km/h key).
        c = payload(raw)["current"]
        assert c == {
            "temp_f": 74, "feels_like_f": 77, "humidity": 54, "wind_mph": 4,
            "cloud_cover": 0, "precip_prob": 0, "description": "Sunny",
            "icon": "clear", "is_day": True,
        }

    def test_temps_round_half_up_like_the_frontend(self, raw):
        # Bug: Python's banker's rounding shows 74° in the header while the chart
        # (Math.round) labels the same 74.5 reading as 75°.
        raw["current"]["temperature_2m"] = 74.5
        assert payload(raw)["current"]["temp_f"] == 75

    def test_peak_then_cool_on_recorded_afternoon(self, raw):
        # Bug: summary reports only "cooling" and hides the afternoon high, or names
        # the wrong peak hour.
        assert payload(raw)["summary"] == (
            "Peaking at 76° around 3:45 PM, then cooling to 68°. "
            "Dry and mostly sunny through 8 PM."
        )

    def test_rain_peak_names_first_peak_slot(self, raw):
        # Bug: picks the last of tied maxima, or the hour the rain chance first
        # becomes non-zero, rather than when it peaks.
        set_series(raw, "minutely_15", "precipitation_probability",
                   {"15:00": 30, "16:00": 40, "16:15": 40, "17:00": 40, "18:00": 10})
        assert payload(raw)["summary"].endswith("Rain chance peaks at 40% around 4 PM.")

    def test_high_rain_chance_says_likely(self, raw):
        # Bug: a 70% chance is described with the same soft wording as 20%.
        set_series(raw, "minutely_15", "precipitation_probability", {"17:30": 70})
        assert payload(raw)["summary"].endswith("Rain likely around 5:30 PM (70%).")

    def test_thunder_beats_rain_percentage(self, raw):
        # Bug: a thunderstorm code (the reason to get out of the pool) is drowned out
        # by a generic rain-chance sentence.
        set_series(raw, "minutely_15", "precipitation_probability", {"17:00": 80})
        set_series(raw, "minutely_15", "weather_code", {"16:45": 95})
        assert payload(raw)["summary"].endswith("Thunderstorms possible around 4:45 PM.")

    def test_rain_outside_window_is_ignored(self, raw):
        # Bug: rain at 21:00 (beyond now+6h) leaks into the summary.
        set_series(raw, "minutely_15", "precipitation_probability", {"21:00": 90})
        assert "Dry" in payload(raw)["summary"]

    def test_night_window_says_clear_not_sunny(self, raw):
        # Bug: "mostly sunny" shown for an evening/overnight window.
        s = payload(raw, now=datetime(2026, 10, 7, 21, 0))["summary"]
        assert "sunny" not in s

    def test_steady_temperature_wording(self):
        # Bug: a 1° wobble reported as "Warming"/"Cooling".
        t0 = datetime(2026, 10, 7, 12, 0)
        rows = [{"dt": t0.replace(hour=12 + i), "temp_f": 80 + (i % 2) * 0.8,
                 "precip_prob": 0, "cloud_cover": 90, "code": 3, "is_day": 1}
                for i in range(7)]
        assert summarize(rows) == "Holding near 80°. Dry and cloudy through 6 PM."

    def test_describe_day_night_and_unknown(self):
        # Bug: "Sunny" at night, or an unmapped WMO code raising KeyError.
        assert describe(0, True) == ("Sunny", "clear")
        assert describe(0, False) == ("Clear", "clear")
        assert describe(63, True) == ("Rain", "rain")
        assert describe(1234, True) == ("Unknown", "cloudy")
        assert describe(None, True) == ("Unknown", "cloudy")


# ---------------------------------------------------------------- caching

class TestCache:
    def test_cached_for_ten_minutes_then_refreshed(self, raw):
        # Bug: every page load hits Open-Meteo, or the cache never expires.
        f, clock = Fetcher(raw), Clock()
        svc = WeatherService(CFG, fetcher=f, clock=clock)
        asyncio.run(svc.get())
        clock.t += 599
        asyncio.run(svc.get())
        assert f.calls == 1
        clock.t += 2
        asyncio.run(svc.get())
        assert f.calls == 2

    def test_stale_data_served_when_refresh_fails(self, raw):
        # Bug: a failed refresh wipes good data and the section disappears, or
        # stale data is passed off as fresh.
        f, clock = Fetcher(raw), Clock()
        svc = WeatherService(CFG, fetcher=f, clock=clock)
        first = asyncio.run(svc.get())
        f.fail = True
        clock.t += 15 * 60
        p = asyncio.run(svc.get())
        assert p["available"] is True
        assert p["stale"] is True
        assert p["updated_at"] == first["updated_at"]
        # Window is re-sliced to the new "now", not frozen at fetch time.
        assert p["hourly"][0]["time"] == "2026-10-07T14:00-04:00"

    def test_failed_refresh_backs_off(self, raw):
        # Bug: while Open-Meteo is down every request waits out the 8 s timeout.
        f, clock = Fetcher(raw), Clock()
        f.fail = True
        svc = WeatherService(CFG, fetcher=f, clock=clock)
        asyncio.run(svc.get())
        clock.t += 30
        asyncio.run(svc.get())
        assert f.calls == 1
        clock.t += 31
        asyncio.run(svc.get())
        assert f.calls == 2

    def test_bad_response_does_not_replace_good_data(self, raw):
        # Bug: a 200 with a garbage body overwrites the cache and the section vanishes.
        f, clock = Fetcher(raw), Clock()
        svc = WeatherService(CFG, fetcher=f, clock=clock)
        asyncio.run(svc.get())
        f.raw = {"error": True, "reason": "bad"}
        clock.t += 11 * 60
        p = asyncio.run(svc.get())
        assert p["available"] is True and p["stale"] is True

    def test_cached_data_not_blocked_by_slow_refresh(self, raw):
        # Bug: once the cache expires, every page load queues behind a refresh that
        # can take the full 8 s timeout, even though good data is on hand.
        f, clock = Fetcher(raw), Clock()
        svc = WeatherService(CFG, fetcher=f, clock=clock)
        asyncio.run(svc.get())
        clock.t += 11 * 60
        f.delay = 1.0

        async def scenario():
            refresh = asyncio.create_task(svc.get())
            await asyncio.sleep(0.01)  # let it take the lock
            quick = await asyncio.wait_for(svc.get(), timeout=0.2)
            await refresh
            return quick

        assert asyncio.run(scenario())["available"] is True

    def test_unavailable_when_never_fetched(self, raw):
        # Bug: first-ever failure raises (500) or returns a half-built payload.
        f = Fetcher(raw)
        f.fail = True
        svc = WeatherService(CFG, fetcher=f, clock=Clock())
        assert asyncio.run(svc.get()) == {"available": False}

    def test_concurrent_requests_share_one_fetch(self, raw):
        # Bug: a burst of page loads on a cold cache stampedes the API.
        f = Fetcher(raw)
        f.delay = 0.05
        svc = WeatherService(CFG, fetcher=f, clock=Clock())

        async def burst():
            return await asyncio.gather(*(svc.get() for _ in range(5)))

        results = asyncio.run(burst())
        assert f.calls == 1
        assert all(r["available"] for r in results)


# ---------------------------------------------------------------- config + route

def test_env_overrides_and_bad_values_dont_crash(monkeypatch):
    # Bug: a typo in WEATHER_LAT / WEATHER_TZ crashes app startup, or a half-valid
    # location is used with a made-up other half.
    monkeypatch.delenv("WEATHER_ZIP", raising=False)
    monkeypatch.setenv("WEATHER_LAT", "35.5")
    monkeypatch.setenv("WEATHER_LON", "not-a-number")
    monkeypatch.setenv("WEATHER_TZ", "Mars/Olympus")
    monkeypatch.setenv("WEATHER_LABEL", "Beach house")
    assert Config.from_env() is None  # no valid location -> weather off
    monkeypatch.setenv("WEATHER_LON", "-77.0")
    cfg = Config.from_env()
    assert (cfg.lat, cfg.lon, cfg.tz, cfg.label) == (35.5, -77.0, weather.DEFAULT_TZ, "Beach house")


def test_route_returns_200_unavailable_on_failure(monkeypatch, raw):
    # Bug: weather outage surfaces as a 5xx on the guest page.
    f = Fetcher(raw)
    f.fail = True
    monkeypatch.setattr(weather, "_service", WeatherService(CFG, fetcher=f, clock=Clock()))
    app = FastAPI()
    app.include_router(weather.router)
    r = TestClient(app).get("/api/weather")
    assert r.status_code == 200
    assert r.json() == {"available": False}


def test_dst_end_day_window_uses_response_fixed_offset(raw):
    # Bug: Open-Meteo keeps one fixed offset (EDT, -4h) for the whole response even
    # after DST ends; windowing in the DST-aware zone started the 6 h window an hour
    # in the past and labelled every row an hour off on the change day.
    import copy
    from datetime import timedelta as td

    shifted = copy.deepcopy(raw)
    for key in ("hourly", "minutely_15"):
        if key in shifted:
            shifted[key]["time"] = [
                (datetime.fromisoformat(t) + td(days=25)).strftime("%Y-%m-%dT%H:%M")
                for t in shifted[key]["time"]
            ]
    # 18:00Z on 2026-11-01 is 13:00 EST, but 14:00 in the response's fixed -04:00.
    now = datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc)
    p = build_payload(shifted, CFG, now, now, False)
    first = p["hourly"][0]
    assert first["time"] == "2026-11-01T13:00-05:00"
    key = "minutely_15" if "minutely_15" in shifted else "hourly"
    i = shifted[key]["time"].index("2026-11-01T14:00")
    assert first["temp_f"] == round(shifted[key]["temperature_2m"][i], 1)


def test_no_location_configured_means_weather_off(monkeypatch):
    # Bug: shipping the author's home location as a default for everyone who
    # deploys this, or crashing when no location is set.
    import asyncio

    for k in ("WEATHER_LAT", "WEATHER_LON", "WEATHER_ZIP"):
        monkeypatch.delenv(k, raising=False)
    assert Config.from_env() is None

    async def boom(cfg):
        raise AssertionError("must not fetch without a location")

    svc = WeatherService(None, fetcher=boom)
    svc.cfg = None
    assert asyncio.run(svc.get()) == {"available": False}


def test_zip_is_geocoded_once_then_cached(monkeypatch, raw):
    # Bug: looking the zip up on every refresh (extra third-party calls), or never
    # resolving it so the forecast request goes out without coordinates.
    import asyncio
    from dataclasses import replace as dc_replace

    monkeypatch.setenv("WEATHER_ZIP", "28411")
    monkeypatch.delenv("WEATHER_LAT", raising=False)
    monkeypatch.delenv("WEATHER_LON", raising=False)
    cfg = Config.from_env()
    assert cfg.zip == "28411" and cfg.lat is None
    lookups, fetched_with = [], []

    async def geocode(c):
        lookups.append(c.zip)
        return dc_replace(c, lat=34.3, lon=-77.8, label="Wilmington, NC")

    async def fetch(c):
        fetched_with.append((c.lat, c.lon))
        return raw

    clock = Clock()
    svc = WeatherService(cfg, fetcher=fetch, clock=clock, geocoder=geocode)

    async def go():
        await svc.get()
        clock.t += 15 * 60
        return await svc.get()

    p = asyncio.run(go())
    assert lookups == ["28411"]
    assert fetched_with == [(34.3, -77.8), (34.3, -77.8)]
    assert p["location"] == "Wilmington, NC"
