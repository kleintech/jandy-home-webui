from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

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
    return Limits(
        spa_min=i("SPA_MIN", d.spa_min),
        spa_max=i("SPA_MAX", d.spa_max),
        pool_heat_min=i("POOL_HEAT_MIN", d.pool_heat_min),
        pool_heat_max=i("POOL_HEAT_MAX", d.pool_heat_max),
        pool_chill_min=i("POOL_CHILL_MIN", d.pool_chill_min),
        min_spread=i("POOL_MIN_SPREAD", d.min_spread),
    )


def create_app(service: PoolService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = service or PoolService(
            make_backend(), limits_from_env(), float(os.environ.get("POLL_SECONDS", "15"))
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
            raise HTTPException(502, f"The pool controller didn't respond: {exc}") from exc

    def svc() -> PoolService:
        return app.state.svc

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/api/state")
    async def state():
        return svc().state()

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

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC, check_dir=False), name="static")
    return app


logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
app = create_app()
