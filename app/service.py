"""Guest-level pool rules on top of a Backend.

Everything a guest can do goes through here, so the limits (spa max 103, pool heat
min 82, chill max 92, chill >= heat + 5, spillover vs water features) are enforced on
the server and not just by the sliders.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from .backends.base import Backend, BackendError, Snapshot, StaleData, Switch

log = logging.getLogger(__name__)

DEFAULT_COLOR = "White"
# After we switch something, the cloud can keep reporting the old state for a while.
# Trust what we commanded for this long, so a second tap (or a second guest) doesn't
# send the same toggle again and undo the first.
SETTLE_SECONDS = 20.0
# One retry when the controller sends an incomplete update just before a command.
STALE_RETRY_SECONDS = 2.0
# Only poll the iAqualink cloud while someone has the page open: a viewer polls
# /api/state every few seconds, so no request for this long means nobody is looking.
IDLE_SECONDS = 60.0


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


class RuleError(Exception):
    """A request the house rules don't allow (maps to HTTP 409/422)."""


class PoolService:
    def __init__(self, backend: Backend, limits: Limits | None = None, poll_seconds: float = 15,
                 settle_seconds: float = SETTLE_SECONDS, stale_retry_seconds: float = STALE_RETRY_SECONDS,
                 idle_seconds: float = IDLE_SECONDS, cover_hint: bool = True) -> None:
        self.backend = backend
        # Show "the pool cover is closed" under Spillover / Water Features.
        self.cover_hint = cover_hint
        self.idle_seconds = idle_seconds
        self._last_viewer = float("-inf")
        self._last_refresh = float("-inf")
        # Commands waiting for (or holding) the lock; page polls never queue behind them.
        self._cmd_pending = 0
        self.settle_seconds = settle_seconds
        self.stale_retry_seconds = stale_retry_seconds
        self._commanded: dict[str, tuple[bool, float]] = {}
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
        except Exception as exc:
            # Come up anyway; the poller keeps retrying and the UI shows "can't reach".
            log.warning("initial connect failed: %s", exc)
        self._poller = asyncio.create_task(self._poll_loop())

    async def close(self) -> None:
        if self._poller:
            self._poller.cancel()
        await self.backend.close()

    def _viewed_recently(self) -> bool:
        return time.monotonic() - self._last_viewer <= self.idle_seconds

    def _due(self) -> bool:
        return time.monotonic() - self._last_refresh >= self.poll_seconds

    async def _refresh_if_due(self) -> None:
        if self._lock.locked() or self._cmd_pending or not self._due():
            return  # a command or another refresh is running and will update state
        try:
            async with self._lock:
                if self._due():
                    await self._refresh()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("refresh failed: %s", exc)

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self.poll_seconds / 3)
            if self._viewed_recently():
                await self._refresh_if_due()

    async def viewer_state(self) -> dict[str, Any]:
        """State for a page poll: marks someone as watching, and if the last
        refresh is older than a poll interval (nobody was watching), refreshes
        before answering so a freshly opened page shows current data."""
        self._last_viewer = time.monotonic()
        await self._refresh_if_due()
        return self.state()

    async def _refresh(self) -> Snapshot:
        self._last_refresh = time.monotonic()
        try:
            snap = await self.backend.refresh()
        except StaleData:
            raise  # keep showing the last good snapshot
        except BackendError:
            self.snap.connected = False
            raise
        except Exception as exc:
            self.snap.connected = False
            raise BackendError(f"unexpected reply: {exc!r}") from exc
        self.snap = self._overlay_commanded(snap)
        self.updated_at = datetime.now(UTC)
        return self.snap

    def _overlay_commanded(self, snap: Snapshot) -> Snapshot:
        now = time.monotonic()
        changes = {}
        for name, (on, at) in list(self._commanded.items()):
            attr = "light_on" if name == "light" else name
            if now - at > self.settle_seconds or getattr(snap, attr) == on:
                del self._commanded[name]  # settled, or the controller caught up
            else:
                changes[attr] = on
        return replace(snap, **changes) if changes else snap

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
            "air_temp": s.air_temp,
            "light": {
                "available": s.light_available,
                "on": s.light_on,
                # The color control rests on White whenever the light is off, so the
                # next guest who switches it on gets white unless they pick otherwise.
                "color": (s.light_color or DEFAULT_COLOR) if s.light_on else DEFAULT_COLOR,
                "colors": s.light_colors,
                "cycles": s.light_cycles,
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
                "heat_min": lim.pool_heat_min,
                "heat_max": (
                    min(lim.pool_heat_max, lim.pool_chill_max - lim.min_spread)
                    if chill_supported else lim.pool_heat_max
                ),
                "chill_min": lim.pool_heat_min + lim.min_spread,
                "chill_max": lim.pool_chill_max,
                "min_spread": lim.min_spread,
                "chill_supported": chill_supported,
                "spillover_available": s.spillover_available,
                "spillover": s.spillover,
                "water_features": s.water_features,
                # From the panel's cover_pool (1 = covered, as observed); None = unknown.
                "covered": s.pool_covered,
                "cover_hint": self.cover_hint and s.pool_covered is True,
            },
            "equipment": self._equipment(),
        }

    def _equipment(self) -> dict[str, Any]:
        """The owner's read-only equipment rows (see app/equipment.py). When the
        last refresh failed, the rows are the last good ones and the Controller row
        says so."""
        groups = [
            {**g, "rows": [dict(r) for r in g.get("rows", [])]}
            for g in (self.snap.equipment or {}).get("groups", [])
        ]
        if not self.snap.connected:
            row = {"id": "status", "label": "Controller", "value": "Not reachable", "warn": True}
            panel = next((g for g in groups if g.get("id") == "panel"), None)
            if panel is None:
                groups.append({"id": "panel", "title": "Panel", "note": None, "rows": [row]})
            else:
                rows = panel["rows"]
                current = next((r for r in rows if r.get("id") == "status"), None)
                if current is None:
                    rows.insert(0, row)
                elif current.get("value") == "Online":
                    current.update(row)
        return {"groups": groups}

    # ---- commands ------------------------------------------------------------------

    async def _ensure(self, name: Switch, on: bool) -> None:
        """Set a switch only if it isn't already there (Jandy commands are toggles)."""
        current = getattr(self.snap, "light_on" if name == "light" else name)
        if current != on:
            await self.backend.set_switch(name, on)
            self._commanded[name] = (on, time.monotonic())
            setattr(self.snap, "light_on" if name == "light" else name, on)

    async def _run(self, action) -> dict[str, Any]:
        self._cmd_pending += 1
        try:
            return await self._run_locked(action)
        finally:
            self._cmd_pending -= 1

    async def _run_locked(self, action) -> dict[str, Any]:
        async with self._lock:
            # Decide from fresh state: a stale cache plus a toggle command would flip
            # something the wrong way.
            try:
                await self._refresh()
            except StaleData:
                await asyncio.sleep(self.stale_retry_seconds)
                try:
                    await self._refresh()
                except StaleData as exc:
                    raise BackendError("the controller isn't reporting its state right now; try again") from exc
            if not self.snap.connected:
                raise BackendError("the controller is offline")
            try:
                await action()
            finally:
                try:
                    await self._refresh()
                except Exception as exc:
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
                    # The guest cap applies to whatever is already on the panel too.
                    if self.snap.spa_set is not None and self.snap.spa_set > self.limits.spa_max:
                        await self.backend.set_spa_setpoint(self.limits.spa_max)
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
                if self._recently_commanded("light", True):
                    return  # double tap; the first one is still taking effect
                # Turning on always starts at white; picking a color is a separate step.
                await self.backend.set_light_color(DEFAULT_COLOR)
                self._commanded["light"] = (True, time.monotonic())
            else:
                await self._ensure("light", on)

        return await self._run(action)

    def _recently_commanded(self, name: str, on: bool) -> bool:
        entry = self._commanded.get(name)
        return bool(entry and entry[0] == on and time.monotonic() - entry[1] <= self.settle_seconds)

    async def set_light_color(self, color: str) -> dict[str, Any]:
        async def action() -> None:
            if color not in self.snap.light_colors:
                raise RuleError(f"unknown light color {color!r}")
            await self.backend.set_light_color(color)
            self._commanded["light"] = (True, time.monotonic())

        return await self._run(action)

    async def set_spa_setpoint(self, temp: int) -> dict[str, Any]:
        lim = self.limits
        if not lim.spa_min <= temp <= lim.spa_max:
            raise RuleError(f"spa temperature must be between {lim.spa_min} and {lim.spa_max}")
        return await self._run(lambda: self.backend.set_spa_setpoint(temp))

    async def set_pool_setpoints(self, heat: int, chill: int | None) -> dict[str, Any]:
        lim = self.limits
        if not lim.pool_heat_min <= heat <= lim.pool_heat_max:
            raise RuleError(f"pool heat set point must be between {lim.pool_heat_min} and {lim.pool_heat_max}")

        async def action() -> None:
            # Checked against fresh state: whether there is a chiller decides the rules.
            if self.snap.pool_chill_set is None:
                if chill is not None:
                    raise RuleError("this controller has no chill set point")
            else:
                if chill is None:
                    raise RuleError("chill_set is required")
                if chill > lim.pool_chill_max:
                    raise RuleError(f"pool chill set point can't exceed {lim.pool_chill_max}")
                if chill - heat < lim.min_spread:
                    raise RuleError(f"chill must be at least {lim.min_spread} degrees above heat")
            await self.backend.set_pool_setpoints(heat, chill)

        return await self._run(action)

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
