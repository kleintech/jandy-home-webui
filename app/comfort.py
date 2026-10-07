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
   water <= 88°F is a relief (+1); water > 90°F gives no relief (-1).

5. **Missing water temp** (Jandy reports none while the filter pump is off; in spa
   mode the pool sensor may be blank): the base comes from the feels-like air
   temperature instead (>= 85: 4, >= 78: 3, >= 72: 2, >= 65: 1, else 0), the same
   adjustments apply, the level is capped at Great, and the sentence says the water
   temperature shows when the pump is running.

6. **Hazards** override the score. Thunder now or a thunderstorm code (95/96/99)
   in the next 3 hours gives rating "Storms" with a get-out-at-first-thunder line
   (lightning: "when thunder roars, go indoors"). Rain now, or a >= 60% chance in
   the next 3 hours, is mentioned in the sentence but doesn't change the rating.

The sentence is "<Rating> for swimming: <water>, <one condition>." -- the most salient
condition wins (rain > heat relief > wind/cool chill > overcast/night > sun/calm), so
it stays to two short clauses. Temperatures are rounded whole °F, as on the panel.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Iterable

# Same WMO codes as app.weather.THUNDER_CODES (kept here so this module has no
# imports from the weather service and stays trivially testable).
THUNDER_CODES = frozenset({95, 96, 99})
LOOKAHEAD = timedelta(hours=3)

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


def _hour_label(iso: str) -> str:
    """'2026-10-07T15:45-04:00' -> '4 PM' (nearest hour, the pool's own clock)."""
    dt = datetime.fromisoformat(iso)
    dt = (dt + timedelta(minutes=30)).replace(minute=0)
    h = dt.hour % 12 or 12
    return f"{h} {'AM' if dt.hour < 12 else 'PM'}"


def _soon(upcoming: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Forecast points within LOOKAHEAD of the first one (the current slot)."""
    pts = []
    start = None
    for p in upcoming or ():
        try:
            t = datetime.fromisoformat(p["time"])
        except (KeyError, TypeError, ValueError):
            continue
        if start is None:
            start = t
        if t - start <= LOOKAHEAD:
            pts.append(p)
    return pts


def swim_comfort(water_f: Any, current: dict[str, Any] | None,
                 upcoming: Iterable[dict[str, Any]] | None = None,
                 now_is_day: bool | None = None) -> dict[str, Any] | None:
    """Rate swimming comfort now. See the module docstring for the model.

    `water_f`: pool water °F or None. `current`: the weather payload's `current`
    block (temp_f, feels_like_f, wind_mph, humidity, cloud_cover, icon, is_day...).
    `upcoming`: the payload's `hourly` rows (time, precip_prob, code, ...).
    `now_is_day` overrides `current["is_day"]` when given.
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
    is_day = bool(current.get("is_day")) if now_is_day is None else bool(now_is_day)
    water_num = _num(water_f)
    water = _rnd(water_num) if water_num is not None else None
    soon = _soon(upcoming or [])

    # ---- hazards: thunder overrides everything
    if icon == "storm":
        return _result("Storms", 0, "Storms now — stay out of the pool until 30 minutes after the last thunder.")
    thunder = next((p for p in soon if _num(p.get("code")) in THUNDER_CODES), None)
    if thunder is not None:
        return _result("Storms", 0,
                       f"Storms possible around {_hour_label(thunder['time'])} — "
                       "get out at the first thunder.")

    # ---- base
    if water is not None:
        band, level, cap = water_band(water)
    else:
        band, level, cap = None, _air_base(feel), 4

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
    overcast = is_day and cloud is not None and cloud >= 80
    gloomy = (overcast or not is_day) and feel < 75
    if gloomy:
        penalty += 1
    level -= min(penalty, 4)

    # ---- sun and heat
    sunny = is_day and cloud is not None and cloud <= 40
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
    rain_now = icon in ("rain", "drizzle")
    # "by" the first slot that crosses the threshold, not the peak.
    wet = next((p for p in soon if (_num(p.get("precip_prob")) or 0) >= RAIN_LIKELY_PCT), None)
    rain = ("with rain falling" if rain_now else None if wet is None else
            "but rain likely soon" if wet is soon[0] else
            f"but rain likely by {_hour_label(wet['time'])}")

    if water is None:
        if not is_day:
            sky = f"{feel}° tonight"
        elif sunny:
            sky = f"{feel}° and sunny"
        elif overcast:
            sky = f"{feel}° and overcast"
        else:
            sky = f"{feel}° and partly cloudy"
        extra = rain or ("but windy" if windy else "but breezy" if breezy else None)
        body = sky if extra is None else f"{sky}, {extra}"
        return _result(rating, level,
                       f"{rating} for swimming: {body}; water temp shows when the pump is running.")

    chill = None
    if windy and feel < 80:
        chill = f"but {feel}° and windy feels cold getting out"
    elif breezy:
        chill = f"but {feel}° and breezy feels cold getting out"
    elif cool:
        chill = f"but {feel}° {'dry ' if dry else ''}air feels cool getting out"
    elif dry and feel < 75:
        chill = "but dry air feels cool getting out"

    if rain:
        cond = rain
    elif relief:
        cond = "a relief from the muggy heat" if hum is not None and hum >= 55 else "a relief from the heat"
    elif no_relief:
        cond = "not much relief from the heat"
    elif chill:
        cond = chill
    elif windy:
        cond = "but windy"
    elif not is_day:
        cond = f"{'a cool' if feel < 70 else 'a mild' if feel < 78 else 'a warm'} {feel}° night"
    elif overcast and gloomy:
        cond = f"but overcast and {feel}°"
    elif sunny:
        cond = "sunny and calm" if wind < BREEZY_MPH else "sunny and breezy"
    elif overcast:
        cond = f"overcast and {feel}°"
    else:
        cond = f"partly cloudy and {feel}°"

    # Name the band unless it is "ideal" (the number says enough), or a "but ..."
    # clause already explains the rating and "refreshing" would read as a contradiction.
    show_band = band != "ideal" and not (band == "refreshing" and cond.startswith("but "))
    lead = f"{water}° water is {band}" if show_band else f"{water}° water"
    return _result(rating, level, f"{rating} for swimming: {lead}, {cond}.")


def _result(rating: str, level: int, text: str) -> dict[str, Any]:
    return {"rating": rating, "level": level, "text": text}
