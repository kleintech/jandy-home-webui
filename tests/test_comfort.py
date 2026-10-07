"""Swim comfort: realistic scenarios, band edges, and hazard overrides.

Each test names the bug it would catch. Expected ratings are judgments about the
scenario (what a guest would say), not re-derivations of the scoring formula.
"""

import pytest

from app.comfort import swim_comfort


def cur(**kw):
    """A pleasant, sunny, calm 80°F afternoon; override per scenario."""
    base = dict(temp_f=80, feels_like_f=80, wind_mph=4, humidity=55, cloud_cover=10,
                icon="clear", is_day=True)
    base.update(kw)
    return base


def hours(*probs, codes=None, start=13):
    """Hourly forecast points from start:00 local, with rain chances and WMO codes."""
    codes = codes or {}
    return [{"time": f"2026-10-07T{start + i:02d}:00-04:00", "temp_f": 80.0,
             "precip_prob": p, "cloud_cover": 10, "humidity": 55, "code": codes.get(i, 1)}
            for i, p in enumerate(probs)]


DRY = hours(0, 0, 0, 0, 0, 0, 0)


class TestScenarios:
    def test_hot_sunny_calm_day_with_86_water_is_perfect(self):
        # Bug: ideal water on a hot, calm, sunny day rated below Perfect (e.g. heat
        # treated as a penalty, or the 82-88 band mis-bounded).
        r = swim_comfort(86, cur(temp_f=88, feels_like_f=88, humidity=45), DRY)
        assert r["rating"] == "Perfect" and r["level"] == 5
        assert r["text"].startswith("Perfect for swimming: 86° water")

    def test_mild_sunny_calm_day_says_sunny_and_calm(self):
        # Bug: sky/wind wording lost; guests see only a number.
        r = swim_comfort(84, cur(temp_f=78, feels_like_f=78), DRY)
        assert r["rating"] == "Perfect"
        assert r["text"] == "Perfect for swimming: 84° water, sunny and calm."

    def test_breezy_cool_day_with_78_water_is_chilly(self):
        # Bug: wind ignored -> a 68°, 15 mph day with 78° water rated Good/Great.
        r = swim_comfort(78, cur(temp_f=68, feels_like_f=66, wind_mph=15, cloud_cover=30), DRY)
        assert r["rating"] == "Chilly"
        assert "breezy" in r["text"] and "getting out" in r["text"]
        # "refreshing" next to a Chilly rating reads as a contradiction.
        assert "refreshing" not in r["text"]

    def test_wind_alone_lowers_rating_on_same_day(self):
        # Bug: wind threshold never reached (mph vs km/h mixup) so wind changes nothing.
        calm = swim_comfort(80, cur(temp_f=74, feels_like_f=74), DRY)
        windy = swim_comfort(80, cur(temp_f=74, feels_like_f=74, wind_mph=20), DRY)
        assert windy["level"] < calm["level"]
        assert "windy" in windy["text"]

    def test_warm_water_cool_overcast_evening_is_not_great(self):
        # Bug: warm water alone drives the rating; climbing out into 66° grey air ignored.
        r = swim_comfort(86, cur(temp_f=66, feels_like_f=66, cloud_cover=95, icon="cloudy",
                                 wind_mph=6), DRY)
        assert r["rating"] in ("Good", "Fair")
        assert "66°" in r["text"] and "getting out" in r["text"]
        assert "sunny" not in r["text"]

    def test_muggy_95_air_makes_cool_water_the_relief(self):
        # Bug: high heat index treated as uncomfortable swimming, or "muggy" never said.
        r = swim_comfort(84, cur(temp_f=95, feels_like_f=108, humidity=70, cloud_cover=30,
                                 icon="partly"), DRY)
        assert r["rating"] == "Perfect"
        assert "relief from the muggy heat" in r["text"]

    def test_muggy_day_with_very_warm_water_is_no_relief(self):
        # Bug: heat bonus applied regardless of water temp -> 91° water "Perfect" at 95°.
        r = swim_comfort(91, cur(temp_f=95, feels_like_f=108, humidity=70), DRY)
        assert r["level"] <= 3
        assert "not much relief" in r["text"] and "very warm" in r["text"]

    def test_thunderstorm_forecast_overrides_rating(self):
        # Bug: lightning risk buried behind a nice water temperature.
        r = swim_comfort(86, cur(), hours(10, 30, 60, 70, codes={2: 95}))
        assert r["rating"] == "Storms" and r["level"] == 0
        assert r["text"] == "Storms possible around 3 PM — get out at the first thunder."

    def test_thunder_right_now_says_stay_out(self):
        # Bug: current storm only checked in the forecast rows, so "now" is missed.
        r = swim_comfort(86, cur(icon="storm", cloud_cover=100), DRY)
        assert r["rating"] == "Storms"
        assert "stay out" in r["text"]

    def test_thunder_beyond_three_hours_does_not_override(self):
        # Bug: a storm 6 hours out cancels a perfectly good swim now.
        r = swim_comfort(86, cur(), hours(0, 0, 0, 0, 0, 40, 60, codes={5: 95}))
        assert r["rating"] != "Storms"

    def test_rain_likely_soon_is_mentioned_with_time(self):
        # Bug: a 70%+ rain chance in the next hours not mentioned at all.
        r = swim_comfort(84, cur(cloud_cover=60, icon="partly"), hours(10, 30, 70, 80))
        assert "rain likely by 3 PM" in r["text"]

    def test_low_rain_chance_not_mentioned(self):
        # Bug: rain threshold inverted/too low, so every summary nags about rain.
        r = swim_comfort(84, cur(), hours(10, 20, 30, 20))
        assert "rain" not in r["text"]

    def test_raining_now_is_mentioned(self):
        # Bug: current rain ignored when the forecast rows are dry.
        r = swim_comfort(84, cur(icon="rain", cloud_cover=100), DRY)
        assert "rain" in r["text"]

    def test_missing_water_temp_still_summarizes_and_explains(self):
        # Bug: pump off (pool_temp None) hides the note, crashes, or prints "None°".
        r = swim_comfort(None, cur(temp_f=84, feels_like_f=86), DRY)
        assert r is not None
        assert "None" not in r["text"]
        assert "86° and sunny" in r["text"]
        assert "water temp shows when the pump is running" in r["text"]
        # Without knowing the water we never promise Perfect.
        assert r["rating"] != "Perfect"

    def test_missing_water_temp_cool_windy_is_low(self):
        # Bug: no-water path skips the wind/air adjustments and defaults to a good rating.
        r = swim_comfort(None, cur(temp_f=60, feels_like_f=55, wind_mph=20, cloud_cover=90,
                                   icon="cloudy"), DRY)
        assert r["rating"] == "Cold"

    def test_night_does_not_say_sunny(self):
        # Bug: clear night sky described as "sunny" / given the sun bonus.
        r = swim_comfort(84, cur(temp_f=74, feels_like_f=74, is_day=False, cloud_cover=0), DRY)
        assert "sunny" not in r["text"]
        assert "night" in r["text"]
        day = swim_comfort(84, cur(temp_f=74, feels_like_f=74, cloud_cover=0), DRY)
        assert r["level"] < day["level"]

    def test_now_is_day_argument_overrides_payload(self):
        # Bug: the explicit day/night flag ignored in favor of a stale payload value.
        r = swim_comfort(84, cur(is_day=True), DRY, now_is_day=False)
        assert "night" in r["text"]

    def test_cold_water_never_rated_good_even_on_a_hot_day(self):
        # Bug: hot sunny weather lifts 70° water to Good/Great.
        r = swim_comfort(70, cur(temp_f=92, feels_like_f=96, humidity=40), DRY)
        assert r["rating"] in ("Cold", "Chilly")
        assert "70° water is cold" in r["text"]

    def test_bath_warm_water_never_perfect(self):
        # Bug: >92° water rated as ideal because only the low end was banded.
        r = swim_comfort(95, cur(), DRY)
        assert r["level"] <= 3
        assert "bath-warm" in r["text"]

    def test_sentence_is_short(self):
        # Bug: verbose stacking of every factor overflows the card on a phone.
        worst = swim_comfort(None, cur(temp_f=70, feels_like_f=70, wind_mph=12), hours(10, 70))
        assert len(worst["text"]) <= 110
        assert worst["text"].count(",") <= 2


class TestBandEdges:
    # Bug for all of these: off-by-one at a band edge (< vs <=), e.g. 82° water
    # still called "refreshing" or 74° called "cold". Same calm, partly cloudy 78°
    # day throughout so only the water changes.
    DAY = dict(temp_f=78, feels_like_f=78, cloud_cover=60, icon="partly")

    @pytest.mark.parametrize("water,word", [
        (73, "is cold"), (74, "is brisk"), (77, "is brisk"), (78, "is refreshing"),
        (81, "is refreshing"), (89, "is very warm"), (92, "is very warm"), (93, "is bath-warm"),
    ])
    def test_band_words(self, water, word):
        r = swim_comfort(water, cur(**self.DAY), DRY)
        assert f"{water}° water {word}" in r["text"]

    @pytest.mark.parametrize("water", [82, 85, 88])
    def test_ideal_band_has_no_qualifier(self, water):
        r = swim_comfort(water, cur(**self.DAY), DRY)
        assert f"{water}° water, " in r["text"]
        assert r["rating"] == "Perfect"

    def test_ratings_rise_then_fall_across_bands(self):
        # Bug: band ordering scrambled, e.g. very warm rated above ideal.
        lv = {w: swim_comfort(w, cur(**self.DAY), DRY)["level"] for w in (70, 76, 80, 85, 90, 95)}
        assert lv[70] < lv[76] < lv[80] < lv[85]
        assert lv[95] < lv[90] <= lv[85]

    def test_water_rounds_half_up_like_the_panel(self):
        # Bug: banker's rounding shows 82.5 as "82°" while the page shows 83°.
        r = swim_comfort(81.5, cur(**self.DAY), DRY)
        assert "82° water," in r["text"]

    def test_wind_edges(self):
        # Bug: breezy/windy thresholds off by one (9 mph counted breezy, 10 not).
        cool = dict(temp_f=74, feels_like_f=74, cloud_cover=60, icon="partly")
        assert "breezy" not in swim_comfort(84, cur(wind_mph=9, **cool), DRY)["text"]
        assert "breezy" in swim_comfort(84, cur(wind_mph=10, **cool), DRY)["text"]
        assert "windy" in swim_comfort(84, cur(wind_mph=18, **cool), DRY)["text"]

    def test_rain_threshold_edge(self):
        # Bug: 59% treated as "likely" or 60% not.
        assert "rain" not in swim_comfort(84, cur(), hours(59))["text"]
        assert "rain likely" in swim_comfort(84, cur(), hours(60))["text"]


class TestRobustness:
    def test_no_current_weather_returns_none(self):
        # Bug: crash (500) when the weather block is missing or has no temperature.
        assert swim_comfort(84, None, DRY) is None
        assert swim_comfort(84, {"temp_f": None}, DRY) is None

    def test_sparse_current_block_still_works(self):
        # Bug: KeyError/TypeError when optional fields (feels-like, wind, humidity,
        # clouds) are null, as Open-Meteo sometimes returns.
        r = swim_comfort(84, {"temp_f": 80, "feels_like_f": None, "wind_mph": None,
                              "humidity": None, "cloud_cover": None, "is_day": True}, [])
        assert r["rating"] in ("Good", "Great", "Perfect")

    def test_bad_forecast_rows_are_skipped(self):
        # Bug: one malformed time string in the forecast takes the note down.
        rows = [{"time": "garbage", "code": 95}] + hours(0, 0)
        assert swim_comfort(84, cur(), rows)["rating"] != "Storms"


def test_rain_already_likely_in_current_slot_says_soon():
    # Bug: "rain likely by 1 PM" when it is already 1 PM (a time in the past/now).
    r = swim_comfort(84, cur(), hours(80, 80, 80))
    assert "rain likely soon" in r["text"]
