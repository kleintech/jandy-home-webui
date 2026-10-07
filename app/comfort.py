"""How comfortable a swim will be right now: one short sentence for guests.

`swim_comfort()` is pure and deterministic. It combines the pool's water temperature
with the current weather and the next few hours of forecast, and returns::

    {"rating": "Great", "level": 4,
     "text": "Great for swimming: 84° water, sunny and calm."}

or None when there is no usable weather. Levels: Cold 0, Chilly 1, Fair 2, Good 3,
Great 4, Perfect 5; "Storms" is a hazard override reported at level 0.

How it scores
-------------

1. **Water band** (the base). Typical residential-pool guidance: competition pools are
   kept 77-82°F (FINA/World Aquatics range 25-28°C), recreational pools usually
   80-84°F, and most people find 82-88°F ideal for lounging/play; above ~88°F the
   water stops cooling you, and above ~92°F it feels like a bath (hot tubs start
   around 100°F). Below ~74°F most guests find a pool cold. Bands, on the rounded
   water temperature:

   ======== ============ ===== ==========
   °F        band         base  max level
   ======== ============ ===== ==========
   < 74      cold          0     1
   74-77     brisk         1     3
   78-81     refreshing    3     5
   82-88     ideal         5     5
   89-92     very warm     4     4
   > 92      bath-warm     3     3
   ======== ============ ===== ==========

   The "max level" cap means nice weather can't talk cold water up to "Great".
   Water above 100°F (past where pool water should be; hot tubs top out at 104°F)
   is "too hot for a long swim" and capped at Fair. Separately, climbing out into
   air that feels < 55°F caps the level at Chilly, and < 45°F at Cold.

2. **Getting out** (evaporative chill). Wet skin loses heat fast, so what matters is
   the air you climb out into. Uses the feels-like temperature (falls back to air
   temp). Penalties, together never more than -4:

   * cool air: feels < 70°F -1, < 62°F -2
   * wind: breezy >= 10 mph -1, windy >= 18 mph -2 (breezy is ignored once it feels
     >= 80°F, where a breeze is welcome; windy still costs 1 there)
   * dry air: humidity < 40% while it feels < 80°F -1 (faster evaporation)
   * no warming sun: overcast (cloud >= 80%) by day, or night, while it feels < 75°F -1

3. **Sun** on the deck: daytime, cloud <= 40% and it feels 70-84°F: +1 (on a
   cooler day the sun no longer offsets a wet guest's chill).

4. **Heat**: when it feels >= 90°F (or air >= 85°F with humidity >= 65%, "muggy"),
   water <= 88°F is a relief (+1); water > 90°F gives no relief (-1). Cold or brisk
   water is still called cold ("70° water is cold, but a relief from the heat").

5. **Missing water temp** (Jandy reports none while the filter pump is off; in spa
   mode the pool sensor may be blank): the base comes from the feels-like air
   temperature instead (>= 85: 4, >= 78: 3, >= 72: 2, >= 65: 1, else 0), the same
   adjustments apply, the level is capped at Great, and the sentence says why the
   water temperature is missing (pump off, spa mode, controller offline/unknown).

6. **Hazards** override the score. Thunder now or a thunderstorm code (95/96/99)
   in the next 3 hours gives rating "Storms" with a get-out-at-first-thunder line
   (lightning: "when thunder roars, go indoors"). Rain, snow or freezing rain now, or
   a >= 60% chance in the next 3 hours (from now), is mentioned in the sentence but
   doesn't change the rating: "soon" when under 45 minutes away, else "by <hour>"
   rounded up.

7. **Day/night**: `is_day` from the payload; when missing, the local hour (7-19 is
   day), else neutral wording -- a missing flag never reads as night.

The sentence is "<Rating> for swimming: <water>, <one condition>." -- the most salient
condition wins (rain > heat relief > wind/cool chill > overcast/night > sun/calm), so
it stays to two short clauses. Temperatures are rounded whole °F, as on the panel;
the feels-like temperature is printed as "feels like 60°" when it is 3°+ off the air.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

# Same WMO codes as app.weather.THUNDER_CODES (kept here so this module has no
# imports from the weather service and stays trivially testable).
THUNDER_CODES = frozenset({95, 96, 99})
SNOW_CODES = frozenset({71, 73, 75, 77, 85, 86})
FREEZING_CODES = frozenset({56, 57, 66, 67})   # freezing drizzle / freezing rain
LOOKAHEAD = timedelta(hours=3)                  # measured from now
STEP_MAX = timedelta(hours=1)   # a row this recent still covers "now" (the current slot)
SOON = timedelta(minutes=45)    # rain closer than this is "soon", not "by <hour>"
DAY_START, DAY_END = 7, 19      # local-hour fallback when is_day is missing

TOO_HOT_F = 100       # above this, pool water is hot-tub warm: at most Fair
COLD_FEEL = 55        # feels-like below this: at most Chilly, "cold" getting out
FREEZING_FEEL = 45    # feels-like below this: Cold, "freezing" getting out

# Why the water temperature is missing -> what the sentence says about it.
WATER_NOTES = {
    "pump_off": "water temp shows when the pump is running",
    "spa": "pool temp isn't measured in spa mode",
    "offline": "pool temp unavailable",
    "unknown": "pool temp unavailable",
}

RATINGS = ("Cold", "Chilly", "Fair", "Good", "Great", "Perfect")

BREEZY_MPH = 10
WINDY_MPH = 18
RAIN_LIKELY_PCT = 60


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)) and math.isfinite(v):
        return float(v)
    return None


def _rnd(x: float) -> int:
    """Round half up (matches the frontend's Math.round)."""
    return math.floor(x + 0.5)


def water_band(water: int) -> tuple[str, int, int]:
    """(band, base level, max level) for a rounded water temperature in °F."""
    if water < 74:
        return "cold", 0, 1
    if water < 78:
        return "brisk", 1, 3
    if water < 82:
        return "refreshing", 3, 5
    if water <= 88:
        return "ideal", 5, 5
    if water <= 92:
        return "very warm", 4, 4
    return "bath-warm", 3, 3


def _air_base(feel: int) -> int:
    if feel >= 85:
        return 4
    if feel >= 78:
        return 3
    if feel >= 72:
        return 2
    if feel >= 65:
        return 1
    return 0


def _parse_time(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def _hour_label(t: datetime, up: bool = False) -> str:
    """15:45 -> '4 PM' (nearest hour, or the next whole hour with up=True), on the
    pool's own clock (the forecast row's offset)."""
    if up:
        if t.minute or t.second or t.microsecond:
            t = t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    else:
        t = (t + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0)
    h = t.hour % 12 or 12
    return f"{h} {'AM' if t.hour < 12 else 'PM'}"


def _rows(upcoming: Any) -> list[tuple[datetime, dict[str, Any]]]:
    """(time, row) for every usable forecast row; anything malformed is skipped."""
    out = []
    try:
        it = iter(upcoming) if upcoming is not None else iter(())
    except TypeError:
        return out
    for p in it:
        if isinstance(p, dict):
            t = _parse_time(p.get("time"))
            if t is not None:
                out.append((t, p))
    return out


def _clock(rows: list[tuple[datetime, dict[str, Any]]], now: Any):
    """A common aware timeline: (rows with aware times, aware now or None).

    Naive times (rows or `now`) are read in the forecast's own offset (the first
    aware row's), else UTC, so mixed naive/aware input never raises on compare.
    `now` defaults to the first row's time (the current slot)."""
    ref = next((t.tzinfo for t, _ in rows if t.tzinfo is not None), None)
    if ref is None:
        ref = now.tzinfo if isinstance(now, datetime) and now.tzinfo is not None else UTC
    fix = [(t if t.tzinfo is not None else t.replace(tzinfo=ref), p) for t, p in rows]
    if isinstance(now, datetime):
        now = now if now.tzinfo is not None else now.replace(tzinfo=ref)
        now = now.astimezone(ref)
    else:
        now = fix[0][0] if fix else None
    return fix, now


def _precip_word(code: Any, icon: Any = None) -> str:
    c = _num(code)
    if icon == "snow" or c in SNOW_CODES:
        return "snow"
    if c in FREEZING_CODES:
        return "freezing rain"
    return "rain"


def swim_comfort(water_f: Any, current: dict[str, Any] | None,
                 upcoming: Iterable[dict[str, Any]] | None = None,
                 now_is_day: bool | None = None, now: datetime | None = None,
                 water_reason: str | None = None) -> dict[str, Any] | None:
    """Rate swimming comfort now. See the module docstring for the model.

    `water_f`: pool water °F or None. `current`: the weather payload's `current`
    block (temp_f, feels_like_f, wind_mph, humidity, cloud_cover, icon, code,
    is_day...). `upcoming`: the payload's `hourly` rows (time, precip_prob, code, ...).
    `now_is_day` overrides `current["is_day"]` when given. `now` is the current time
    (defaults to the first forecast row). `water_reason` says why `water_f` is None:
    "offline", "spa", "pump_off" (the default) or "unknown". Never raises.
    """
    if not isinstance(current, dict):
        return None
    air = _num(current.get("temp_f"))
    if air is None:
        return None
    feel_raw = _num(current.get("feels_like_f"))
    feel = _rnd(feel_raw if feel_raw is not None else air)
    air_i = _rnd(air)
    wind = _num(current.get("wind_mph")) or 0.0
    hum = _num(current.get("humidity"))
    cloud = _num(current.get("cloud_cover"))
    icon = current.get("icon")
    code = _num(current.get("code"))
    water_num = _num(water_f)
    water = _rnd(water_num) if water_num is not None else None

    rows, now = _clock(_rows(upcoming), now)
    soon = [(t, p) for t, p in rows if now is not None and now - STEP_MAX < t <= now + LOOKAHEAD]

    # Day/night: explicit flag, else the payload's, else the local hour (7-19 is day),
    # else unknown -- never assume night just because the flag is missing.
    day_flag = now_is_day if now_is_day is not None else current.get("is_day")
    if isinstance(day_flag, (bool, int, float)) and not (isinstance(day_flag, float) and math.isnan(day_flag)):
        is_day: bool | None = bool(day_flag)
    elif now is not None:
        is_day = DAY_START <= now.hour < DAY_END
    else:
        is_day = None
    night = is_day is False

    # ---- hazards: thunder overrides everything
    if icon == "storm" or code in THUNDER_CODES:
        return _result("Storms", 0, "Storms now — stay out of the pool until 30 minutes after the last thunder.")
    thunder = next((t for t, p in soon if _num(p.get("code")) in THUNDER_CODES), None)
    if thunder is not None:
        return _result("Storms", 0,
                       f"Storms possible around {_hour_label(thunder)} — "
                       "get out at the first thunder.")

    # ---- base
    if water is not None:
        band, level, cap = water_band(water)
        if water > TOO_HOT_F:
            band, cap = "too hot for a long swim", min(cap, 2)
    else:
        band, level, cap = None, _air_base(feel), 4
    # Climbing out into near-freezing air is never better than Chilly/Cold,
    # however warm the water.
    if feel < FREEZING_FEEL:
        cap = 0
    elif feel < COLD_FEEL:
        cap = min(cap, 1)

    # ---- getting out: chill penalties
    penalty = 0
    cool = feel < 70
    if feel < 62:
        penalty += 2
    elif cool:
        penalty += 1
    windy = wind >= WINDY_MPH
    breezy = not windy and wind >= BREEZY_MPH and feel < 80
    if windy:
        penalty += 2 if feel < 80 else 1
    elif breezy:
        penalty += 1
    dry = hum is not None and hum < 40 and feel < 80
    if dry:
        penalty += 1
    overcast = is_day is not False and cloud is not None and cloud >= 80
    gloomy = (overcast or night) and feel < 75
    if gloomy:
        penalty += 1
    level -= min(penalty, 4)

    # ---- sun and heat
    sunny = is_day is True and cloud is not None and cloud <= 40
    if sunny and 70 <= feel < 85:
        level += 1
    hot = feel >= 90 or (air_i >= 85 and hum is not None and hum >= 65)
    relief = hot and water is not None and water <= 88
    no_relief = hot and water is not None and water > 90
    if relief:
        level += 1
    elif no_relief:
        level -= 1

    level = max(0, min(level, cap))
    rating = RATINGS[level]

    # ---- sentence: "<Rating> for swimming: <water>, <one condition>."
    # Feels-like is named as such when it is noticeably off the air temperature.
    differs = abs(feel - air_i) >= 3
    temp = f"feels like {feel}°" if differs else f"{feel}°"
    cold_word = "freezing" if feel < FREEZING_FEEL else "cold"
    cool_word = cold_word if feel < COLD_FEEL else "cool"

    if icon == "snow" or code in SNOW_CODES:
        rain = "with snow falling"
    elif code in FREEZING_CODES:
        rain = "with freezing rain falling"
    elif icon in ("rain", "drizzle"):
        rain = "with rain falling"
    else:
        # "by" the first slot that crosses the threshold, not the peak.
        wet = next(((t, p) for t, p in soon
                    if (_num(p.get("precip_prob")) or 0) >= RAIN_LIKELY_PCT), None)
        if wet is None:
            rain = None
        else:
            what = _precip_word(wet[1].get("code"))
            rain = (f"but {what} likely soon" if wet[0] - now < SOON else
                    f"but {what} likely by {_hour_label(wet[0], up=True)}")

    if water is None:
        if night:
            sky = f"{temp} tonight"
        elif sunny:
            sky = f"{temp} and sunny"
        elif overcast:
            sky = f"{temp} and overcast"
        elif is_day is None:
            sky = f"{temp} outside"
        else:
            sky = f"{temp} and partly cloudy"
        extra = rain or ("but windy" if windy else "but breezy" if breezy else None)
        body = sky if extra is None else f"{sky}, {extra}"
        note = WATER_NOTES.get(water_reason or "pump_off", WATER_NOTES["unknown"])
        return _result(rating, level, f"{rating} for swimming: {body}; {note}.")

    chill = None
    if (windy and feel < 80) or breezy:
        w = "windy" if windy else "breezy"
        chill = (f"but {w} and {temp}, {cold_word} getting out" if differs else
                 f"but {feel}° and {w} feels {cold_word} getting out")
    elif cool:
        chill = (f"but {'dry and ' if dry else ''}{temp}, {cool_word} getting out" if differs else
                 f"but {feel}° {'dry ' if dry else ''}air feels {cool_word} getting out")
    elif dry and feel < 75:
        chill = "but dry air feels cool getting out"

    cold_water = band in ("cold", "brisk")
    if rain:
        cond = rain
    elif relief:
        heat = "the muggy heat" if hum is not None and hum >= 55 else "the heat"
        # Cold water on a hot day is still cold: say so, then the relief.
        cond = f"but a relief from {heat}" if cold_water else f"a relief from {heat}"
    elif no_relief:
        cond = "not much relief from the heat"
    elif chill:
        cond = chill
    elif windy:
        cond = "but windy"
    elif night:
        mood = ("a cold" if feel < COLD_FEEL else "a cool" if feel < 70 else
                "a mild" if feel < 78 else "a warm")
        cond = f"{mood} night that {temp}" if differs else f"{mood} {feel}° night"
    elif overcast and gloomy:
        cond = f"but overcast and {temp}"
    elif sunny:
        cond = "sunny and calm" if wind < BREEZY_MPH else "sunny and breezy"
    elif overcast:
        cond = f"overcast and {temp}"
    elif is_day is None:
        cond = f"{temp} outside"
    else:
        cond = f"partly cloudy and {temp}"

    # Name the band unless it is "ideal" (the number says enough), or a "but ..."
    # clause already explains the rating and "refreshing" would read as a contradiction.
    show_band = band != "ideal" and not (band == "refreshing" and cond.startswith("but "))
    lead = f"{water}° water is {band}" if show_band else f"{water}° water"
    return _result(rating, level, f"{rating} for swimming: {lead}, {cond}.")


def _result(rating: str, level: int, text: str) -> dict[str, Any]:
    return {"rating": rating, "level": level, "text": text}
