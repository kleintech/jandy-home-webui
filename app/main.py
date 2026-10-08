from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import weather
from .backends.base import Backend, BackendError
from .service import Limits, PoolService, RuleError

STATIC = Path(__file__).parent / "static"
log = logging.getLogger(__name__)


class Mode(BaseModel):
    mode: str

class OnOff(BaseModel):
    on: bool

class Color(BaseModel):
    color: str

class SpaSet(BaseModel):
    set_temp: int = Field(ge=32, le=120)

class PoolSet(BaseModel):
    heat_set: int = Field(ge=32, le=120)
    chill_set: int | None = Field(default=None, ge=32, le=120)


def make_backend() -> Backend:
    kind = os.environ.get("JANDY_BACKEND", "iaqualink")
    if kind == "mock":
        from .backends.mock import MockBackend

        return MockBackend(latency=float(os.environ.get("MOCK_LATENCY", "0")))
    from .backends.iaqualink import IAqualinkBackend

    return IAqualinkBackend.from_env()


def limits_from_env() -> Limits:
    def i(name: str, default: int) -> int:
        return int(os.environ.get(name, default))

    d = Limits()
    lim = Limits(
        spa_min=i("SPA_MIN", d.spa_min),
        spa_max=i("SPA_MAX", d.spa_max),
        pool_heat_min=i("POOL_HEAT_MIN", d.pool_heat_min),
        pool_heat_max=i("POOL_HEAT_MAX", d.pool_heat_max),
        pool_chill_max=i("POOL_CHILL_MAX", d.pool_chill_max),
        min_spread=i("POOL_MIN_SPREAD", d.min_spread),
    )
    # Fail fast: inverted limits would make every set point request fail, and a
    # negative spread would let chill drop below heat.
    problems = [
        msg for bad, msg in [
            (lim.spa_min > lim.spa_max, "SPA_MIN > SPA_MAX"),
            (lim.min_spread < 0, "POOL_MIN_SPREAD < 0"),
            (lim.pool_heat_min > lim.pool_heat_max, "POOL_HEAT_MIN > POOL_HEAT_MAX"),
            (lim.pool_heat_min + lim.min_spread > lim.pool_chill_max,
             "POOL_HEAT_MIN + POOL_MIN_SPREAD > POOL_CHILL_MAX"),
        ] if bad
    ]
    if problems:
        raise SystemExit("invalid limits: " + "; ".join(problems))
    return lim


def create_app(service: PoolService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = service or PoolService(
            make_backend(), limits_from_env(), float(os.environ.get("POLL_SECONDS", "15")),
            idle_seconds=float(os.environ.get("IDLE_SECONDS", "60")),
            cover_hint=os.environ.get("POOL_COVER_HINT", "1").strip().lower() not in ("0", "false", "no", "off"),
        )
        app.state.svc = svc
        await svc.start()
        yield
        await svc.close()

    app = FastAPI(title="Pool & Spa", lifespan=lifespan, docs_url=None, redoc_url=None)

    async def call(coro):
        try:
            return await coro
        except RuleError as exc:
            raise HTTPException(409, str(exc)) from exc
        except BackendError as exc:
            log.warning("controller error: %s", exc)
            raise HTTPException(502, f"Couldn't reach the pool: {exc}") from exc

    def svc() -> PoolService:
        return app.state.svc

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/api/state")
    async def state():
        return await svc().viewer_state()

    @app.post("/api/mode")
    async def mode(body: Mode):
        return await call(svc().set_mode(body.mode))

    @app.post("/api/light")
    async def light(body: OnOff):
        return await call(svc().set_light(body.on))

    @app.post("/api/light/color")
    async def light_color(body: Color):
        return await call(svc().set_light_color(body.color))

    @app.post("/api/spa/setpoint")
    async def spa_setpoint(body: SpaSet):
        return await call(svc().set_spa_setpoint(body.set_temp))

    @app.post("/api/spa/bubbles")
    async def bubbles(body: OnOff):
        return await call(svc().set_bubbles(body.on))

    @app.post("/api/pool/setpoints")
    async def pool_setpoints(body: PoolSet):
        return await call(svc().set_pool_setpoints(body.heat_set, body.chill_set))

    @app.post("/api/pool/spillover")
    async def spillover(body: OnOff):
        return await call(svc().set_spillover(body.on))

    @app.post("/api/pool/water_features")
    async def water_features(body: OnOff):
        return await call(svc().set_water_features(body.on))

    @app.get("/sw.js")
    async def service_worker():
        # Served from the root so its scope covers the whole app (needed to install
        # it to the home screen). It caches nothing.
        return FileResponse(STATIC / "sw.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app.include_router(weather.router)
    @app.middleware("http")
    async def revalidate_static(request, call_next):
        # Make browsers revalidate (ETag) every time, so a new index.html is never
        # paired with an old cached app.js after an update.
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/static", StaticFiles(directory=STATIC, check_dir=False), name="static")
    return app


logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
# httpx logs every request URL at INFO; iAqualink URLs carry the session ID.
logging.getLogger("httpx").setLevel(logging.WARNING)
app = create_app()
