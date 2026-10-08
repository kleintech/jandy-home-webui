"""Guest-level pool rules on top of a Backend.

Everything a guest can do goes through here, so the limits (spa max 103, pool heat
min 82, chill max 92, chill >= heat + 5, spillover vs water features) are enforced on
the server and not just by the sliders.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from .backends.base import Backend, BackendError, Snapshot, StaleData, Switch
from .config_store import AppConfig, ConfigInvalid, ConfigStore, LightStep, Limits, SpaMaxStep

__all__ = ["Limits", "PoolService", "RuleError", "SequenceError", "UnknownToggle"]

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


class RuleError(Exception):
    """A request the house rules don't allow (maps to HTTP 409/422)."""


class UnknownToggle(RuleError):
    """No guest toggle with that id (HTTP 404)."""


class SequenceError(BackendError):
    """A Hot Tub On/Off step failed at the controller; the message (generic, safe
    for guests) says which step, and the steps after it were not run."""


# Which device kinds each kind of configured step / guest toggle may drive.
FITS = {
    "switch": frozenset({"pump", "heater", "heatpump", "aux", "vsp"}),
    "scene": frozenset({"scene"}),
    "light": frozenset({"light"}),
    "toggle": frozenset({"aux", "light", "scene"}),
}
# When the panel's device list isn't known (controller unreachable), keys are checked
# against the shapes the backends produce instead; a run re-checks the real panel.
KNOWN_KEYS = {
    "switch": re.compile(r"(pool_pump|spa_pump|spa_heater|pool_heater|solar_heater|heatpump|aux_[A-Za-z0-9]{1,3})"),
    "scene": re.compile(r"onetouch_\d{1,2}"),
    "light": re.compile(r"aux_[A-Za-z0-9]{1,3}|icl_zone_\d{1,2}"),
    "toggle": re.compile(r"aux_[A-Za-z0-9]{1,3}|icl_zone_\d{1,2}|onetouch_\d{1,2}"),
}
SEQUENCE_NAMES = {"hot_tub_on": "Hot Tub On", "hot_tub_off": "Hot Tub Off"}


class PoolService:
    def __init__(self, backend: Backend, limits: Limits | None = None, poll_seconds: float = 15,
                 settle_seconds: float = SETTLE_SECONDS, stale_retry_seconds: float = STALE_RETRY_SECONDS,
                 idle_seconds: float = IDLE_SECONDS, cover_hint: bool = True,
                 config: ConfigStore | None = None) -> None:
        self.backend = backend
        # Live settings (limits, Hot Tub sequences, guest toggles, weather). Without a
        # store: defaults from `limits`, kept in memory.
        self.config = config or ConfigStore(None, limits or Limits())
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
        # The same, by device key (owner Advanced controls). Guest and owner commands
        # are recorded in both, so neither can toggle what the other just switched.
        self._dev_commanded: dict[str, tuple[bool, float]] = {}
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
        snap = replace(snap, **changes) if changes else snap
        return self._overlay_devices(snap)

    def _overlay_devices(self, snap: Snapshot) -> Snapshot:
        devices = (snap.advanced or {}).get("devices")
        if not self._dev_commanded or not devices:
            return snap
        now = time.monotonic()
        out = dict(devices)
        for key, (on, at) in list(self._dev_commanded.items()):
            cur = devices.get(key)
            if now - at > self.settle_seconds or (cur is not None and cur.get("on") == on):
                del self._dev_commanded[key]
            elif cur is not None:
                out[key] = {**cur, "on": on}
        snap.advanced = {**snap.advanced, "devices": out}
        return snap

    def _switch_keys(self) -> dict[str, str]:
        try:
            return dict(self.backend.switch_keys())
        except Exception as exc:
            log.warning("switch key lookup failed: %r", exc)
            return {}

    def _pending_device(self, key: str) -> bool | None:
        """What we last commanded this device to, while the cloud hasn't caught up."""
        entry = self._dev_commanded.get(key)
        if entry and time.monotonic() - entry[1] <= self.settle_seconds:
            return entry[0]
        return None

    def _set_device_on(self, key: str, on: bool, now: float) -> None:
        self._dev_commanded[key] = (on, now)
        devices = (self.snap.advanced or {}).get("devices")
        if devices and key in devices:
            self.snap.advanced = {**self.snap.advanced,
                                  "devices": {**devices, key: {**devices[key], "on": on}}}

    def _note_device(self, key: str, on: bool) -> None:
        """Record an owner command on a device, and on any guest switch it drives."""
        now = time.monotonic()
        self._set_device_on(key, on, now)
        for name, k in self._switch_keys().items():
            if k == key:
                self._commanded[name] = (on, now)
                setattr(self.snap, "light_on" if name == "light" else name, on)

    def _note_guest(self, name: str, on: bool) -> None:
        """Record a guest command, also against the device key it drives."""
        now = time.monotonic()
        self._commanded[name] = (on, now)
        key = self._switch_keys().get(name)
        if key:
            self._set_device_on(key, on, now)

    # ---- configuration -------------------------------------------------------------

    def app_config(self) -> AppConfig:
        """The live settings document (saved, or today's defaults)."""
        return self.config.current(self._switch_keys())

    @property
    def limits(self) -> Limits:
        return self.app_config().limits.to_limits()

    def _devices(self) -> dict[str, dict[str, Any]]:
        return (self.snap.advanced or {}).get("devices") or {}

    def _role_of(self, key: str, keys: dict[str, str] | None = None) -> str | None:
        """The guest switch (filter_pump, bubbles, ...) a device key drives, if any."""
        for name, k in (keys if keys is not None else self._switch_keys()).items():
            if k == key:
                return name
        return None

    def _key_known(self, key: str) -> bool:
        return key in self._devices() or self._role_of(key) is not None

    def _key_on(self, key: str) -> bool:
        role = self._role_of(key)
        if role is not None:
            return bool(getattr(self.snap, "light_on" if role == "light" else role))
        return bool(self._devices().get(key, {}).get("on"))

    def check_config_devices(self, cfg: AppConfig) -> None:
        """Every key in the document must be a device the panel reports and of a kind
        that fits its use. While the device list isn't known (controller
        unreachable), keys are checked against the shapes backends produce; a run
        re-checks against the panel. Raises ConfigInvalid naming the field."""
        devices = self._devices() if self.snap.connected else {}
        keys = self._switch_keys()
        # The devices the env device map gives each use (any kind: that is how the
        # defaults drive them). Toggles only get the old guest toggles' devices, so a
        # remapped heater can't be offered to guests this way.
        role_keys = {
            "switch": {k for n, k in keys.items() if n != "light"},
            "toggle": {k for n, k in keys.items() if n in ("bubbles", "spillover", "water_features")},
        }
        lights = {x.get("key"): x for x in (self.snap.advanced or {}).get("lights") or []}

        def check(where: str, key: str, use: str) -> None:
            if key in role_keys.get(use, ()):
                return
            if devices:
                d = devices.get(key)
                if d is None:
                    raise ConfigInvalid(f"{where}.key: the controller has no device {key!r}")
                if d["kind"] not in FITS[use]:
                    raise ConfigInvalid(f"{where}.key: {d['label']} ({key}) can't be used as a {use}")
            elif not KNOWN_KEYS[use].fullmatch(key):
                raise ConfigInvalid(f"{where}.key: {key!r} isn't a known {use} device")

        for seq in ("hot_tub_on", "hot_tub_off"):
            for i, st in enumerate(getattr(cfg, seq)):
                if isinstance(st, SpaMaxStep):
                    continue
                where = f"{seq}.{i}"
                check(where, st.key, st.action)
                if isinstance(st, LightStep) and st.effect is not None and devices:
                    entry = lights.get(st.key) or {}
                    guest_color = keys.get("light") == st.key and st.effect in self.snap.light_colors
                    if st.effect not in (entry.get("effects") or []) and not guest_color:
                        raise ConfigInvalid(f"{where}.effect: that light has no effect {st.effect!r}")
        for i, t in enumerate(cfg.guest_toggles):
            check(f"guest_toggles.{i}", t.key, "toggle")

    def toggles_state(self, cfg: AppConfig) -> list[dict[str, Any]]:
        out = []
        for t in cfg.guest_toggles:
            blocker = next((o for o in (cfg.toggle(c) for c in t.conflicts)
                            if o is not None and self._key_on(o.key)), None)
            out.append({
                "id": t.id,
                "label": t.label,
                "on": self._key_on(t.key),
                "modes": list(t.modes),
                "available": self._key_known(t.key),
                "blocked_by": blocker.label if blocker else None,
            })
        return out

    # ---- state ---------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        cfg = self.app_config()
        s, lim = self.snap, cfg.limits.to_limits()
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
            # Guest toggles from the settings (the fixed spa.bubbles / pool.spillover /
            # pool.water_features fields above stay for one release).
            "toggles": self.toggles_state(cfg),
            # The page refetches /api/config when this changes.
            "config_version": cfg.version,
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
            self._note_guest(name, on)
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

    async def _ensure_key(self, key: str, on: bool) -> None:
        """Switch a device by key only if it isn't already there. A guest switch's
        device goes through _ensure (guest settle overlay); any other through the
        owner path (device settle overlay), refusing a reversal while it settles."""
        role = self._role_of(key)
        if role is not None:
            await self._ensure(role, on)  # type: ignore[arg-type]
            return
        d = self._devices().get(key)
        if d is None:
            raise RuleError("that device isn't on the controller")
        pending = self._pending_device(key)
        if pending is not None:
            if pending == on:
                return  # already commanded; the cloud hasn't caught up yet
            raise RuleError(f"{d['label']} is still changing; try again in a few seconds")
        if d.get("on") == on:
            return
        await self.backend.adv_set_switch(key, on)
        self._note_device(key, on)

    async def _light_step(self, st: LightStep) -> None:
        if not st.on or st.effect is None:
            await self._ensure_key(st.key, st.on)
            return
        if self._role_of(st.key) == "light" and st.effect in self.snap.light_colors:
            # The guest light: same path as the color chips (White maps onto the
            # panel's own white).
            if self.snap.light_on and self.snap.light_color == st.effect:
                return
            await self.backend.set_light_color(st.effect)
            self._note_guest("light", True)
            return
        entry = next((x for x in (self.snap.advanced or {}).get("lights") or []
                      if x.get("key") == st.key), None)
        if entry is None or st.key not in self._devices():
            raise RuleError("that light isn't on the controller")
        if st.effect not in (entry.get("effects") or []):
            raise RuleError("that light has no such effect")
        if self._pending_device(st.key) is False:
            raise RuleError("the light is still turning off; try again in a few seconds")
        if self._devices()[st.key].get("on") and entry.get("effect") == st.effect:
            return
        await self.backend.adv_set_light_effect(st.key, st.effect)
        self._note_device(st.key, True)

    async def _run_step(self, st, lim: Limits) -> None:
        if isinstance(st, SpaMaxStep):
            # Never above the guest cap, even if the step says more.
            cap = min(st.value, lim.spa_max)
            if self.snap.spa_set is not None and self.snap.spa_set > cap:
                await self.backend.set_spa_setpoint(cap)
        elif isinstance(st, LightStep):
            await self._light_step(st)
        else:  # switch / scene: the device's own on/off state decides
            await self._ensure_key(st.key, st.on)

    def describe_step(self, st) -> str:
        if isinstance(st, SpaMaxStep):
            return f"spa set point at most {st.value}"
        label = self._devices().get(st.key, {}).get("label") or st.key
        if isinstance(st, LightStep) and st.on and st.effect:
            return f"{label} {st.effect}"
        return f"{label} {'on' if st.on else 'off'}"

    async def set_mode(self, mode: str) -> dict[str, Any]:
        """Hot Tub On (spa) / Off (pool): the configured steps, in order, from fresh
        state. Every key is checked against the panel before anything is sent; a
        failing step stops the sequence and the error says which one."""
        if mode not in ("pool", "spa"):
            raise RuleError(f"unknown mode {mode!r}")
        seq = "hot_tub_on" if mode == "spa" else "hot_tub_off"
        name = SEQUENCE_NAMES[seq]

        async def action() -> None:
            cfg = self.app_config()
            steps, lim = list(getattr(cfg, seq)), cfg.limits.to_limits()
            for i, st in enumerate(steps, 1):
                if not isinstance(st, SpaMaxStep) and not self._key_known(st.key):
                    log.warning("%s step %d: device %r not on the controller", name, i, st.key)
                    raise RuleError(f"{name} uses a device the controller doesn't report "
                                    f"(step {i}); check Settings")
            self.busy = mode
            try:
                for i, st in enumerate(steps, 1):
                    what = self.describe_step(st)
                    try:
                        await self._run_step(st, lim)
                    except RuleError as exc:
                        log.warning("%s stopped at step %d (%s): %s", name, i, what, exc)
                        raise RuleError(f"{name} stopped at step {i} of {len(steps)} ({what}): {exc}") from exc
                    except Exception as exc:
                        # Details (library text, URLs) go to the log, not to guests.
                        log.warning("%s failed at step %d (%s): %r", name, i, what, exc)
                        raise SequenceError(f"{name} stopped at step {i} of {len(steps)} ({what}); "
                                            "the steps after it weren't run") from exc
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
                self._note_guest("light", True)
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
            self._note_guest("light", True)

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

    # ---- guest toggles -------------------------------------------------------------

    async def set_toggle(self, tid: str, on: bool) -> dict[str, Any]:
        """A configured guest toggle. Conflicts are checked against fresh state (a
        conflicting toggle that is on refuses the request)."""
        if self.app_config().toggle(tid) is None:
            raise UnknownToggle("no such control")

        async def action() -> None:
            cfg = self.app_config()
            t = cfg.toggle(tid)
            if t is None:
                raise RuleError("that control was just removed; reload the page")
            if not self._key_known(t.key):
                raise RuleError(f"{t.label} isn't available on the controller")
            if on:
                for cid in t.conflicts:
                    other = cfg.toggle(cid)
                    if other is not None and self._key_on(other.key):
                        raise RuleError(f"turn off {other.label} before {t.label}")
            await self._ensure_key(t.key, on)

        return await self._run(action)

    async def _legacy_toggle(self, tid: str, on: bool, fallback) -> dict[str, Any]:
        """Old fixed endpoints: the toggle with that id when there is one; on
        defaults whose device didn't resolve, the old behaviour; refused once the
        owner's saved settings have no such toggle."""
        if self.app_config().toggle(tid) is not None:
            return await self.set_toggle(tid, on)
        if self.config.saved:
            raise UnknownToggle("no such control")
        return await fallback()

    async def set_bubbles(self, on: bool) -> dict[str, Any]:
        return await self._legacy_toggle(
            "bubbles", on, lambda: self._run(lambda: self._ensure("bubbles", on)))

    async def set_spillover(self, on: bool) -> dict[str, Any]:
        async def action() -> None:
            if on and self.snap.water_features:
                raise RuleError("turn off Water Features before Spillover")
            await self._ensure("spillover", on)

        return await self._legacy_toggle("spillover", on, lambda: self._run(action))

    async def set_water_features(self, on: bool) -> dict[str, Any]:
        async def action() -> None:
            if on and self.snap.spillover:
                raise RuleError("turn off Spillover before Water Features")
            await self._ensure("water_features", on)

        return await self._legacy_toggle("water_features", on, lambda: self._run(action))
