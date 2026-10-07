"""Short-range weather for the guest page (Open-Meteo, no API key).

`GET /api/weather` returns current conditions, the next ~6 hours of temperature,
rain chance and cloud cover, and a one-line summary. Data is fetched lazily on
request, cached for 10 minutes, and the last good response is served if a refresh
fails. Nothing here touches the pool controller; a weather failure only ever
produces ``{"available": false}`` or a stale payload.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)

API_URL = "https://api.open-meteo.com/v1/forecast"

# Zip 28411 (Wilmington, NC) centroid, from https://api.zippopotam.us/us/28411.
DEFAULT_LAT = 34.3033
DEFAULT_LON = -77.8039
DEFAULT_LABEL = "Wilmington, NC"
DEFAULT_TZ = "America/New_York"

CACHE_SECONDS = 600       # serve cached data for 10 minutes
RETRY_SECONDS = 60        # after a failed refresh, wait this long before trying again
TIMEOUT_SECONDS = 8.0
WINDOW = timedelta(hours=6)

SERIES_VARS = "temperature_2m,precipitation_probability,cloud_cover,weather_code,is_day"
CURRENT_VARS = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,"
    "wind_speed_10m,cloud_cover,weather_code,is_day"
)

# WMO weather interpretation codes -> (description, icon key).
# Icon keys: clear, partly, cloudy, fog, drizzle, rain, snow, storm
# (the frontend picks a day/night variant of clear/partly using is_day).
WMO: dict[int, tuple[str, str]] = {
    0: ("Clear", "clear"),
    1: ("Mostly clear", "clear"),
    2: ("Partly cloudy", "partly"),
    3: ("Overcast", "cloudy"),
    45: ("Fog", "fog"),
    48: ("Freezing fog", "fog"),
    51: ("Light drizzle", "drizzle"),
    53: ("Drizzle", "drizzle"),
    55: ("Heavy drizzle", "drizzle"),
    56: ("Freezing drizzle", "drizzle"),
    57: ("Freezing drizzle", "drizzle"),
    61: ("Light rain", "rain"),
    63: ("Rain", "rain"),
    65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "rain"),
    67: ("Freezing rain", "rain"),
    71: ("Light snow", "snow"),
    73: ("Snow", "snow"),
    75: ("Heavy snow", "snow"),
    77: ("Snow grains", "snow"),
    80: ("Light showers", "rain"),
    81: ("Showers", "rain"),
    82: ("Heavy showers", "rain"),
    85: ("Snow showers", "snow"),
    86: ("Heavy snow showers", "snow"),
    95: ("Thunderstorms", "storm"),
    96: ("Thunderstorms with hail", "storm"),
    99: ("Thunderstorms with hail", "storm"),
}
THUNDER_CODES = {95, 96, 99}


def describe(code: Any, is_day: bool = True) -> tuple[str, str]:
    """Return (description, icon) for a WMO code; day-aware wording for clear skies."""
    try:
        desc, icon = WMO[int(code)]
    except (KeyError, TypeError, ValueError):
        return ("Unknown", "cloudy")
    if icon == "clear" and is_day:
        desc = "Sunny" if int(code) == 0 else "Mostly sunny"
    return desc, icon


@dataclass(frozen=True)
class Config:
    lat: float = DEFAULT_LAT
    lon: float = DEFAULT_LON
    label: str = DEFAULT_LABEL
    tz: str = DEFAULT_TZ

    @classmethod
    def from_env(cls) -> "Config":
        def f(name: str, default: float) -> float:
            raw = os.environ.get(name)
            try:
                return float(raw) if raw else default
            except ValueError:
                log.warning("ignoring invalid %s=%r", name, raw)
                return default

        tz = os.environ.get("WEATHER_TZ") or DEFAULT_TZ
        try:
            ZoneInfo(tz)
        except Exception:
            log.warning("ignoring invalid WEATHER_TZ=%r", tz)
            tz = DEFAULT_TZ
        return cls(
            lat=f("WEATHER_LAT", DEFAULT_LAT),
            lon=f("WEATHER_LON", DEFAULT_LON),
            label=os.environ.get("WEATHER_LABEL") or DEFAULT_LABEL,
            tz=tz,
        )

    def params(self) -> dict[str, Any]:
        return {
            "latitude": self.lat,
            "longitude": self.lon,
            "current": CURRENT_VARS,
            "minutely_15": SERIES_VARS,
            "hourly": SERIES_VARS,
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
            "timezone": self.tz,
            "forecast_days": 2,
        }


# ---------------------------------------------------------------- parsing

def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)) and math.isfinite(v):
        return float(v)
    return None


def rnd(x: float) -> int:
    """Round half up, matching the frontend's Math.round (Python's round() is banker's)."""
    return math.floor(x + 0.5)


def _int(v: Any) -> int | None:
    n = _num(v)
    return None if n is None else rnd(n)


def _series(block: Any) -> list[dict[str, Any]] | None:
    """Turn an Open-Meteo column block into rows; None if unusable."""
    if not isinstance(block, dict) or not isinstance(block.get("time"), list):
        return None
    times = block["time"]
    cols = {k: block.get(k) for k in ("temperature_2m", "precipitation_probability",
                                      "cloud_cover", "weather_code", "is_day")}
    for k in ("temperature_2m", "precipitation_probability", "cloud_cover"):
        if not isinstance(cols[k], list) or len(cols[k]) != len(times):
            return None
    rows = []
    for i, t in enumerate(times):
        try:
            when = datetime.fromisoformat(t)
        except (TypeError, ValueError):
            return None
        def col(k: str):
            c = cols[k]
            return c[i] if isinstance(c, list) and i < len(c) else None
        rows.append({
            "dt": when,
            "temp_f": _num(col("temperature_2m")),
            "precip_prob": _int(col("precipitation_probability")),
            "cloud_cover": _int(col("cloud_cover")),
            "code": _int(col("weather_code")),
            "is_day": col("is_day"),
        })
    return rows


def _window(rows: list[dict[str, Any]], now: datetime, step: timedelta) -> list[dict[str, Any]]:
    """Rows from the slot containing `now` through now+6h (naive local times)."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    into_slot = (now - midnight).total_seconds() % step.total_seconds()
    start = now - timedelta(seconds=into_slot)
    end = now + WINDOW
    return [r for r in rows if start <= r["dt"] <= end]


def _complete(rows: list[dict[str, Any]]) -> bool:
    return len(rows) >= 2 and all(
        r["temp_f"] is not None and r["precip_prob"] is not None and r["cloud_cover"] is not None
        for r in rows
    )


def select_window(raw: dict[str, Any], now: datetime) -> tuple[list[dict[str, Any]], int]:
    """Pick the 6h window, preferring 15-minute data, falling back to hourly.

    `now` is a naive datetime in the forecast's local timezone. Returns (rows, step minutes).
    """
    for key, step in (("minutely_15", 15), ("hourly", 60)):
        rows = _series(raw.get(key))
        if rows is None:
            continue
        win = _window(rows, now, timedelta(minutes=step))
        # Each slot must have all three charted values; otherwise try coarser data.
        if _complete(win) and win[-1]["dt"] - win[0]["dt"] >= WINDOW - timedelta(minutes=step):
            return win, step
    return [], 0


# ---------------------------------------------------------------- summary

def fmt_time(dt: datetime) -> str:
    h = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{h} {ampm}" if dt.minute == 0 else f"{h}:{dt.minute:02d} {ampm}"


def fmt_hour(dt: datetime) -> str:
    """Nearest whole hour, e.g. 3:15 PM -> "3 PM", 7:45 PM -> "8 PM" (summaries stay short)."""
    return fmt_time((dt + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0))


def _first_max(rows, key):
    best = None
    for r in rows:
        if best is None or r[key] > best[key]:
            best = r
    return best


def _first_min(rows, key):
    best = None
    for r in rows:
        if best is None or r[key] < best[key]:
            best = r
    return best


def summarize(rows: list[dict[str, Any]]) -> str:
    """Two short sentences: temperature trend, then rain / sky outlook."""
    if not rows:
        return ""
    first = rows[0]
    hi = _first_max(rows, "temp_f")
    lo = _first_min(rows, "temp_f")
    t0 = rnd(first["temp_f"])
    hi_t, lo_t = rnd(hi["temp_f"]), rnd(lo["temp_f"])
    rise, fall = hi_t - t0, t0 - lo_t
    if rise >= 2 and fall >= 2:
        if hi["dt"] < lo["dt"]:
            temp = f"Peaking at {hi_t}° around {fmt_time(hi['dt'])}, then cooling to {lo_t}°."
        else:
            temp = f"Dipping to {lo_t}° around {fmt_time(lo['dt'])}, then warming to {hi_t}°."
    elif rise >= 2:
        temp = f"Warming to {hi_t}° by {fmt_hour(hi['dt'])}."
    elif fall >= 2:
        temp = f"Cooling to {lo_t}° by {fmt_hour(lo['dt'])}."
    else:
        temp = f"Holding near {t0}°."

    thunder = next((r for r in rows if r["code"] in THUNDER_CODES), None)
    wet = _first_max(rows, "precip_prob")
    p = wet["precip_prob"]
    if thunder is not None:
        sky = f"Thunderstorms possible around {fmt_time(thunder['dt'])}."
    elif p >= 60:
        sky = f"Rain likely around {fmt_time(wet['dt'])} ({p}%)."
    elif p >= 20:
        sky = f"Rain chance peaks at {p}% around {fmt_time(wet['dt'])}."
    else:
        avg_cloud = sum(r["cloud_cover"] for r in rows) / len(rows)
        days = sum(1 for r in rows if r["is_day"] == 1)
        bright = "sunny" if days * 2 >= len(rows) else "clear"
        if avg_cloud < 30:
            cover = f"mostly {bright}"
        elif avg_cloud < 70:
            cover = "partly cloudy"
        else:
            cover = "cloudy"
        until = fmt_hour(rows[-1]["dt"])
        # Don't repeat the time the temperature sentence already ends on.
        sky = f"Dry and {cover}." if temp.endswith(f"by {until}.") else \
            f"Dry and {cover} through {until}."
    return f"{temp} {sky}"


# ---------------------------------------------------------------- payload

def build_payload(raw: dict[str, Any], cfg: Config, now: datetime,
                  fetched_at: datetime, stale: bool) -> dict[str, Any]:
    """Assemble the API response from a raw Open-Meteo response.

    `now` is aware (or naive wall time in the response's own offset); `fetched_at`
    is aware. Raises ValueError if the response is unusable.

    Open-Meteo stamps every row with ONE fixed UTC offset for the whole response
    (`utc_offset_seconds`), even across a DST change, so rows are windowed in that
    fixed offset and only converted to the real local zone for display.
    """
    tz = ZoneInfo(cfg.tz)
    offset = _num(raw.get("utc_offset_seconds"))
    fixed = timezone(timedelta(seconds=offset)) if offset is not None else tz
    if now.tzinfo is not None:
        now = now.astimezone(fixed).replace(tzinfo=None)
    cur = raw.get("current")
    if not isinstance(cur, dict) or _num(cur.get("temperature_2m")) is None:
        raise ValueError("no current conditions")
    rows, step = select_window(raw, now)
    if not rows:
        raise ValueError("no forecast rows for the next 6 hours")
    rows = [dict(r, dt=r["dt"].replace(tzinfo=fixed).astimezone(tz).replace(tzinfo=None)) for r in rows]
    is_day = cur.get("is_day") == 1
    desc, icon = describe(cur.get("weather_code"), is_day)
    current = {
        "temp_f": rnd(_num(cur.get("temperature_2m"))),
        "feels_like_f": _int(cur.get("apparent_temperature")),
        "humidity": _int(cur.get("relative_humidity_2m")),
        "wind_mph": _int(cur.get("wind_speed_10m")),
        "cloud_cover": _int(cur.get("cloud_cover")),
        "precip_prob": rows[0]["precip_prob"],
        "description": desc,
        "icon": icon,
        "is_day": is_day,
    }
    return {
        "available": True,
        "location": cfg.label,
        "updated_at": fetched_at.astimezone(tz).isoformat(timespec="seconds"),
        "stale": stale,
        "step_minutes": step,
        "current": current,
        "hourly": [
            {
                "time": r["dt"].replace(tzinfo=tz).isoformat(timespec="minutes"),
                "temp_f": round(r["temp_f"], 1),
                "precip_prob": r["precip_prob"],
                "cloud_cover": r["cloud_cover"],
            }
            for r in rows
        ],
        "summary": summarize(rows),
    }


# ---------------------------------------------------------------- service

Fetcher = Callable[[Config], Awaitable[dict[str, Any]]]


async def fetch_open_meteo(cfg: Config) -> dict[str, Any]:
    # httpx's timeout is per read; bound the whole request so a trickling server
    # can't hold the refresh lock.
    async with asyncio.timeout(TIMEOUT_SECONDS + 2), httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        r = await client.get(API_URL, params=cfg.params())
        r.raise_for_status()
        data = r.json()
    if not isinstance(data, dict):
        raise ValueError("unexpected response")
    return data


class WeatherService:
    """Lazy, cached weather. One refresh at a time; last good data survives failures."""

    def __init__(self, cfg: Config | None = None, fetcher: Fetcher = fetch_open_meteo,
                 clock: Callable[[], float] = time.time):
        self.cfg = cfg or Config.from_env()
        self._fetch = fetcher
        self._clock = clock
        self._lock = asyncio.Lock()
        self._raw: dict[str, Any] | None = None
        self._fetched_at: float | None = None   # epoch seconds of last good data
        self._next_try = 0.0                    # epoch seconds; earliest next refresh

    def _now_local(self) -> datetime:
        # Aware; build_payload converts it into the response's own fixed offset.
        return datetime.fromtimestamp(self._clock(), UTC)

    async def _refresh_if_due(self) -> None:
        if self._clock() < self._next_try:
            return
        if self._lock.locked() and self._raw is not None:
            return  # a refresh is in flight; serve what we have rather than queue behind it
        async with self._lock:
            now = self._clock()
            if now < self._next_try:      # another request refreshed while we waited
                return
            try:
                raw = await self._fetch(self.cfg)
                # Validate before replacing known-good data.
                build_payload(raw, self.cfg, self._now_local(),
                              datetime.fromtimestamp(now).astimezone(), False)
            except Exception as exc:  # noqa: BLE001 - weather must never raise
                log.warning("weather refresh failed: %s", exc)
                self._next_try = now + RETRY_SECONDS
                return
            self._raw = raw
            self._fetched_at = now
            self._next_try = now + CACHE_SECONDS

    async def get(self) -> dict[str, Any]:
        try:
            await self._refresh_if_due()
            if self._raw is None or self._fetched_at is None:
                return {"available": False}
            stale = self._clock() - self._fetched_at > CACHE_SECONDS
            fetched = datetime.fromtimestamp(self._fetched_at).astimezone()
            return build_payload(self._raw, self.cfg, self._now_local(), fetched, stale)
        except Exception as exc:  # noqa: BLE001
            log.warning("weather unavailable: %s", exc)
            return {"available": False}


router = APIRouter()
_service: WeatherService | None = None


def get_service() -> WeatherService:
    global _service
    if _service is None:
        _service = WeatherService()
    return _service


@router.get("/api/weather")
async def weather():
    data = await get_service().get()
    return JSONResponse(data, headers={"Cache-Control": "no-store"})
