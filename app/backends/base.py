"""The contract between the guest-facing service and whatever talks to the Jandy.

The service only ever speaks in pool terms ("spa mode", "bubbles"); each backend maps
those onto its own device names. That keeps the business rules testable against the
mock and the Jandy-specific naming in one file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

# On/off things a guest can affect, directly or through a mode change.
Switch = Literal[
    "filter_pump",
    "spa_mode",
    "spa_heater",
    "pool_heater",
    "bubbles",
    "water_features",
    "spillover",
    "light",
]


@dataclass
class Snapshot:
    connected: bool = False
    unit: str = "F"
    filter_pump: bool = False
    spa_mode: bool = False
    spa_heater: bool = False
    pool_heater: bool = False
    spa_temp: int | None = None
    pool_temp: int | None = None
    spa_set: int | None = None
    pool_heat_set: int | None = None
    # None when the controller has no chiller / chill setpoint.
    pool_chill_set: int | None = None
    bubbles: bool = False
    water_features: bool = False
    spillover: bool = False
    spillover_available: bool = False
    light_available: bool = False
    light_on: bool = False
    light_color: str | None = None
    light_colors: list[str] = field(default_factory=list)


class BackendError(Exception):
    """A command the controller rejected or could not be reached for."""


class Backend(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def refresh(self) -> Snapshot: ...

    async def set_switch(self, name: Switch, on: bool) -> None: ...

    async def set_light_color(self, color: str) -> None: ...

    async def set_spa_setpoint(self, temp: int) -> None: ...

    async def set_pool_setpoints(self, heat: int, chill: int | None) -> None: ...
