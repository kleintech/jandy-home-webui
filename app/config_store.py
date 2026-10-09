"""Runtime app configuration: what guests see, their limits, the Hot Tub On/Off
sequences, the guest toggles and the weather location.

The document (see `AppConfig`) is edited by the owner through `PUT /api/config` and
kept as JSON at `CONFIG_PATH` (default /data/config.json). With no saved file the
app runs on defaults built from today's environment variables (SPA_MAX, ...,
JANDY_*_DEVICE through the backend's device map, WEATHER_*), so a fresh install
behaves exactly as before this existed.

- Saves are atomic (temp file in the same directory, fsync, rename, fsync the
  directory), so a crash or power cut leaves either the old file or the new one.
- A missing, unreadable or invalid file is logged and the defaults are used; it
  never stops the app.
- Every saved document carries a `version`; a save names the version it edited and
  is refused (409) when that is no longer current (optimistic concurrency).

Validation is in two layers: the pydantic models check shape and types, and
`check_document` checks the house rules (ranges, unique ids, conflicts, step
counts). Whether a device key exists on the panel is checked by the service
(`PoolService.check_config_devices`), which knows the panel.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError

log = logging.getLogger(__name__)

DEFAULT_PATH = "/data/config.json"
SCHEMA = 1
# The panel's own set point range; guest limits must fit inside it.
PANEL_MIN, PANEL_MAX = 34, 104
MAX_STEPS = 12
MAX_TOGGLES = 20
TOGGLE_ID = re.compile(r"[a-z0-9_]{1,30}")
KEY_RE = r"^[A-Za-z0-9_]{1,40}$"

STALE_DETAIL = "Settings changed elsewhere; reload"
STORAGE_DETAIL = "Settings storage isn't available"


@dataclass(frozen=True)
class Limits:
    spa_min: int = 80
    spa_max: int = 103
    # Heat is the LOW pool set point (heat below it), chill the HIGH one (cool above it),
    # and chill must stay at least min_spread above heat.
    pool_heat_min: int = 82
    pool_heat_max: int = 92
    pool_chill_max: int = 92
    min_spread: int = 5


class ConfigInvalid(ValueError):
    """The document breaks a rule (HTTP 422); the message names the field."""


class StaleVersion(Exception):
    """The edit was based on an older version (HTTP 409)."""


class StorageUnavailable(Exception):
    """CONFIG_PATH can't be written (HTTP 503)."""


class _M(BaseModel):
    # Unknown fields are ignored rather than refused, so a newer page talking to an
    # older server (or the reverse) doesn't fail on a field one side doesn't know.
    model_config = ConfigDict(extra="ignore")


class MainPage(_M):
    weather: StrictBool = True
    weather_chart: StrictBool = True
    swim: StrictBool = True
    temps: StrictBool = True
    mode_switch: StrictBool = True
    light: StrictBool = True
    light_color: StrictBool = True
    setpoints: StrictBool = True
    toggles: StrictBool = True


class LimitsDoc(_M):
    spa_min: StrictInt
    spa_max: StrictInt
    pool_heat_min: StrictInt
    pool_heat_max: StrictInt
    pool_chill_max: StrictInt
    min_spread: StrictInt

    def to_limits(self) -> Limits:
        return Limits(**self.model_dump())


class SwitchStep(_M):
    action: Literal["switch"]
    key: str = Field(pattern=KEY_RE)
    on: StrictBool


class SceneStep(_M):
    action: Literal["scene"]
    key: str = Field(pattern=KEY_RE)
    on: StrictBool


class LightStep(_M):
    action: Literal["light"]
    key: str = Field(pattern=KEY_RE)
    on: StrictBool = True
    effect: str | None = Field(default=None, min_length=1, max_length=40)


class SpaMaxStep(_M):
    action: Literal["spa_setpoint_max"]
    value: StrictInt


Step = Annotated[SwitchStep | SceneStep | LightStep | SpaMaxStep, Field(discriminator="action")]


class GuestToggle(_M):
    id: str
    key: str = Field(pattern=KEY_RE)
    label: str
    modes: list[Literal["pool", "spa"]]
    conflicts: list[str] = Field(default_factory=list)


class WeatherDoc(_M):
    zip: str | None = None
    country: str = "us"
    lat: float | None = None
    lon: float | None = None
    label: str = ""


class AppConfig(_M):
    version: StrictInt = 0
    main_page: MainPage = Field(default_factory=MainPage)
    limits: LimitsDoc
    hot_tub_on: list[Step]
    hot_tub_off: list[Step]
    guest_toggles: list[GuestToggle]
    weather: WeatherDoc

    def toggle(self, tid: str) -> GuestToggle | None:
        return next((t for t in self.guest_toggles if t.id == tid), None)


# ---- validation ---------------------------------------------------------------------

STEP_TAGS = ("switch", "scene", "light", "spa_setpoint_max")


def _loc(loc: tuple) -> str:
    # Drop the union tag pydantic puts after a step index: hot_tub_on.0.switch.key
    out = [str(x) for i, x in enumerate(loc)
           if not (i == 2 and loc[0] in ("hot_tub_on", "hot_tub_off") and x in STEP_TAGS)]
    return ".".join(out) or "body"


def parse(data: Any) -> AppConfig:
    """Shape/type validation; ConfigInvalid names the first bad field."""
    if not isinstance(data, dict):
        raise ConfigInvalid("settings must be a JSON object")
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        err = exc.errors()[0]
        raise ConfigInvalid(f"{_loc(err['loc'])}: {err['msg']}") from None


def limit_problems(lim: Limits, names: dict[str, str] | None = None) -> list[str]:
    """Consistency rules for guest limits (shared with the start-up env check)."""
    n = names or {k: k for k in ("spa_min", "spa_max", "pool_heat_min", "pool_heat_max",
                                 "pool_chill_max", "min_spread")}
    return [
        msg for bad, msg in [
            (lim.spa_min > lim.spa_max, f"{n['spa_min']} > {n['spa_max']}"),
            # At least 1: with 0, heat and chill could meet and the heat pump would
            # heat and chill against itself.
            (lim.min_spread < 1, f"{n['min_spread']} < 1"),
            (lim.pool_heat_min > lim.pool_heat_max, f"{n['pool_heat_min']} > {n['pool_heat_max']}"),
            (lim.pool_heat_min + lim.min_spread > lim.pool_chill_max,
             f"{n['pool_heat_min']} + {n['min_spread']} > {n['pool_chill_max']}"),
        ] if bad
    ]


def check_document(cfg: AppConfig) -> AppConfig:
    """House rules on a parsed document. Returns it with conflicts made symmetric."""
    lim = cfg.limits
    for name in ("spa_min", "spa_max", "pool_heat_min", "pool_heat_max", "pool_chill_max"):
        v = getattr(lim, name)
        if not PANEL_MIN <= v <= PANEL_MAX:
            raise ConfigInvalid(f"limits.{name}: must be between {PANEL_MIN} and {PANEL_MAX}")
    if not 1 <= lim.min_spread <= PANEL_MAX - PANEL_MIN:
        raise ConfigInvalid(f"limits.min_spread: must be between 1 and {PANEL_MAX - PANEL_MIN}")
    problems = limit_problems(lim.to_limits(), {k: f"limits.{k}" for k in lim.model_dump()})
    if problems:
        raise ConfigInvalid("; ".join(problems))

    for seq in ("hot_tub_on", "hot_tub_off"):
        steps = getattr(cfg, seq)
        if len(steps) > MAX_STEPS:
            raise ConfigInvalid(f"{seq}: at most {MAX_STEPS} steps")
        for i, s in enumerate(steps):
            where = f"{seq}.{i}"
            if isinstance(s, SpaMaxStep) and not PANEL_MIN <= s.value <= PANEL_MAX:
                raise ConfigInvalid(f"{where}.value: must be between {PANEL_MIN} and {PANEL_MAX}")
            if isinstance(s, LightStep) and not s.on and s.effect is not None:
                raise ConfigInvalid(f"{where}.effect: can't set an effect while turning the light off")
            if isinstance(s, SceneStep) and not re.fullmatch(r"onetouch_\d{1,2}", s.key):
                raise ConfigInvalid(f"{where}.key: a scene step needs a OneTouch key (onetouch_N)")

    toggles = cfg.guest_toggles
    if len(toggles) > MAX_TOGGLES:
        raise ConfigInvalid(f"guest_toggles: at most {MAX_TOGGLES}")
    ids: set[str] = set()
    for i, t in enumerate(toggles):
        where = f"guest_toggles.{i}"
        if not TOGGLE_ID.fullmatch(t.id):
            raise ConfigInvalid(f"{where}.id: use 1-30 lowercase letters, digits or _")
        if t.id in ids:
            raise ConfigInvalid(f"{where}.id: duplicate id {t.id!r}")
        ids.add(t.id)
        label = " ".join(t.label.split())
        if not 1 <= len(label) <= 30:
            raise ConfigInvalid(f"{where}.label: 1-30 characters")
        t.label = label
        if not t.modes:
            raise ConfigInvalid(f"{where}.modes: pick pool, spa or both")
        t.modes = [m for m in ("pool", "spa") if m in t.modes]
    for i, t in enumerate(toggles):
        for c in t.conflicts:
            if c not in ids:
                raise ConfigInvalid(f"guest_toggles.{i}.conflicts: no toggle with id {c!r}")
            if c == t.id:
                raise ConfigInvalid(f"guest_toggles.{i}.conflicts: a toggle can't conflict with itself")
    # Symmetric: if A blocks B, B blocks A.
    pairs = {(t.id, c) for t in toggles for c in t.conflicts}
    for t in toggles:
        t.conflicts = [o.id for o in toggles if (t.id, o.id) in pairs or (o.id, t.id) in pairs]

    w = cfg.weather
    w.zip = (w.zip or "").strip() or None
    w.label = " ".join(w.label.split())
    w.country = (w.country or "us").strip().lower()
    if w.zip is not None and not re.fullmatch(r"[A-Za-z0-9 -]{2,10}", w.zip):
        raise ConfigInvalid("weather.zip: 2-10 letters, digits, spaces or dashes")
    if not re.fullmatch(r"[a-z]{2}", w.country):
        raise ConfigInvalid("weather.country: a two-letter country code")
    if len(w.label) > 40:
        raise ConfigInvalid("weather.label: at most 40 characters")
    if (w.lat is None) != (w.lon is None):
        raise ConfigInvalid("weather.lat: set both lat and lon, or neither")
    if w.lat is not None and not (-90 <= w.lat <= 90 and -180 <= w.lon <= 180):
        raise ConfigInvalid("weather.lat: latitude -90..90 and longitude -180..180")
    return cfg


# ---- defaults -----------------------------------------------------------------------

DEFAULT_TOGGLES = [  # id (= guest switch role), label, modes, conflicts
    ("bubbles", "Bubbles", ["spa"], []),
    ("spillover", "Spillover", ["pool"], ["water_features"]),
    ("water_features", "Water Features", ["pool"], ["spillover"]),
]


def build_defaults(limits: Limits, weather: dict[str, Any], keys: dict[str, str]) -> AppConfig:
    """Today's behaviour as a document. `keys` is the backend's guest switch -> device
    key map (JANDY_*_DEVICE resolved against the panel); a guest toggle whose device
    doesn't resolve is left out, as the old UI hid an unavailable Spillover."""

    def key(role: str, fallback: str) -> str:
        return keys.get(role) or fallback

    def switch(role: str, fallback: str, on: bool) -> dict[str, Any]:
        k = key(role, fallback)
        action = "scene" if re.fullmatch(r"onetouch_\d{1,2}", k) else "switch"
        return {"action": action, "key": k, "on": on}

    present = {tid for tid, *_ in DEFAULT_TOGGLES if keys.get(tid)}
    toggles = [
        {"id": tid, "key": keys[tid], "label": label, "modes": modes,
         "conflicts": [c for c in conflicts if c in present]}
        for tid, label, modes, conflicts in DEFAULT_TOGGLES if tid in present
    ]
    lim = {k: getattr(limits, k) for k in ("spa_min", "spa_max", "pool_heat_min", "pool_heat_max",
                                           "pool_chill_max", "min_spread")}
    # Typed through the model, but not held to check_document's ranges: env limits
    # already passed the start-up check and are the operator's call.
    return AppConfig.model_validate({
        "version": 0,
        "main_page": {},
        "limits": lim,
        "hot_tub_on": [
            # Pump first: spa mode moves the valves, and the heater needs flow.
            switch("filter_pump", "pool_pump", True),
            switch("spa_mode", "spa_pump", True),
            # The guest cap applies to whatever is already on the panel too.
            {"action": "spa_setpoint_max", "value": limits.spa_max},
            switch("spa_heater", "spa_heater", True),
        ],
        "hot_tub_off": [
            switch("spa_heater", "spa_heater", False),
            switch("spa_mode", "spa_pump", False),
        ],
        "guest_toggles": toggles,
        "weather": weather,
    })


def weather_from_env() -> dict[str, Any]:
    from . import weather

    cfg = weather.Config.from_env()
    if cfg is None:
        return {"zip": None, "country": (os.environ.get("WEATHER_COUNTRY") or "us").lower(),
                "lat": None, "lon": None, "label": os.environ.get("WEATHER_LABEL") or ""}
    return {"zip": cfg.zip, "country": cfg.country, "lat": cfg.lat, "lon": cfg.lon, "label": cfg.label}


# ---- store --------------------------------------------------------------------------

def public(cfg: AppConfig) -> dict[str, Any]:
    return cfg.model_dump(mode="json")


class ConfigStore:
    """The live config. `path=None` keeps it in memory only (tests, embedding)."""

    def __init__(self, path: str | Path | None = None, limits: Limits | None = None,
                 weather: dict[str, Any] | None = None) -> None:
        self.path = Path(path) if path else None
        self.env_limits = limits or Limits()
        self.env_weather = weather if weather is not None else weather_from_env()
        self._saved: AppConfig | None = None
        self._defaults: AppConfig | None = None
        self.version = 0
        self._load()

    @classmethod
    def from_env(cls, limits: Limits) -> ConfigStore:
        return cls(os.environ.get("CONFIG_PATH") or DEFAULT_PATH, limits)

    @property
    def saved(self) -> bool:
        return self._saved is not None

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            log.info("no saved settings at %s; using defaults from the environment", self.path)
            return
        except OSError as exc:
            log.error("can't read settings %s (%s); using defaults", self.path, exc)
            return
        try:
            cfg = check_document(parse(json.loads(raw)))
        except (ValueError, ConfigInvalid) as exc:
            # The next save replaces the file.
            log.error("ignoring invalid settings file %s (%s); using defaults", self.path, exc)
            return
        self._saved = cfg
        self.version = cfg.version

    def current(self, keys: dict[str, str]) -> AppConfig:
        """The live document. Defaults depend on which devices the panel resolved,
        so they are rebuilt; when they change the version moves, which tells pages
        to refetch."""
        if self._saved is not None:
            return self._saved
        d = build_defaults(self.env_limits, self.env_weather, keys)
        if self._defaults is None or public(d) != public(self._defaults):
            if self._defaults is not None:
                self.version += 1
            self._defaults = d
        return self._defaults.model_copy(update={"version": self.version})

    def save(self, cfg: AppConfig, expected_version: int) -> AppConfig:
        """Persist and apply. Synchronous (no await between the version check and
        the swap), so two concurrent saves can't both pass the check."""
        if expected_version != self.version:
            raise StaleVersion(STALE_DETAIL)
        new = cfg.model_copy(update={"version": self.version + 1})
        if self.path is not None:
            self._write(new)
        self._saved = new
        self.version = new.version
        return new

    def reset(self) -> None:
        if self.path is not None:
            try:
                self.path.unlink(missing_ok=True)
            except OSError as exc:
                log.error("can't delete settings %s: %s", self.path, exc)
                raise StorageUnavailable(STORAGE_DETAIL) from exc
        self._saved = None
        self._defaults = None
        self.version += 1

    def _write(self, cfg: AppConfig) -> None:
        assert self.path is not None
        data = json.dumps({"schema": SCHEMA, **public(cfg)}, indent=2, sort_keys=True) + "\n"
        d = self.path.parent
        tmp = None
        try:
            d.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=d)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            tmp = None
            try:
                dfd = os.open(d, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass  # some filesystems can't fsync a directory; the rename is done
        except OSError as exc:
            log.error("can't save settings to %s: %s", self.path, exc)
            raise StorageUnavailable(STORAGE_DETAIL) from exc
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
