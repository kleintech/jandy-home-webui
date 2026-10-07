"""In-memory stand-in for the Jandy, for UI work and tests (JANDY_BACKEND=mock)."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from .base import Snapshot, Switch

# Jandy LED WaterColors effects as the iaqualink backend presents them.
COLORS = [
    "White", "Sky Blue", "Cobalt Blue", "Caribbean Blue", "Spring Green", "Emerald Green",
    "Emerald Rose", "Magenta", "Violet", "Slow Splash", "Fast Splash", "USA!",
    "Fat Tuesday", "Disco Tech",
]


class MockBackend:
    def __init__(self, latency: float = 0.0) -> None:
        self.latency = latency
        self.calls: list[tuple] = []
        self.state = Snapshot(
            connected=True,
            filter_pump=True,
            spa_temp=97,
            pool_temp=84,
            spa_set=101,
            pool_heat_set=90,
            pool_chill_set=85,
            spillover_available=True,
            light_available=True,
            light_color="White",
            light_colors=list(COLORS),
        )

    async def _wait(self) -> None:
        if self.latency:
            await asyncio.sleep(self.latency)

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def refresh(self) -> Snapshot:
        return replace(self.state, light_colors=list(self.state.light_colors))

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
