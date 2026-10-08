"""Owner-only "Advanced" controls: everything the panel lets us switch or set, without
the guest limits but with the same safety machinery as the guest controls.

Every write goes through `PoolService._run` (one command at a time, refresh first,
refuse on stale/offline data, refresh after) and acts only on device keys present
in the fresh snapshot's allowlist (`Snapshot.advanced["devices"]`). The backend then
maps the key onto the library's device object and calls its own method; no command
string is ever built from a request. On/off writes are recorded for the settle
overlay, shared with the guest controls, so a lagging cloud can't make a second
request toggle the device back.

Routes (all require an owner session, see app/owner_auth.py):

    GET  /api/advanced                       -> view (below)
    POST /api/advanced/switch    {key, on}
    POST /api/advanced/heatpump  {on?, mode?}            mode: "heat" | "chill"
    POST /api/advanced/setpoints {spa?, pool_heat?, pool_chill?}
    POST /api/advanced/light     {key, on?, effect?, brightness?}
    POST /api/advanced/vsp       {key, on?, preset?}
    POST /api/advanced/salt      {pool_pct?, spa_pct?}   0-100
    POST /api/advanced/salt/boost {action, hours?, mode?}
          action: start|stop|pause|resume; start needs hours 1-24, mode pool|spillover

Every POST answers with the updated view. Errors: 409 {"detail": <reason>} for a
request the panel state doesn't allow, 422 for a malformed body, 502 for a
controller failure (generic text; details only in the log).
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections.abc import Callable, Iterable
from typing import Any, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from .backends.base import BackendError
from .service import PoolService, RuleError

log = logging.getLogger(__name__)

# Chill (the high pool set point) must stay at least this far above heat (the low one),
# so the heat pump never heats and chills against itself.
MIN_SPREAD = 1
# Fetch the salt cell's config at most this often, and only for the Advanced view.
SALT_TTL = 60.0
MAX_LABEL = 40
KEY_PATTERN = r"^[A-Za-z0-9_]{1,40}$"

# Friendly names for the panel's fixed devices (the panel gives these no label).
HOME_LABELS = {
    "pool_pump": "Filter pump",
    "spa_pump": "Spa mode",
    "spa_heater": "Spa heater",
    "pool_heater": "Pool heater",
    "solar_heater": "Solar heater",
    "heatpump": "Heat pump",
}
# Unused virtual aux slots on an RS panel are named "Aux V1", "Aux V2", ...
PLACEHOLDER_LABEL = re.compile(r"Aux V\d+", re.IGNORECASE)

# Salt cell (SWC) commands from the iAqualink protocol reference. Not implemented by
# iaqualink-py and NOT VERIFIED on this panel (see app/backends/iaqualink.py).
SWC_GET = "get_swc_config"
SWC_SET = "set_swc_config"
SWC_BOOST = "control_swc_boost"
BOOST_ACTIONS = ("start", "stop", "pause", "resume")
BOOST_MODES = ("pool", "spillover")


# ---- helpers shared with the backends ------------------------------------------------

def text(v: Any) -> str:
    if v is None or isinstance(v, (dict, list, bool)):
        return ""
    return str(v).strip()


def clean_label(raw: Any, key: str, secrets: Iterable[str] = ()) -> str:
    label = " ".join(text(raw).split())[:MAX_LABEL]
    if not label or any(s and len(s) >= 4 and s.casefold() in label.casefold() for s in secrets):
        return key
    return label


def device_entry(key: str, label: str, kind: str, on: bool, role: str | None) -> dict[str, Any]:
    # A virtual aux nobody has named or given a guest function: the UI tucks it away.
    placeholder = kind == "aux" and role is None and bool(PLACEHOLDER_LABEL.fullmatch(label))
    return {"key": key, "label": label, "kind": kind, "on": bool(on),
            "placeholder": placeholder, "role": role}


def check_salt_pct(v: Any) -> int:
    if type(v) is not int or not 0 <= v <= 100:
        raise BackendError("invalid salt cell setting")
    return v


def boost_params(action: str, hours: int | None, mode: str | None) -> dict[str, str]:
    """Wire parameters for control_swc_boost, from validated values only."""
    if action not in BOOST_ACTIONS:
        raise BackendError("invalid boost action")
    if action == "start":
        if type(hours) is not int or not 1 <= hours <= 24 or mode not in BOOST_MODES:
            raise BackendError("invalid boost settings")
        return {"boosthrs": str(hours), "boostmode": mode, "boostcontrol": "start"}
    # stop/pause/resume act on the running boost; the reference lists boosthrs and
    # boostmode for the command, but they only configure a start.
    return {"boostcontrol": action}


def _int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and math.isfinite(v):
        return int(v)
    s = text(v)
    return int(s) if re.fullmatch(r"\d{1,4}", s) else None


def _pct(v: Any) -> int | None:
    n = _int(v)
    return n if n is not None and 0 <= n <= 100 else None


def parse_swc_config(data: Any) -> dict[str, Any]:
    """get/set_swc_config and control_swc_boost replies -> our shape. Raises
    ValueError when the reply is an error or the controller is offline."""
    if not isinstance(data, dict):
        raise ValueError("not an object")
    err = data.get("is_error")
    if err is True or text(err).lower() in ("true", "1"):
        raise ValueError("is_error")
    resp = data.get("response")
    if resp is not None and text(resp).lower() != "success":
        raise ValueError("response not success")
    status = data.get("device_status")
    if status is not None and text(status).lower() != "online":
        raise ValueError("device offline")
    if _pct(data.get("poolSWCSP")) is None and _pct(data.get("spaSWCSP")) is None:
        raise ValueError("no salt cell set points in the reply")
    raw = text(data.get("boostStatus")).lower()
    boost_status = {"on": "on", "paused": "paused", "": "off"}.get(raw, "unknown")
    mode = text(data.get("boostMode")).lower()
    return {
        "pool_pct": _pct(data.get("poolSWCSP")),
        "spa_pct": _pct(data.get("spaSWCSP")),
        "boost": {
            "status": boost_status,
            "hours": _int(data.get("boostHrsVal")),
            "remaining_hours": _int(data.get("remainingBoostHrs")),
            "remaining_mins": _int(data.get("remainingBoostMins")),
            "mode": mode if mode in BOOST_MODES else None,
            # Reference: "on" = boost enabled by the cell's DIP switch; "off" or
            # absent = disabled.
            "dip_enabled": text(data.get("boostDipSwitch")).lower() == "on",
        },
    }


# ---- controller ------------------------------------------------------------------

def _rule(msg: str) -> RuleError:
    return RuleError(msg)


class AdvancedControls:
    """Owner operations on top of a PoolService (shares its lock, refresh and
    settle overlay)."""

    def __init__(self, svc: PoolService) -> None:
        self.svc = svc
        self._salt: dict[str, Any] | None = None
        self._salt_at = float("-inf")
        self._salt_error = False

    @classmethod
    def of(cls, svc: PoolService) -> AdvancedControls:
        ctl = getattr(svc, "_advanced_controls", None)
        if ctl is None:
            ctl = cls(svc)
            svc._advanced_controls = ctl
        return ctl

    # ---- view --------------------------------------------------------------------

    def _adv(self) -> dict[str, Any]:
        return self.svc.snap.advanced or {}

    def _devices(self) -> dict[str, dict[str, Any]]:
        return self._adv().get("devices") or {}

    def view(self) -> dict[str, Any]:
        s, a = self.svc.snap, self._adv()
        devices = self._devices()
        groups = []
        for gid, title, test in [
            ("pumps", "Pumps & heaters", lambda k, d: d["kind"] in ("pump", "heater", "heatpump")),
            ("aux", "Aux circuits", lambda k, d: d["kind"] in ("aux", "light") and k.startswith("aux_")),
            ("scenes", "OneTouch scenes", lambda k, d: d["kind"] == "scene"),
        ]:
            items = [dict(d) for k, d in devices.items() if test(k, d)]
            if items:
                groups.append({"id": gid, "title": title, "items": items})

        def merged(entry: dict[str, Any]) -> dict[str, Any]:
            d = devices.get(entry["key"], {})
            return {"key": entry["key"], "label": d.get("label", entry["key"]),
                    "on": bool(d.get("on")), "role": d.get("role"),
                    **{k: v for k, v in entry.items() if k != "key"}}

        hp = a.get("heatpump")
        heatpump = None
        if hp is not None and "heatpump" in devices:
            heatpump = {"on": devices["heatpump"]["on"], **hp}

        ranges = a.get("setpoint_ranges") or {}

        def sp(name: str, value: int | None) -> dict[str, Any] | None:
            r = ranges.get(name)
            return {"value": value, **r} if r else None

        salt = None
        sa = a.get("salt") or {}
        if sa.get("present"):
            salt = {
                "status": sa.get("status"),
                "output": sa.get("output"),
                "config": self._salt,
                "error": self._salt_error,
                "verified": False,  # documented API, not yet confirmed on this panel
            }
        return {
            "connected": s.connected,
            "busy": self.svc.busy is not None or self.svc._cmd_pending > 0,
            "updated_at": self.svc.updated_at.isoformat() if self.svc.updated_at else None,
            "unit": s.unit,
            "switches": groups,
            "heatpump": heatpump,
            "setpoints": {
                "min_spread": MIN_SPREAD,
                "spa": sp("spa", s.spa_set),
                "pool_heat": sp("pool_heat", s.pool_heat_set),
                "pool_chill": sp("pool_chill", s.pool_chill_set),
            },
            "lights": [merged(x) for x in a.get("lights") or [] if x.get("key") in devices],
            "vsp": [merged(x) for x in a.get("vsp") or [] if x.get("key") in devices],
            "salt": salt,
        }

    async def read(self) -> dict[str, Any]:
        """GET: marks a viewer (so the poller keeps the data fresh), refreshes if
        due, and fetches the salt cell config if it is older than SALT_TTL."""
        svc = self.svc
        svc._last_viewer = time.monotonic()
        await svc._refresh_if_due()
        await self._maybe_fetch_salt()
        return self.view()

    def _store_salt(self, cfg: dict[str, Any]) -> None:
        self._salt, self._salt_at, self._salt_error = cfg, time.monotonic(), False

    async def _maybe_fetch_salt(self) -> None:
        svc = self.svc
        if not svc.snap.connected or not (self._adv().get("salt") or {}).get("present"):
            return
        if time.monotonic() - self._salt_at < SALT_TTL:
            return
        if svc._lock.locked() or svc._cmd_pending:
            return  # a command is running; show the cached config
        async with svc._lock:
            if time.monotonic() - self._salt_at < SALT_TTL:
                return
            try:
                self._store_salt(await svc.backend.salt_config())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("salt cell config read failed: %s", exc)
                self._salt_at, self._salt_error = time.monotonic(), True

    # ---- writes ------------------------------------------------------------------

    async def _do(self, action: Callable) -> dict[str, Any]:
        await self.svc._run(action)
        return self.view()

    def _device(self, key: str, kinds: Iterable[str]) -> dict[str, Any]:
        d = self._devices().get(key)
        if d is None or d["kind"] not in kinds:
            raise _rule("unknown device")
        return d

    async def _ensure(self, key: str, on: bool, kinds: Iterable[str]) -> None:
        """Switch an allowlisted device only if it isn't already there, deciding
        from fresh state plus the settle overlay (Jandy commands are toggles)."""
        d = self._device(key, kinds)
        pending = self.svc._pending_device(key)
        if pending is not None:
            if pending == on:
                return  # already commanded; the cloud hasn't caught up yet
            raise _rule(f"{d['label']} is still changing; try again in a few seconds")
        if d["on"] == on:
            return
        await self.svc.backend.adv_set_switch(key, on)
        self.svc._note_device(key, on)

    SWITCH_KINDS = ("pump", "heater", "heatpump", "aux", "light", "scene")

    async def set_switch(self, key: str, on: bool) -> dict[str, Any]:
        return await self._do(lambda: self._ensure(key, on, self.SWITCH_KINDS))

    async def set_heatpump(self, on: bool | None, mode: str | None) -> dict[str, Any]:
        if on is None and mode is None:
            raise _rule("nothing to change")

        async def action() -> None:
            hp = self._adv().get("heatpump")
            if hp is None or "heatpump" not in self._devices():
                raise _rule("this panel has no heat pump")
            if mode is not None:
                if mode not in (hp.get("modes") or []):
                    raise _rule("this heat pump has no such mode")
                await self.svc.backend.adv_set_heatpump_mode(mode)
            if on is not None:
                # enable_disable_hpm takes an explicit on/off (not a toggle).
                await self._ensure("heatpump", on, ("heatpump",))

        return await self._do(action)

    async def set_setpoints(self, spa: int | None, pool_heat: int | None,
                            pool_chill: int | None) -> dict[str, Any]:
        if spa is None and pool_heat is None and pool_chill is None:
            raise _rule("nothing to change")

        async def action() -> None:
            s = self.svc.snap
            ranges = self._adv().get("setpoint_ranges") or {}

            def check(name: str, label: str, v: int | None) -> None:
                if v is None:
                    return
                r = ranges.get(name)
                if not r:
                    raise _rule(f"this panel has no {label} set point")
                if not r["min"] <= v <= r["max"]:
                    raise _rule(f"{label} set point must be between {r['min']} and {r['max']}")

            check("spa", "spa", spa)
            check("pool_heat", "pool heat", pool_heat)
            check("pool_chill", "pool chill", pool_chill)
            pool = pool_heat is not None or pool_chill is not None
            heat = pool_heat if pool_heat is not None else s.pool_heat_set
            has_chill = ranges.get("pool_chill") is not None or s.pool_chill_set is not None
            if pool and has_chill:
                chill = pool_chill if pool_chill is not None else s.pool_chill_set
                if heat is None or chill is None:
                    raise _rule("the current pool set points aren't known; send both")
                if chill - heat < MIN_SPREAD:
                    raise _rule(f"chill must be at least {MIN_SPREAD} degree above heat")
            if pool and heat is None:
                raise _rule("the current pool heat set point isn't known; send it")
            if spa is not None and spa != s.spa_set:
                await self.svc.backend.set_spa_setpoint(spa)
            if pool:
                # The backend orders the two writes so a failure can't invert them.
                await self.svc.backend.set_pool_setpoints(heat, pool_chill)

        return await self._do(action)

    def _light(self, key: str) -> dict[str, Any]:
        self._device(key, ("light",))
        entry = next((x for x in self._adv().get("lights") or [] if x.get("key") == key), None)
        if entry is None:
            raise _rule("unknown device")
        return entry

    async def set_light(self, key: str, on: bool | None, effect: str | None,
                        brightness: int | None) -> dict[str, Any]:
        if on is None and effect is None and brightness is None:
            raise _rule("nothing to change")
        if on is False and (effect is not None or brightness is not None):
            raise _rule("can't set an effect or brightness while turning the light off")

        async def action() -> None:
            light = self._light(key)
            if effect is not None and effect not in light["effects"]:
                raise _rule("unknown light effect")
            step = light.get("brightness_step")
            if brightness is not None and (not step or not step <= brightness <= 100 or brightness % step):
                raise _rule(f"brightness must be a multiple of {step} from {step} to 100" if step
                            else "this light isn't dimmable")
            if on is not None and effect is None:
                await self._ensure(key, on, ("light",))
            if effect is not None:
                if self.svc._pending_device(key) is False:
                    raise _rule("the light is still turning off; try again in a few seconds")
                await self.svc.backend.adv_set_light_effect(key, effect)
                self.svc._note_device(key, True)
            if brightness is not None:
                await self.svc.backend.adv_set_light_brightness(key, brightness)
                if light["type"] == "dimmable":
                    self.svc._note_device(key, True)  # set_light at a level turns it on

        return await self._do(action)

    async def set_vsp(self, key: str, on: bool | None, preset: str | None) -> dict[str, Any]:
        if on is None and preset is None:
            raise _rule("nothing to change")
        if on is False and preset is not None:
            raise _rule("can't pick a speed while stopping the pump")

        async def action() -> None:
            self._device(key, ("vsp",))
            entry = next((x for x in self._adv().get("vsp") or [] if x.get("key") == key), None)
            if entry is None:
                raise _rule("unknown device")
            if preset is not None:
                if preset not in entry["presets"]:
                    raise _rule("unknown pump speed")
                if preset != entry.get("preset"):
                    await self.svc.backend.adv_set_vsp_preset(key, preset)
                    self.svc._note_device(key, True)
            else:
                await self._ensure(key, on, ("vsp",))

        return await self._do(action)

    def _require_salt(self) -> None:
        if not (self._adv().get("salt") or {}).get("present"):
            raise _rule("this panel has no salt cell")

    async def set_salt(self, pool_pct: int | None, spa_pct: int | None) -> dict[str, Any]:
        if pool_pct is None and spa_pct is None:
            raise _rule("nothing to change")

        async def action() -> None:
            self._require_salt()
            # set_swc_config always carries both values: start from the cell's own.
            cfg = await self.svc.backend.salt_config()
            self._store_salt(cfg)
            pool = pool_pct if pool_pct is not None else cfg["pool_pct"]
            spa = spa_pct if spa_pct is not None else cfg["spa_pct"]
            if pool is None or spa is None:
                raise _rule("the salt cell didn't report its current settings; send both")
            if (pool, spa) == (cfg["pool_pct"], cfg["spa_pct"]):
                return
            self._store_salt(await self.svc.backend.set_salt(pool, spa))

        return await self._do(action)

    async def salt_boost(self, action_name: str, hours: int | None, mode: str | None) -> dict[str, Any]:
        if action_name != "start" and (hours is not None or mode is not None):
            raise _rule("hours and mode only apply to start")
        if action_name == "start" and hours is None:
            raise _rule("hours is required to start a boost")

        async def action() -> None:
            self._require_salt()
            cfg = await self.svc.backend.salt_config()
            self._store_salt(cfg)
            status = cfg["boost"]["status"]
            allowed = {"start": ("off",), "stop": ("on", "paused"), "pause": ("on",),
                       "resume": ("paused",)}[action_name]
            if status not in allowed:
                raise _rule(f"can't {action_name} a boost that is {status}")
            use_mode = mode
            if action_name == "start":
                if not cfg["boost"]["dip_enabled"]:
                    raise _rule("boost is disabled on the salt cell (DIP switch)")
                use_mode = mode or cfg["boost"]["mode"]
                if use_mode is None:
                    raise _rule("mode is required to start a boost")
            self._store_salt(await self.svc.backend.salt_boost(action_name, hours, use_mode))

        return await self._do(action)


# ---- HTTP ------------------------------------------------------------------------

class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SwitchReq(_Body):
    key: str = Field(pattern=KEY_PATTERN)
    on: StrictBool


class HeatPumpReq(_Body):
    on: StrictBool | None = None
    mode: Literal["heat", "chill"] | None = None


class SetpointsReq(_Body):
    spa: StrictInt | None = Field(default=None, ge=0, le=120)
    pool_heat: StrictInt | None = Field(default=None, ge=0, le=120)
    pool_chill: StrictInt | None = Field(default=None, ge=0, le=120)


class LightReq(_Body):
    key: str = Field(pattern=KEY_PATTERN)
    on: StrictBool | None = None
    effect: str | None = Field(default=None, min_length=1, max_length=40)
    brightness: StrictInt | None = Field(default=None, ge=0, le=100)


class VspReq(_Body):
    key: str = Field(pattern=KEY_PATTERN)
    on: StrictBool | None = None
    preset: str | None = Field(default=None, min_length=1, max_length=40)


class SaltReq(_Body):
    pool_pct: StrictInt | None = Field(default=None, ge=0, le=100)
    spa_pct: StrictInt | None = Field(default=None, ge=0, le=100)


class BoostReq(_Body):
    action: Literal["start", "stop", "pause", "resume"]
    hours: StrictInt | None = Field(default=None, ge=1, le=24)
    mode: Literal["pool", "spillover"] | None = None


def router(get_svc: Callable[[], PoolService], require_owner: Callable,
           call: Callable) -> APIRouter:
    def no_store(response: Response) -> None:
        response.headers["Cache-Control"] = "no-store"

    r = APIRouter(prefix="/api/advanced", dependencies=[Depends(require_owner), Depends(no_store)])

    def ctl() -> AdvancedControls:
        return AdvancedControls.of(get_svc())

    @r.get("")
    async def view():
        return await ctl().read()

    @r.post("/switch")
    async def switch(body: SwitchReq):
        return await call(ctl().set_switch(body.key, body.on))

    @r.post("/heatpump")
    async def heatpump(body: HeatPumpReq):
        return await call(ctl().set_heatpump(body.on, body.mode))

    @r.post("/setpoints")
    async def setpoints(body: SetpointsReq):
        return await call(ctl().set_setpoints(body.spa, body.pool_heat, body.pool_chill))

    @r.post("/light")
    async def light(body: LightReq):
        return await call(ctl().set_light(body.key, body.on, body.effect, body.brightness))

    @r.post("/vsp")
    async def vsp(body: VspReq):
        return await call(ctl().set_vsp(body.key, body.on, body.preset))

    @r.post("/salt")
    async def salt(body: SaltReq):
        return await call(ctl().set_salt(body.pool_pct, body.spa_pct))

    @r.post("/salt/boost")
    async def boost(body: BoostReq):
        return await call(ctl().salt_boost(body.action, body.hours, body.mode))

    return r
