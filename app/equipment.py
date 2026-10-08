"""Read-only "Equipment status" rows for the owner's advanced section.

Built only from data the backend already fetched on its last refresh (iAqualink
`get_home`, plus the aux / OneTouch / heat pump devices iaqualink-py parsed). Nothing
here sends a command.

Shape (`/api/state` -> `"equipment"`):

    {"groups": [
        {"id": "cover", "title": "Pool cover", "note": str | None,
         "rows": [{"id": "cover", "label": "Pool cover",
                   "value": "Closed (covered)", "warn": False}, ...]},
        ...
    ]}

Groups, in order (a group with no rows is left out): `cover`, `pumps` (Pumps &
heat), `salt` (Salt cell), `chemistry` (Water chemistry), `circuits` (Aux circuits
& scenes), `panel`. Row ids are stable (see each builder below). A field the panel
leaves blank is left out rather than shown empty. `warn` marks something abnormal
(salt cell status outside the documented set, heat pump alert, freeze protection
active, controller offline). Values are plain language, never raw codes, except
for unknown codes, which are shown verbatim (and flagged where that may mean a
fault). Serials, emails, tokens and session IDs never appear.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from typing import Any

# Documented iAqualink salt cell (SWC) statuses. Anything else is shown verbatim
# and flagged as a possible fault.
SWC_STATUSES = {
    "standby": "Standby",
    "running": "Running",
    "boosting": "Boosting",
    "boostpaused": "Boost paused",
}
HEATER_STATES = {"0": "Off", "1": "Heating", "3": "On, idle"}
HEATPUMP_STATES = {"off": "Off", "enabled": "On, idle", "on": "Running"}
ON_OFF = {"0": "Off", "1": "On"}
# iaqualink-py's IaquaHpmErrorCode, worded for people.
HEATPUMP_ALERTS = {
    "1": "Exchanger protection (cooling)",
    "2": "Evaporator high temperature (cooling)",
    "3": "Phase order fault",
    "4": "Low pressure (cooling)",
    "5": "High pressure (cooling)",
    "6": "Compressor discharge temperature fault",
    "7": "Water inlet sensor fault",
    "8": "Fluid line sensor fault",
    "9": "Defrost sensor fault",
    "10": "Air inlet sensor fault",
    "11": "Compressor discharge sensor fault",
    "12": "Board communication fault",
    "14": "Electronic board overheat",
    "15": "Electrical supply protection",
    "16": "Fan motor error",
    "17": "Compressor driver problem",
    "18": "Driver/compressor communication error",
    "19": "Main board not configured",
    "20": "Unrecognised configuration",
    "-1": "Unknown fault",
}
COVER_NOTE = ("As the panel reports it: 1 = covered was seen on this panel; "
              "0 = uncovered is assumed, not yet observed.")
MAX_TEXT = 60


def flatten(home_screen: Iterable[dict] | None) -> dict[str, Any]:
    """`get_home`'s `home_screen` is a list of one-key dicts; merge them."""
    out: dict[str, Any] = {}
    for item in home_screen or ():
        if isinstance(item, dict):
            out.update(item)
    return out


def _text(v: Any) -> str:
    """A value as display text; "" for missing/blank."""
    if v is None or isinstance(v, (dict, list)):
        return ""
    s = str(v).strip()
    return s[:MAX_TEXT]


def _row(rid: str, label: str, value: str, warn: bool = False) -> dict[str, Any]:
    return {"id": rid, "label": label, "value": value, "warn": bool(warn)}


def _mapped(rid: str, label: str, raw: Any, names: dict[str, str]) -> dict[str, Any] | None:
    v = _text(raw)
    if not v:
        return None
    if v in names:
        return _row(rid, label, names[v])
    # Unknown code: show it as-is so the owner can look it up; not a known fault.
    return _row(rid, label, f"Unknown ({v})")


def pool_covered(home: dict[str, Any] | None) -> bool | None:
    """True/False from `cover_pool` ("1" covered, "0" uncovered), else None."""
    v = _text((home or {}).get("cover_pool"))
    return {"1": True, "0": False}.get(v)


def panel_model(response: Any, secrets: Iterable[str] = ()) -> str | None:
    """Best-effort panel model/firmware from the `response` field.

    `get_home` carries `AQU='70','<hex bytes>'`, whose bytes end with an ASCII
    string such as "B0316823 RS-4 Combo" (then a few status bytes). The last printable run of 6+ characters is kept,
    and only if it looks like text; anything that matches a known secret (the
    serial) is dropped. Returns None when nothing usable is found.
    """
    s = _text_raw(response)
    m = re.match(r"^AQU='70','(.*)'$", s, re.DOTALL)
    if not m:
        return None
    body = m.group(1).strip()
    hexdigits = re.sub(r"[\s:,]", "", body)
    if not hexdigits or len(hexdigits) % 2 or not re.fullmatch(r"[0-9A-Fa-f]+", hexdigits):
        return None
    data = bytes.fromhex(hexdigits)
    # The model string is followed by a few status bytes on real panels
    # (e.g. "...Combo\x00\x00\x00\x5c\x00\x37"), so take the LAST printable run
    # long enough to be text rather than a run at the very end.
    runs = [r for r in re.findall(rb"[\x20-\x7e]{6,}", data) if re.search(rb"[A-Za-z]", r)]
    if not runs:
        return None
    text = " ".join(runs[-1].decode("ascii").split())
    if len(text) < 4 or not re.search(r"[A-Za-z]", text) or len(text) > MAX_TEXT:
        return None
    for secret in secrets:
        if secret and len(secret) >= 4 and secret.casefold() in text.casefold():
            return None
    return text


def _text_raw(v: Any) -> str:
    return v if isinstance(v, str) else ""


def build(
    home: dict[str, Any] | None,
    *,
    status: str | None = None,
    firmware: Any = None,
    heatpump_alert: Any = None,
    aux_on: Iterable[str] | None = None,
    scenes_on: Iterable[str] | None = None,
    secrets: Iterable[str] = (),
) -> dict[str, Any]:
    """Equipment groups from a flattened `home_screen` dict (None = no good reply yet).

    `status` is the panel's own status ("Online", "Offline", "Service"); `firmware`
    the top-level `attached_system_fw_version`; `heatpump_alert` an alert code the
    library saw; `aux_on` / `scenes_on` the labels of aux circuits / OneTouch scenes
    that are on (None = not known, so the row is left out).
    """
    h = home or {}
    secrets = [s for s in secrets if s]
    groups: list[dict[str, Any]] = []

    def group(gid: str, title: str, rows: list, note: str | None = None) -> None:
        rows = [r for r in rows if r]
        if rows:
            groups.append({"id": gid, "title": title, "note": note, "rows": rows})

    # ---- pool cover --------------------------------------------------------------
    cover = _text(h.get("cover_pool"))
    covered = pool_covered(h)
    group("cover", "Pool cover", [
        _row("cover", "Pool cover", "Closed (covered)" if covered
             else "Open (uncovered)" if covered is False else f"Unknown ({cover})")
        if cover else None,
    ], COVER_NOTE)

    # ---- pumps & heat ------------------------------------------------------------
    hp = h.get("heatpump_info") if isinstance(h.get("heatpump_info"), dict) else {}
    hp_rows: list = []
    if hp.get("isheatpumpPresent") is True:
        hp_rows.append(_mapped("heatpump", "Heat pump", hp.get("heatpumpstatus"), HEATPUMP_STATES))
        mode = _text(hp.get("heatpumpmode"))
        if mode:
            hp_rows.append(_row("heatpump_mode", "Heat pump mode",
                                mode.capitalize() if mode in ("heat", "chill") else f"Unknown ({mode})"))
    alert = _text(heatpump_alert) or _text(hp.get("alert_message"))
    if alert:
        hp_rows.append(_row("heatpump_alert", "Heat pump alert",
                            HEATPUMP_ALERTS.get(alert, f"Code {alert}"), warn=True))
    group("pumps", "Pumps & heat", [
        _mapped("filter_pump", "Filter pump", h.get("pool_pump"), ON_OFF),
        _mapped("spa_mode", "Spa mode", h.get("spa_pump"), ON_OFF),
        _mapped("spa_heater", "Spa heater", h.get("spa_heater"), HEATER_STATES),
        _mapped("pool_heater", "Pool heater", h.get("pool_heater"), HEATER_STATES),
        _mapped("solar_heater", "Solar heater", h.get("solar_heater"), HEATER_STATES),
        *hp_rows,
    ])

    # ---- salt cell ---------------------------------------------------------------
    swc = h.get("swc_info") if isinstance(h.get("swc_info"), dict) else {}
    salt_rows: list = []
    if swc.get("isswcPresent") is True:
        st = _text(swc.get("swcPoolStatus"))
        if st:
            known = SWC_STATUSES.get(st.casefold())
            salt_rows.append(_row("swc_status", "Salt cell",
                                  known or f"{st} (possible fault)", warn=known is None))
        out = swc.get("swcPoolValue")
        if isinstance(out, (int, float)) and not isinstance(out, bool) and math.isfinite(out):
            salt_rows.append(_row("swc_output", "Salt cell output", f"{out:g}%"))
        elif _text(out).replace(".", "", 1).isdigit():
            salt_rows.append(_row("swc_output", "Salt cell output", f"{_text(out)}%"))
    group("salt", "Salt cell", salt_rows)

    # ---- water chemistry ---------------------------------------------------------
    def chem(rid: str, label: str, suffix: str = "") -> dict | None:
        v = _text(h.get(rid))
        return _row(rid, label, v + suffix) if v else None

    group("chemistry", "Water chemistry", [
        chem("pool_salinity", "Pool salinity", " ppm"),
        chem("spa_salinity", "Spa salinity", " ppm"),
        chem("ph", "pH"),
        chem("orp", "ORP", " mV"),
    ])

    # ---- aux circuits & scenes ---------------------------------------------------
    def names(labels: Iterable[str] | None, rid: str, label: str) -> dict | None:
        if labels is None:
            return None
        clean = [_text(x) for x in labels if _text(x)]
        return _row(rid, label, ", ".join(clean) if clean else "None")

    group("circuits", "Aux circuits & scenes", [
        names(aux_on, "aux_on", "Aux circuits on"),
        names(scenes_on, "scenes_on", "OneTouch scenes on"),
    ])

    # ---- panel -------------------------------------------------------------------
    st = _text(status)
    unit = _text(h.get("temp_scale")).upper()
    freeze = _text(h.get("freeze_protection"))
    relays = _text(h.get("relay_count"))
    fw = _text(firmware)
    group("panel", "Panel", [
        _row("status", "Controller", "Online" if st == "Online" else st, warn=st != "Online") if st else None,
        _row("model", "Model", m) if (m := panel_model(h.get("response"), secrets)) else None,
        _row("firmware", "Firmware", fw) if fw else None,
        _row("freeze_protection", "Freeze protection",
             {"0": "Off", "1": "Active"}.get(freeze, f"Unknown ({freeze})"), warn=freeze == "1")
        if freeze else None,
        _row("temp_units", "Temperature units", {"F": "°F", "C": "°C"}.get(unit, unit)) if unit else None,
        _row("relays", "Relays", relays) if relays.isdigit() else None,
    ])

    # Belt and braces: no row may carry a secret (e.g. the serial in a panel label).
    folded = [x.casefold() for x in secrets if len(x) >= 4]
    for g in groups:
        g["rows"] = [r for r in g["rows"]
                     if not any(x in f"{r['label']} {r['value']}".casefold() for x in folded)]
    return {"groups": [g for g in groups if g["rows"]]}
