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
from typing import get_args

from iaqualink.client import AqualinkClient
from iaqualink.device import AqualinkLight, AqualinkNumber, AqualinkSensor, AqualinkSwitch
from iaqualink.exception import AqualinkException, AqualinkServiceUnauthorizedException
from iaqualink.system import SystemStatus
from iaqualink.systems.iaqua.device import (
    IaquaColorLight,
    IaquaDimmableLight,
    IaquaHeater,
    IaquaHeatPump,
    IaquaHeatPumpMode,
    IaquaIclLight,
    IaquaOneTouchSwitch,
    IaquaSwitch,
    IaquaVSPump,
)

from .. import advanced as adv
from .. import equipment
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
        # Last effect set per color light key (aux color lights can't report theirs).
        self._adv_effects: dict[str, str] = {}
        # Latest good get_home reply, flattened, for the read-only equipment view
        # (iaqualink-py doesn't parse swc_info, cover_pool, firmware, ...).
        self._home: dict | None = None
        self._home_fw: str | None = None
        self._home_status: str | None = None

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
            # Details (which may include URLs) go to the log, not to guests.
            log.warning("iAqualink login failed: %r", exc)
            raise BackendError("couldn't sign in to iAqualink") from exc
        self._login_backoff = 0.0
        iaqua = {k: s for k, s in systems.items() if s.type == "iaqua"}
        if self.serial:
            self.system = iaqua.get(self.serial)
        elif iaqua:
            self.system = next(iter(iaqua.values()))
        if self.system is None:
            # Masked: this message can reach the page and the log.
            found = ", ".join(f"…{k[-4:]} ({s.type})" for k, s in systems.items()) or "none"
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

        def wrap(name, is_bad, on_good=None):
            orig = getattr(system, name)

            def parse(response):
                try:
                    data = response.json()
                    if is_bad(data):
                        self._stale = True
                    elif on_good is not None:
                        on_good(data)
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
            status = home.get("status")
            self._home_status = status if isinstance(status, str) else None
            return status != "Online" or home.get("system_type") in ("", None)

        def home_good(data):
            # Only a complete, Online reply replaces what the equipment view shows.
            self._home = merged(data["home_screen"])
            fw = data.get("attached_system_fw_version")
            self._home_fw = fw if isinstance(fw, (str, int, float)) else None

        def devices_bad(data):
            screen = data["devices_screen"]
            if screen[0].get("status") != "Online":
                return True
            return any(
                attr.get("state") == "NaN" for x in screen[3:] for attr in next(iter(x.values()))
            )

        def onetouch_bad(data):
            return merged(data["onetouch_screen"]).get("status") != "Online"

        wrap("_parse_home_response", home_bad, home_good)
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
            log.warning("iAqualink call failed: %r", exc)
            raise BackendError("the pool controller didn't respond") from exc

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
            # Only the panel's own status: the rest of the last reply may be old.
            return Snapshot(connected=False, equipment=equipment.build(
                None, status=self._home_status or "Offline", secrets=self._secrets()))
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
            # Relay color lights pick a color by switching power on and off; ICL zones don't.
            light_cycles=isinstance(light, IaquaColorLight),
            pool_covered=equipment.pool_covered(self._home),
            equipment=self._equipment(),
            advanced=self._advanced(),
        )

    def _secrets(self) -> list[str]:
        """Strings that must never show up in the equipment view."""
        system_serial = getattr(self.system, "serial", None)
        return [x for x in (self.serial, system_serial, self._username) if isinstance(x, str) and x]

    def _equipment(self) -> dict:
        """Read-only status rows from the last good refresh. Never sends anything."""
        alert_dev = self.system.devices.get("heatpump_alert")

        def on_labels(prefix: str) -> list[str]:
            return [
                str(d.data.get("label") or k)
                for k, d in self.system.devices.items()
                if k.startswith(prefix) and isinstance(d, (AqualinkSwitch, AqualinkLight)) and d.is_on
            ]

        try:
            return equipment.build(
                self._home,
                status=self._home_status,
                firmware=self._home_fw,
                heatpump_alert=alert_dev.data.get("state") if alert_dev is not None else None,
                aux_on=on_labels("aux_"),
                scenes_on=on_labels("onetouch_") if any(
                    k.startswith("onetouch_") for k in self.system.devices) else None,
                secrets=self._secrets(),
            )
        except Exception as exc:  # the advanced view must never break guest state
            log.warning("equipment view failed: %r", exc)
            return {}

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
        self._adv_effects[light.name] = effect

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

    # ---- owner "Advanced" controls -------------------------------------------------
    # Every write maps an allowlisted device key to the library's device object and
    # calls its own method (turn_on/turn_off check the cached state, which the service
    # refreshed just before). No command string is ever built from a request.

    def switch_keys(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.system is None:
            return out
        for name in get_args(Switch):
            dev = self._light() if name == "light" else self._find(getattr(self.map, name))
            if dev is not None:
                out[name] = dev.name
        return out

    def _adv_kind(self, key: str, dev) -> str | None:
        """Which owner switch kind a device is, or None if it isn't one we expose."""
        if isinstance(dev, IaquaHeatPump):
            return "heatpump"
        if isinstance(dev, IaquaVSPump):
            return "vsp"
        if isinstance(dev, IaquaIclLight):
            return "light"
        if isinstance(dev, IaquaOneTouchSwitch):
            return "scene"
        if key.startswith("aux_") and isinstance(dev, AqualinkLight):
            return "light"
        if key.startswith("aux_") and isinstance(dev, AqualinkSwitch):
            return "aux"
        if isinstance(dev, IaquaHeater) and key in adv.HOME_LABELS:
            # The panel reports "" for equipment it doesn't have (solar on this panel).
            if key == "solar_heater" and not str(dev.data.get("state") or "").strip():
                return None
            return "heater"
        if isinstance(dev, IaquaSwitch) and key in adv.HOME_LABELS:
            return "pump"
        return None

    def _adv_devices(self) -> dict:
        """key -> library device, for every device an owner may switch. This is the
        allowlist every advanced write is checked against."""
        if self.system is None:
            return {}
        return {k: d for k, d in self.system.devices.items() if self._adv_kind(k, d)}

    def _adv_device(self, key: str, *types):
        dev = self._adv_devices().get(key)
        if dev is None or (types and not isinstance(dev, types)):
            raise BackendError("the controller has no such device")
        return dev

    def _advanced(self) -> dict:
        try:
            return self._advanced_view()
        except Exception as exc:  # never break the guest snapshot over the owner view
            log.warning("advanced view failed: %r", exc)
            return {}

    def _advanced_view(self) -> dict:
        secrets = self._secrets()
        roles: dict[str, str] = {}
        for name, key in self.switch_keys().items():
            roles.setdefault(key, name)
        devices: dict[str, dict] = {}
        lights: list[dict] = []
        vsp: list[dict] = []
        for key, dev in self._adv_devices().items():
            kind = self._adv_kind(key, dev)
            if key in adv.HOME_LABELS:
                raw_label = adv.HOME_LABELS[key]
            elif isinstance(dev, IaquaIclLight):
                raw_label = dev.label
            else:
                raw_label = dev.data.get("label")
            label = adv.clean_label(raw_label, key, secrets)
            on = bool(getattr(dev, "is_on", False))
            devices[key] = adv.device_entry(key, label, kind, on, roles.get(key))
            if kind == "light":
                lights.append(self._light_entry(key, dev, on))
            elif kind == "vsp":
                presets, preset = [], None
                if dev.supports_presets:
                    presets = [str(p) for p in dev.preset_modes]
                    preset = dev.preset_mode
                vsp.append({"key": key, "presets": presets, "preset": preset})

        hp = self.system.devices.get("heatpump")
        heatpump = None
        if isinstance(hp, IaquaHeatPump):
            mode_dev = self.system.devices.get("heatpump_mode")
            heatpump = {
                "status": adv.text(hp.data.get("state")) or None,
                "type": adv.text(hp.data.get("hpm_type")) or None,
                "mode": mode_dev.current_option if isinstance(mode_dev, IaquaHeatPumpMode) else None,
                "modes": [str(o) for o in mode_dev.options] if isinstance(mode_dev, IaquaHeatPumpMode) else [],
            }

        def rng(key: str) -> dict | None:
            dev = self.system.devices.get(key)
            if not isinstance(dev, AqualinkNumber):
                return None
            try:
                return {"min": int(dev.min_value), "max": int(dev.max_value)}
            except Exception:
                return None  # temperature unit not known yet: no writes

        swc = (self._home or {}).get("swc_info")
        swc = swc if isinstance(swc, dict) else {}
        out = swc.get("swcPoolValue")
        return {
            "devices": devices,
            "heatpump": heatpump,
            "setpoint_ranges": {
                "spa": rng("spa_set_point"),
                "pool_heat": rng("pool_set_point"),
                "pool_chill": rng("pool_chill_set_point"),
            },
            "lights": lights,
            # Only pumps the library discovered (system.is_vsp); none means none shown.
            "vsp": vsp,
            "salt": {
                "present": swc.get("isswcPresent") is True,
                "status": adv.text(swc.get("swcPoolStatus")) or None,
                "output": out if isinstance(out, (int, float)) and not isinstance(out, bool) else None,
            },
        }

    def _light_entry(self, key: str, dev, on: bool) -> dict:
        if isinstance(dev, IaquaIclLight):
            ltype, step = "icl", 5
            effect = dev.effect if on else None
        elif isinstance(dev, IaquaColorLight):
            ltype, step = "color", None
            effect = self._adv_effects.get(key) if on else None
        elif isinstance(dev, IaquaDimmableLight):
            ltype, step, effect = "dimmable", 25, None
        else:
            ltype, step, effect = "switch", None, None
        if not on:
            self._adv_effects.pop(key, None)
        effects: list[str] = []
        if ltype in ("icl", "color"):
            effects = [e for e in (dev.effect_list or []) if e != "Off"]
        brightness = None
        if step is not None:
            try:
                brightness = dev.brightness_percentage
            except (TypeError, ValueError, KeyError):
                brightness = None
        return {"key": key, "type": ltype, "effects": effects, "effect": effect,
                "brightness": brightness, "brightness_step": step}

    async def adv_set_switch(self, key: str, on: bool) -> None:
        dev = self._adv_device(key)
        await self._call(dev.turn_on if on else dev.turn_off)
        if not on:
            self._adv_effects.pop(key, None)

    async def adv_set_heatpump_mode(self, mode: str) -> None:
        dev = self.system.devices.get("heatpump_mode") if self.system else None
        if not isinstance(dev, IaquaHeatPumpMode) or mode not in dev.options:
            raise BackendError("the heat pump has no such mode")
        if dev.current_option == mode:
            return
        await self._call(lambda: dev.select_option(mode))

    async def adv_set_light_effect(self, key: str, effect: str) -> None:
        dev = self._adv_device(key, IaquaIclLight, IaquaColorLight)
        if effect == "Off" or effect not in (dev.effect_list or []):
            raise BackendError("that light has no such effect")
        if isinstance(dev, IaquaIclLight) and not dev.is_on:
            # An ICL zone has a separate on/off command; a color alone may not light it.
            await self._call(dev.turn_on)
        await self._call(lambda: dev.set_effect(effect))
        self._adv_effects[key] = effect

    async def adv_set_light_brightness(self, key: str, brightness: int) -> None:
        dev = self._adv_device(key, IaquaIclLight, IaquaDimmableLight)
        await self._call(lambda: dev.set_brightness_percentage(brightness))

    async def adv_set_vsp_preset(self, key: str, preset: str) -> None:
        dev = self._adv_device(key, IaquaVSPump)
        if not dev.supports_presets or preset not in dev.preset_modes:
            raise BackendError("that pump has no such speed")
        await self._call(lambda: dev.set_preset_mode(preset))

    # Salt cell (AquaPure / SWC). iaqualink-py has no support for these; the commands
    # and parameters come from the iAqualink protocol reference
    # (iaqualink-py docs/reference/systems/iaqua.md, "SWC"), sent through the
    # library's session request like every other iaqua command. DOCUMENTED BUT NOT
    # VERIFIED on this panel: the library notes that SWC endpoints may live on the v1
    # r-api host instead; if the panel rejects them, the owner sees a generic error
    # and nothing else changes.

    async def _swc(self, command: str, params: dict | None = None) -> dict:
        if command not in (adv.SWC_GET, adv.SWC_SET, adv.SWC_BOOST):
            raise BackendError("unsupported salt cell command")

        async def go():
            r = await self.system._send_session_request(command, params)
            return r.json()

        data = await self._call(go)
        try:
            return adv.parse_swc_config(data)
        except ValueError as exc:
            log.warning("salt cell %s refused or unreadable: %s", command, exc)
            raise BackendError("the salt cell didn't accept that") from exc

    async def salt_config(self) -> dict:
        return await self._swc(adv.SWC_GET)

    async def set_salt(self, pool_pct: int, spa_pct: int) -> dict:
        adv.check_salt_pct(pool_pct)
        adv.check_salt_pct(spa_pct)
        return await self._swc(adv.SWC_SET, {"poolswcsp": str(pool_pct), "spaswcsp": str(spa_pct)})

    async def salt_boost(self, action: str, hours: int | None, mode: str | None) -> dict:
        return await self._swc(adv.SWC_BOOST, adv.boost_params(action, hours, mode))
