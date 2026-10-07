"""Guest-level pool rules on top of a Backend.

Everything a guest can do goes through here, so the limits (spa max 103, pool heat
max 92, chill min 82, 5 degree spread, spillover vs water features) are enforced on
the server and not just by the sliders.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .backends.base import Backend, BackendError, Snapshot, Switch

log = logging.getLogger(__name__)

DEFAULT_COLOR = "White"


@dataclass(frozen=True)
class Limits:
    spa_min: int = 80
    spa_max: int = 103
    pool_heat_min: int = 70
    pool_heat_max: int = 92
    pool_chill_min: int = 82
    min_spread: int = 5


class RuleError(Exception):
    """A request the house rules don't allow (maps to HTTP 409/422)."""


class PoolService:
    def __init__(self, backend: Backend, limits: Limits | None = None, poll_seconds: float = 15) -> None:
        self.backend = backend
        self.limits = limits or Limits()
        self.poll_seconds = poll_seconds
        self.snap = Snapshot()
        self.updated_at: datetime | None = None
        self.busy: str | None = None
        self._lock = asyncio.Lock()
        self._poller: asyncio.Task | None = None

    # ---- lifecycle -----------------------------------------------------------------

    async def start(self) -> None:
        try:
            await self.backend.start()
            await self._refresh()
        except BackendError as exc:
            # Come up anyway; the poller keeps retrying and the UI shows "can't reach".
            log.warning("initial connect failed: %s", exc)
        self._poller = asyncio.create_task(self._poll_loop())

    async def close(self) -> None:
        if self._poller:
            self._poller.cancel()
        await self.backend.close()

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self.poll_seconds)
            if self._lock.locked():
                continue  # a command is running and will refresh when it finishes
            try:
                async with self._lock:
                    await self._refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep polling whatever happens
                log.warning("poll failed: %s", exc)

    async def _refresh(self) -> Snapshot:
        try:
            self.snap = await self.backend.refresh()
        except BackendError:
            self.snap.connected = False
            raise
        self.updated_at = datetime.now(UTC)
        return self.snap

    # ---- state ---------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        s, lim = self.snap, self.limits
        chill_supported = s.pool_chill_set is not None
        return {
            "connected": s.connected,
            "busy": self.busy is not None,
            "busy_mode": self.busy,
            "unit": s.unit,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "mode": "spa" if s.spa_mode else "pool",
            "light": {
                "available": s.light_available,
                "on": s.light_on,
                # The color control rests on White whenever the light is off, so the
                # next guest who switches it on gets white unless they pick otherwise.
                "color": (s.light_color or DEFAULT_COLOR) if s.light_on else DEFAULT_COLOR,
                "colors": s.light_colors,
            },
            "spa": {
                "current_temp": s.spa_temp,
                "set_temp": s.spa_set,
                "set_min": lim.spa_min,
                "set_max": lim.spa_max,
                "bubbles": s.bubbles,
            },
            "pool": {
                "current_temp": s.pool_temp,
                "heat_set": s.pool_heat_set,
                "chill_set": s.pool_chill_set,
                "heat_min": lim.pool_chill_min + lim.min_spread if chill_supported else lim.pool_heat_min,
                "heat_max": lim.pool_heat_max,
                "chill_min": lim.pool_chill_min,
                "chill_max": lim.pool_heat_max - lim.min_spread,
                "min_spread": lim.min_spread,
                "chill_supported": chill_supported,
                "spillover_available": s.spillover_available,
                "spillover": s.spillover,
                "water_features": s.water_features,
            },
        }

    # ---- commands ------------------------------------------------------------------

    async def _ensure(self, name: Switch, on: bool) -> None:
        """Set a switch only if it isn't already there (Jandy commands are toggles)."""
        current = getattr(self.snap, "light_on" if name == "light" else name)
        if current != on:
            await self.backend.set_switch(name, on)

    async def _run(self, action) -> dict[str, Any]:
        async with self._lock:
            # Decide from fresh state: a stale cache plus a toggle command would flip
            # something the wrong way.
            await self._refresh()
            try:
                await action()
            finally:
                try:
                    await self._refresh()
                except BackendError as exc:
                    log.warning("refresh after command failed: %s", exc)
        return self.state()

    async def set_mode(self, mode: str) -> dict[str, Any]:
        if mode not in ("pool", "spa"):
            raise RuleError(f"unknown mode {mode!r}")

        async def action() -> None:
            self.busy = mode
            try:
                if mode == "spa":
                    # Pump first: spa mode moves the valves, and the heater needs flow.
                    await self._ensure("filter_pump", True)
                    await self._ensure("spa_mode", True)
                    await self._ensure("spa_heater", True)
                else:
                    await self._ensure("spa_heater", False)
                    await self._ensure("spa_mode", False)
            finally:
                self.busy = None

        return await self._run(action)

    async def set_light(self, on: bool) -> dict[str, Any]:
        async def action() -> None:
            if on and self.snap.light_colors and DEFAULT_COLOR in self.snap.light_colors:
                # Turning on always starts at white; picking a color is a separate step.
                await self.backend.set_light_color(DEFAULT_COLOR)
            else:
                await self._ensure("light", on)

        return await self._run(action)

    async def set_light_color(self, color: str) -> dict[str, Any]:
        if color not in self.snap.light_colors:
            raise RuleError(f"unknown light color {color!r}")
        return await self._run(lambda: self.backend.set_light_color(color))

    async def set_spa_setpoint(self, temp: int) -> dict[str, Any]:
        lim = self.limits
        if not lim.spa_min <= temp <= lim.spa_max:
            raise RuleError(f"spa temperature must be between {lim.spa_min} and {lim.spa_max}")
        return await self._run(lambda: self.backend.set_spa_setpoint(temp))

    async def set_pool_setpoints(self, heat: int, chill: int | None) -> dict[str, Any]:
        lim = self.limits
        if heat > lim.pool_heat_max:
            raise RuleError(f"pool heat set point can't exceed {lim.pool_heat_max}")
        if self.snap.pool_chill_set is None:
            if chill is not None:
                raise RuleError("this controller has no chill set point")
            if heat < lim.pool_heat_min:
                raise RuleError(f"pool heat set point must be at least {lim.pool_heat_min}")
        else:
            if chill is None:
                raise RuleError("chill_set is required")
            if chill < lim.pool_chill_min:
                raise RuleError(f"pool chill set point must be at least {lim.pool_chill_min}")
            if heat - chill < lim.min_spread:
                raise RuleError(f"heat must be at least {lim.min_spread} degrees above chill")
        return await self._run(lambda: self.backend.set_pool_setpoints(heat, chill))

    async def set_bubbles(self, on: bool) -> dict[str, Any]:
        return await self._run(lambda: self._ensure("bubbles", on))

    async def set_spillover(self, on: bool) -> dict[str, Any]:
        async def action() -> None:
            if on and self.snap.water_features:
                raise RuleError("turn off Water Features before Spillover")
            await self._ensure("spillover", on)

        return await self._run(action)

    async def set_water_features(self, on: bool) -> dict[str, Any]:
        async def action() -> None:
            if on and self.snap.spillover:
                raise RuleError("turn off Spillover before Water Features")
            await self._ensure("water_features", on)

        return await self._run(action)
