"""In-memory stand-in for the Jandy, for UI work and tests (JANDY_BACKEND=mock)."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from .. import equipment
from .base import Snapshot, Switch

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
        )

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
