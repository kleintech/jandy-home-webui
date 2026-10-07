"""Backend for a Jandy iAqualink (iaqua) system via flz/iaqualink-py (cloud API).

Device names follow iaqualink-py: `pool_pump`, `spa_pump` (spa mode), `spa_heater`,
`pool_temp`, `spa_temp`, `spa_set_point`, `pool_set_point`, `pool_chill_set_point`
(heat pump with chill only), and positional auxes `aux_1`..`aux_7`, `aux_B1`.. whose
labels come from the panel. Every Jandy `set_*` command is a *toggle*, so this backend
only ever goes through the library's `turn_on`/`turn_off`, which check the cached
state first; the service refreshes immediately before each command so that cache is
fresh.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

from iaqualink.client import AqualinkClient
from iaqualink.device import AqualinkLight, AqualinkNumber, AqualinkSensor, AqualinkSwitch
from iaqualink.exception import AqualinkException, AqualinkServiceUnauthorizedException
from iaqualink.system import SystemStatus
from iaqualink.systems.iaqua.device import IaquaIclLight

from .base import BackendError, Snapshot, StaleData, Switch

log = logging.getLogger(__name__)

WHITE = "White"
# Anything the library or a malformed cloud reply can throw while talking to the API.
CALL_ERRORS = (AqualinkException, TimeoutError, OSError, ValueError, KeyError, TypeError)
# Wrong password: wait this long before logging in again, doubling up to the max, so a
# bad Secret doesn't hammer the login endpoint every poll.
LOGIN_BACKOFF = (60.0, 1800.0)


@dataclass(frozen=True)
class DeviceMap:
    """Which Jandy device each guest function drives. Each value is a device key
    (`aux_2`) or a panel label (`Aux V1`, matched case-insensitively)."""

    filter_pump: str = "pool_pump"
    spa_mode: str = "spa_pump"
    spa_heater: str = "spa_heater"
    pool_heater: str = "pool_heater"
    bubbles: str = "aux_2"
    water_features: str = "Aux V1"
    spillover: str = "Spillover"
    # Empty = the first light the panel reports.
    light: str = ""

    @classmethod
    def from_env(cls) -> DeviceMap:
        d = cls()
        return cls(**{f: os.environ.get(f"JANDY_{f.upper()}_DEVICE", getattr(d, f)) for f in d.__dataclass_fields__})


class IAqualinkBackend:
    def __init__(self, username: str, password: str, devices: DeviceMap | None = None,
                 serial: str | None = None, timeout: float = 30) -> None:
        self._username = username
        self._password = password
        self.map = devices or DeviceMap()
        self.serial = serial
        self.timeout = timeout
        self.client: AqualinkClient | None = None
        self.system = None
        # The API can't report a color light's current effect, so remember what we set.
        self._light_effect: str | None = None
        # A chill write blanks the value until the next poll; keep the last one.
        self._last_chill: int | None = None
        self._warned: set[str] = set()
        self._stale = False
        self._login_retry_at = 0.0
        self._login_backoff = 0.0

    @classmethod
    def from_env(cls) -> IAqualinkBackend:
        try:
            user, pw = os.environ["IAQUALINK_USERNAME"], os.environ["IAQUALINK_PASSWORD"]
        except KeyError as exc:
            raise SystemExit(f"{exc.args[0]} is not set (or use JANDY_BACKEND=mock)") from exc
        return cls(user, pw, DeviceMap.from_env(), os.environ.get("IAQUALINK_SERIAL") or None)

    # ---- connection ----------------------------------------------------------------

    async def start(self) -> None:
        if self.system is None:
            await self._connect()

    async def _connect(self) -> None:
        if time.monotonic() < self._login_retry_at:
            raise BackendError("iAqualink rejected the username/password (waiting before retrying)")
        if self.client is None:
            self.client = AqualinkClient(self._username, self._password)
        try:
            async with asyncio.timeout(self.timeout):
                await self.client.login()
                systems = await self.client.get_systems()
        except AqualinkServiceUnauthorizedException as exc:
            lo, hi = LOGIN_BACKOFF
            self._login_backoff = min(hi, self._login_backoff * 2 or lo)
            self._login_retry_at = time.monotonic() + self._login_backoff
            log.error("iAqualink rejected the username/password; next try in %.0fs", self._login_backoff)
            raise BackendError("iAqualink rejected the username/password") from exc
        except CALL_ERRORS as exc:
            raise BackendError(f"login failed: {exc or type(exc).__name__}") from exc
        self._login_backoff = 0.0
        iaqua = {k: s for k, s in systems.items() if s.type == "iaqua"}
        if self.serial:
            self.system = iaqua.get(self.serial)
        elif iaqua:
            self.system = next(iter(iaqua.values()))
        if self.system is None:
            found = ", ".join(f"{k} ({s.type})" for k, s in systems.items()) or "none"
            raise BackendError(f"no iaqua system on this account (found: {found})")
        log.info("using iAqualink system %s (%s)", self.system.name, self.system.type)
        self._watch_parses(self.system)

    def _watch_parses(self, system) -> None:
        """Flag replies the library silently ignores.

        The cloud sometimes answers with an empty `system_type`, a "NaN" aux state or
        an Offline/Service screen; iaqualink-py then skips the update but keeps its old
        device state, which looks current. Acting on it would send toggles the wrong
        way, so remember that the last refresh was incomplete.
        """

        def wrap(name, is_bad):
            orig = getattr(system, name)

            def parse(response):
                try:
                    if is_bad(response.json()):
                        self._stale = True
                except Exception:
                    self._stale = True
                return orig(response)

            setattr(system, name, parse)

        def merged(items):
            out: dict = {}
            for x in items:
                out.update(x)
            return out

        def home_bad(data):
            home = merged(data["home_screen"])
            return home.get("status") != "Online" or home.get("system_type") in ("", None)

        def devices_bad(data):
            screen = data["devices_screen"]
            if screen[0].get("status") != "Online":
                return True
            return any(
                attr.get("state") == "NaN" for x in screen[3:] for attr in next(iter(x.values()))
            )

        def onetouch_bad(data):
            return merged(data["onetouch_screen"]).get("status") != "Online"

        wrap("_parse_home_response", home_bad)
        wrap("_parse_devices_response", devices_bad)
        wrap("_parse_onetouch_response", onetouch_bad)

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()

    async def _call(self, coro_fn):
        if self.system is None:
            await self._connect()
        try:
            async with asyncio.timeout(self.timeout):
                return await coro_fn()
        except CALL_ERRORS as exc:
            raise BackendError(str(exc) or type(exc).__name__) from exc

    # ---- device lookup -------------------------------------------------------------

    def _find(self, ref: str):
        if not ref:
            return None
        devices = self.system.devices
        if ref in devices:
            return devices[ref]
        want = ref.casefold()
        for dev in devices.values():
            if str(dev.data.get("label", "")).casefold() == want:
                return dev
        if ref not in self._warned:
            self._warned.add(ref)
            log.warning("no device matches %r; known: %s", ref, self.describe_devices())
        return None

    def describe_devices(self) -> str:
        return ", ".join(f"{k}={d.data.get('label', '')!r}" for k, d in self.system.devices.items())

    def _light(self) -> AqualinkLight | None:
        if self.map.light:
            dev = self._find(self.map.light)
            return dev if isinstance(dev, AqualinkLight) else None
        return next((d for d in self.system.devices.values() if isinstance(d, AqualinkLight)), None)

    def _switch(self, name: Switch):
        dev = self._light() if name == "light" else self._find(getattr(self.map, name))
        if dev is None:
            raise BackendError(f"the controller has no device for {name.replace('_', ' ')}")
        return dev

    def _white_effect(self, light: AqualinkLight) -> str | None:
        return next((e for e in light.effect_list or [] if "white" in e.casefold()), None)

    def _colors(self, light: AqualinkLight | None) -> list[str]:
        if light is None or not light.supports_effect:
            return []
        white = self._white_effect(light)
        names = [WHITE if e == white else e for e in light.effect_list if e != "Off"]
        # White first: it's the default.
        return sorted(names, key=lambda n: n != WHITE)

    # ---- Backend -------------------------------------------------------------------

    async def refresh(self) -> Snapshot:
        if self.system is None:
            await self._connect()
        self._stale = False
        await self._call(self.system.refresh)
        if self.system.status is not SystemStatus.ONLINE:
            return Snapshot(connected=False)
        if self._stale:
            raise StaleData("the controller sent an incomplete update")

        def on(ref: str) -> bool:
            dev = self._find(ref)
            return bool(dev is not None and getattr(dev, "is_on", False))

        def temp(key: str) -> int | None:
            dev = self.system.devices.get(key)
            raw = dev.current_value if isinstance(dev, AqualinkNumber) else (
                dev.value if isinstance(dev, AqualinkSensor) else None)
            try:
                return int(float(raw))
            except (TypeError, ValueError):
                return None

        chill_dev = self.system.devices.get("pool_chill_set_point")
        chill = temp("pool_chill_set_point")
        if chill is None and chill_dev is not None:
            chill = self._last_chill
        self._last_chill = chill

        light = self._light()
        light_on = bool(light and light.is_on)
        if not light_on:
            self._light_effect = None
        elif isinstance(light, IaquaIclLight) and light.effect in (light.effect_list or []):
            # ICL zones do report their color; aux color lights don't.
            reported = light.effect
            self._light_effect = WHITE if reported == self._white_effect(light) else reported
        unit = getattr(self.system, "temp_unit", None)

        return Snapshot(
            connected=True,
            unit="C" if unit is not None and str(getattr(unit, "value", unit)).upper().startswith("C") else "F",
            filter_pump=on(self.map.filter_pump),
            spa_mode=on(self.map.spa_mode),
            spa_heater=on(self.map.spa_heater),
            pool_heater=on(self.map.pool_heater),
            spa_temp=temp("spa_temp"),
            pool_temp=temp("pool_temp"),
            air_temp=temp("air_temp"),
            spa_set=temp("spa_set_point"),
            pool_heat_set=temp("pool_set_point"),
            pool_chill_set=chill if chill_dev is not None else None,
            bubbles=on(self.map.bubbles),
            water_features=on(self.map.water_features),
            spillover=on(self.map.spillover),
            spillover_available=self._find(self.map.spillover) is not None,
            light_available=light is not None,
            light_on=light_on,
            light_color=self._light_effect,
            light_colors=self._colors(light),
        )

    async def set_switch(self, name: Switch, on: bool) -> None:
        dev = self._switch(name)
        if not isinstance(dev, (AqualinkSwitch, AqualinkLight)):
            raise BackendError(f"{name} is not a switch")
        await self._call(dev.turn_on if on else dev.turn_off)
        if name == "light" and not on:
            self._light_effect = None

    async def set_light_color(self, color: str) -> None:
        light = self._light()
        if light is None or not light.supports_effect:
            raise BackendError("the pool light has no colors")
        effect = self._white_effect(light) if color == WHITE else color
        if effect not in (light.effect_list or []):
            raise BackendError(f"unknown color {color!r}")
        if isinstance(light, IaquaIclLight) and not light.is_on:
            # An ICL zone has a separate on/off command; a color alone may not light it.
            await self._call(light.turn_on)
        await self._call(lambda: light.set_effect(effect))
        self._light_effect = color

    async def set_spa_setpoint(self, temp: int) -> None:
        dev = self.system.devices.get("spa_set_point") if self.system else None
        if not isinstance(dev, AqualinkNumber):
            raise BackendError("the controller has no spa set point")
        await self._call(lambda: dev.set_value(temp))

    async def set_pool_setpoints(self, heat: int, chill: int | None) -> None:
        devices = self.system.devices if self.system else {}
        heat_dev = devices.get("pool_set_point")
        if not isinstance(heat_dev, AqualinkNumber):
            raise BackendError("the controller has no pool set point")
        chill_dev = devices.get("pool_chill_set_point")
        if chill is not None and not isinstance(chill_dev, AqualinkNumber):
            raise BackendError("the controller has no chill set point")

        async def write_heat():
            if heat_dev.current_value != heat:
                await self._call(lambda: heat_dev.set_value(heat))

        async def write_chill():
            if chill is not None:
                await self._call(lambda: chill_dev.set_value(chill))
                self._last_chill = chill

        # Heat is the low set point, chill the high one. Order the two writes so the
        # spread holds even if the second one fails: moving down, lower heat first;
        # moving up, raise chill first.
        current_heat = heat_dev.current_value
        if current_heat is not None and heat < current_heat:
            await write_heat()
            await write_chill()
        else:
            await write_chill()
            await write_heat()
