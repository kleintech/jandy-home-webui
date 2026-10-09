"""In-memory stand-in for the Jandy, for UI work and tests (JANDY_BACKEND=mock)."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import replace

from .. import advanced as adv
from .. import equipment
from .base import BackendError, Snapshot, Switch

# Jandy LED WaterColors effects as the iaqualink backend presents them.
COLORS = [
    "White", "Sky Blue", "Cobalt Blue", "Caribbean Blue", "Spring Green", "Emerald Green",
    "Emerald Rose", "Magenta", "Violet", "Slow Splash", "Fast Splash", "USA!",
    "Fat Tuesday", "Disco Tech",
]

# What a real panel's get_home said (flattened), minus anything identifying. The
# `response` hex ends with the ASCII panel model, like the real one.
SAMPLE_HOME = {
    "status": "Online",
    "response": "AQU='70','00 01 " + " ".join(f"{b:02X}" for b in b"B0316823 RS-4 Combo") + "'",
    "system_type": "0",
    "temp_scale": "F",
    "cover_pool": "1",
    "freeze_protection": "0",
    "solar_heater": "",
    "spa_salinity": "",
    "pool_salinity": "",
    "orp": "",
    "ph": "",
    "heatpump_info": {"isheatpumpPresent": True, "heatpumpstatus": "enabled", "isChillAvailable": True,
                      "heatpumpmode": "heat", "heatpumptype": "4-wired"},
    "swc_info": {"isswcPresent": True, "swcPoolValue": 25, "swcPoolStatus": "running"},
    "relay_count": "4",
}
SAMPLE_FIRMWARE = "4.39"

# Owner "Advanced" devices: guest switches are backed by the Snapshot fields, the rest
# by MockBackend.extra. Labels as the panel would report them.
GUEST_KEYS = {
    "filter_pump": "pool_pump", "spa_mode": "spa_pump", "spa_heater": "spa_heater",
    "pool_heater": "pool_heater", "light": "aux_1", "bubbles": "aux_2",
    "water_features": "aux_3", "spillover": "onetouch_1",
}
DEVICES = [  # key, label, kind
    ("pool_pump", "Filter pump", "pump"), ("spa_pump", "Spa mode", "pump"),
    ("spa_heater", "Spa heater", "heater"), ("pool_heater", "Pool heater", "heater"),
    ("heatpump", "Heat pump", "heatpump"),
    ("aux_1", "Pool Light", "light"), ("aux_2", "Air Blower", "aux"), ("aux_3", "Wtr Feature", "aux"),
    ("aux_4", "Cleaner", "aux"), ("aux_5", "Aux V2", "aux"), ("aux_6", "Aux V3", "aux"),
    ("onetouch_1", "Spillover", "scene"), ("onetouch_2", "All OFF", "scene"),
]
SETPOINT_RANGE = {"min": 34, "max": 104}


class MockBackend:
    def __init__(self, latency: float = 0.0) -> None:
        self.latency = latency
        self.calls: list[tuple] = []
        self.state = Snapshot(
            connected=True,
            filter_pump=True,
            spa_temp=97,
            pool_temp=84,
            air_temp=78,
            spa_set=101,
            pool_heat_set=84,
            pool_chill_set=90,
            spillover_available=True,
            light_available=True,
            light_color="White",
            light_colors=list(COLORS),
            light_cycles=True,
        )
        # Edit to try other panel readings (tests and UI work); the switch rows
        # below are filled in from the live mock state on each refresh.
        self.home = {k: (dict(v) if isinstance(v, dict) else v) for k, v in SAMPLE_HOME.items()}
        # Advanced-only devices (not guest switches), and the heat pump / salt cell.
        self.extra = {"heatpump": True, "aux_4": False, "aux_5": False, "aux_6": False, "onetouch_2": False}
        self.hp_mode = "heat"
        self.salt = {"pool_pct": 25, "spa_pct": 10, "boost": {
            "status": "off", "hours": 24, "remaining_hours": None, "remaining_mins": None,
            "mode": "pool", "dip_enabled": True}}

    async def _wait(self) -> None:
        if self.latency:
            await asyncio.sleep(self.latency)

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def refresh(self) -> Snapshot:
        s = self.state
        def bit(on: bool) -> str:
            return "1" if on else "0"
        home = {
            **self.home,
            "pool_pump": bit(s.filter_pump),
            "spa_pump": bit(s.spa_mode),
            "spa_heater": "1" if s.spa_heater else "0",
            "pool_heater": bit(s.pool_heater),
        }
        aux_on = [name for name, on in [("Pool Light", s.light_on), ("Air Blower", s.bubbles),
                                        ("Wtr Feature", s.water_features)] if on]
        return replace(
            s,
            light_colors=list(s.light_colors),
            pool_covered=equipment.pool_covered(home),
            equipment=equipment.build(home, status="Online", firmware=SAMPLE_FIRMWARE,
                                      aux_on=aux_on, scenes_on=["Spillover"] if s.spillover else []),
            advanced=self._advanced(),
        )

    # ---- owner "Advanced" controls ---------------------------------------------------

    def switch_keys(self) -> dict[str, str]:
        return dict(GUEST_KEYS)

    def _is_on(self, key: str) -> bool:
        for name, k in GUEST_KEYS.items():
            if k == key:
                return bool(getattr(self.state, "light_on" if name == "light" else name))
        return bool(self.extra.get(key))

    def _advanced(self) -> dict:
        roles = {k: n for n, k in GUEST_KEYS.items()}
        devices = {key: adv.device_entry(key, label, kind, self._is_on(key), roles.get(key))
                   for key, label, kind in DEVICES}
        light_on = self.state.light_on
        return {
            "devices": devices,
            "heatpump": {"status": "enabled" if self.extra["heatpump"] else "off", "type": "4-wired",
                         "mode": self.hp_mode, "modes": ["heat", "chill"]},
            "setpoint_ranges": {"spa": dict(SETPOINT_RANGE), "pool_heat": dict(SETPOINT_RANGE),
                                "pool_chill": dict(SETPOINT_RANGE)},
            "lights": [{"key": "aux_1", "type": "color", "effects": list(COLORS),
                        "effect": self.state.light_color if light_on else None,
                        "brightness": None, "brightness_step": None}],
            "vsp": [],  # like the real panel: no variable-speed pump
            "salt": {"present": True, "status": "running", "output": self.salt["pool_pct"]},
        }

    async def adv_set_switch(self, key: str, on: bool) -> None:
        if key not in {k for k, _, _ in DEVICES}:
            raise BackendError("the controller has no such device")
        self.calls.append(("adv_set_switch", key, on))
        await self._wait()
        for name, k in GUEST_KEYS.items():
            if k == key:
                setattr(self.state, "light_on" if name == "light" else name, on)
                return
        self.extra[key] = on

    async def adv_set_heatpump_mode(self, mode: str) -> None:
        self.calls.append(("adv_set_heatpump_mode", mode))
        await self._wait()
        self.hp_mode = mode

    async def adv_set_light_effect(self, key: str, effect: str) -> None:
        if key != "aux_1" or effect not in COLORS:
            raise BackendError("that light has no such effect")
        self.calls.append(("adv_set_light_effect", key, effect))
        await self._wait()
        self.state.light_on = True
        self.state.light_color = effect

    async def adv_set_light_brightness(self, key: str, brightness: int) -> None:
        raise BackendError("that light isn't dimmable")

    async def adv_set_vsp_preset(self, key: str, preset: str) -> None:
        raise BackendError("the controller has no such device")

    async def salt_config(self) -> dict:
        self.calls.append(("salt_config",))
        await self._wait()
        return copy.deepcopy(self.salt)

    async def set_salt(self, pool_pct: int, spa_pct: int) -> dict:
        adv.check_salt_pct(pool_pct)
        adv.check_salt_pct(spa_pct)
        self.calls.append(("set_salt", pool_pct, spa_pct))
        await self._wait()
        self.salt["pool_pct"], self.salt["spa_pct"] = pool_pct, spa_pct
        return copy.deepcopy(self.salt)

    async def salt_boost(self, action: str, hours: int | None, mode: str | None) -> dict:
        adv.boost_params(action, hours, mode)  # same validation as the real backend
        self.calls.append(("salt_boost", action, hours, mode))
        await self._wait()
        b = self.salt["boost"]
        b["status"] = {"start": "on", "stop": "off", "pause": "paused", "resume": "on"}[action]
        if action == "start":
            b.update(hours=hours, mode=mode, remaining_hours=hours, remaining_mins=0)
        elif action == "stop":
            b.update(remaining_hours=None, remaining_mins=None)
        return copy.deepcopy(self.salt)

    async def set_switch(self, name: Switch, on: bool) -> None:
        self.calls.append(("set_switch", name, on))
        await self._wait()
        attr = "light_on" if name == "light" else name
        setattr(self.state, attr, on)

    async def set_light_color(self, color: str) -> None:
        self.calls.append(("set_light_color", color))
        await self._wait()
        self.state.light_on = True
        self.state.light_color = color

    async def set_spa_setpoint(self, temp: int) -> None:
        self.calls.append(("set_spa_setpoint", temp))
        await self._wait()
        self.state.spa_set = temp

    async def set_pool_setpoints(self, heat: int, chill: int | None) -> None:
        self.calls.append(("set_pool_setpoints", heat, chill))
        await self._wait()
        self.state.pool_heat_set = heat
        if chill is not None:
            self.state.pool_chill_set = chill
