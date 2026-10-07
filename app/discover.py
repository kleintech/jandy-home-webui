"""List what your iAqualink panel reports, to fill in the JANDY_*_DEVICE settings.

    IAQUALINK_USERNAME=... IAQUALINK_PASSWORD=... python -m app.discover
    docker compose run --rm pool python -m app.discover

Read-only: it logs in and reads the panel's status screens. It never sends a command.
"""

from __future__ import annotations

import asyncio
import os
import sys

from iaqualink.client import AqualinkClient
from iaqualink.device import AqualinkLight, AqualinkSwitch


async def main() -> int:
    try:
        user, pw = os.environ["IAQUALINK_USERNAME"], os.environ["IAQUALINK_PASSWORD"]
    except KeyError as exc:
        print(f"set {exc.args[0]}", file=sys.stderr)
        return 2
    async with AqualinkClient(user, pw) as client:
        systems = await client.get_systems()
        for serial, system in systems.items():
            # Only the last 4 characters: this output tends to get pasted into issues.
            print(f"System {system.name!r}  type={system.type}  serial=…{serial[-4:]}")
            if system.type != "iaqua":
                print("  (not an iaqua system; this app only drives iaqua panels)")
                continue
            await system.refresh()
            print(f"  status={system.status.name}  units={getattr(system, 'temp_unit', '?')}")
            print(f"  {'key':<22} {'label':<20} {'kind':<26} state")
            for key, dev in system.devices.items():
                kind = type(dev).__name__
                mark = " <- switch" if isinstance(dev, AqualinkSwitch) else (
                    " <- light" if isinstance(dev, AqualinkLight) else "")
                label = dev.data.get("label", "")
                print(f"  {key:<22} {label!s:<20} {kind:<26} {dev.data.get('state')!r}{mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
