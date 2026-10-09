"""Owner gate for the Advanced controls. The guest UI has no login (it lives on the
LAN); the owner unlocks the Advanced section with a PIN.

- `OWNER_PIN` (6-12 ASCII digits) turns the gate on. Unset or malformed: every
  Advanced endpoint answers 403 and `/api/state` reports `advanced.enabled = false`.
- `POST /api/advanced/unlock {"pin"}` compares in constant time and, on success, sets
  an HttpOnly, SameSite=Strict cookie (Secure when the request came over HTTPS,
  directly or per X-Forwarded-Proto) holding an HMAC-signed expiry, valid 30 days.
  The signing key is derived from `OWNER_SECRET` and the PIN, so changing the PIN
  logs every owner out. Without `OWNER_SECRET` (or with one under 32 characters,
  which is ignored with a warning) a random secret is made at start-up, so a
  restart logs owners out too.
- `POST /api/advanced/lock` (send `{}` as JSON) clears the cookie on that browser. (The token is
  stateless: a copied cookie stays valid until it expires or the PIN/secret changes.)
- Failed unlocks are limited per client (5 per 5 minutes) and globally (10 per 15
  minutes, so many addresses can't share the work), answering 429 while limited
  even for the right PIN. Failures are logged without the PIN. The global limit is
  a deliberate trade-off: behind k3s ServiceLB (externalTrafficPolicy Cluster)
  every client arrives from the same address, so it is the limit that actually
  holds, and a guest typing wrong PINs can delay an owner's unlock for up to 15
  minutes. Browsers that are already unlocked are unaffected.
- Every owner POST/PUT (unlock and lock included, see `same_origin`) is refused
  (403) when the browser says it is cross-site (Sec-Fetch-Site) or the Origin
  isn't this site, and must be sent as application/json (415 otherwise), so a
  page elsewhere can't drive them with a form post.

The client address is the TCP peer. `OWNER_TRUSTED_PROXIES` (comma-separated IPs or
CIDRs, e.g. the Traefik pod network) makes the gate take the client from
X-Forwarded-For when the peer is one of those proxies (the right-most address that
isn't a trusted proxy). Without it, everyone behind the proxy shares one per-client
bucket, and the global limit still applies.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import os
import re
import secrets
import time
from collections import deque
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger(__name__)

COOKIE = "pool_owner"
TOKEN_TTL = 30 * 24 * 3600
DISABLED_DETAIL = "Advanced controls are off: set OWNER_PIN"
LOCKED_DETAIL = "Owner PIN required"
# ASCII only: \d would also take Arabic-Indic or full-width digits.
PIN_RE = re.compile(r"[0-9]{6,12}")
MIN_SECRET = 32
CROSS_SITE_DETAIL = "Cross-site request refused"
JSON_DETAIL = "Send this request as application/json"
MAX_TRACKED_CLIENTS = 1024


class _Window:
    """Failure timestamps within a sliding window."""

    def __init__(self, limit: int, seconds: float) -> None:
        self.limit, self.seconds = limit, seconds
        self.hits: deque[float] = deque()

    def _prune(self, now: float) -> None:
        while self.hits and now - self.hits[0] >= self.seconds:
            self.hits.popleft()

    def retry_after(self, now: float) -> float:
        self._prune(now)
        if len(self.hits) < self.limit:
            return 0.0
        return max(1.0, self.seconds - (now - self.hits[0]))

    def add(self, now: float) -> int:
        self._prune(now)
        self.hits.append(now)
        return len(self.hits)


class OwnerGate:
    def __init__(self, pin: str | None, secret: bytes | None = None, *,
                 trusted_proxies: str = "", clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic,
                 per_client: tuple[int, float] = (5, 300.0),
                 global_limit: tuple[int, float] = (10, 900.0)) -> None:
        pin = (pin or "").strip()
        if pin and not PIN_RE.fullmatch(pin):
            log.error("OWNER_PIN must be 6-12 digits (0-9); owner controls (Advanced, "
                      "Settings changes) are OFF until it is fixed")
            pin = ""
        self.enabled = bool(pin)
        self._pin_digest = hashlib.sha256(pin.encode()).digest()
        secret = secret or secrets.token_bytes(32)
        # Bound to the PIN: changing it invalidates every cookie.
        self._key = hmac.new(secret, b"owner-cookie\0" + pin.encode(), hashlib.sha256).digest()
        self._clock, self._mono = clock, monotonic
        self._per_client_cfg = per_client
        self._clients: dict[str, _Window] = {}
        self._global = _Window(*global_limit)
        self._trusted = []
        for part in (trusted_proxies or "").split(","):
            part = part.strip()
            if part:
                try:
                    self._trusted.append(ipaddress.ip_network(part, strict=False))
                except ValueError:
                    log.error("ignoring bad OWNER_TRUSTED_PROXIES entry %r", part)

    @classmethod
    def from_env(cls) -> OwnerGate:
        secret = os.environ.get("OWNER_SECRET") or None
        if secret is not None and len(secret) < MIN_SECRET:
            log.warning("OWNER_SECRET is shorter than %d characters; ignoring it (owner sessions "
                        "end when the app restarts). Use e.g. `openssl rand -hex 32`", MIN_SECRET)
            secret = None
        elif os.environ.get("OWNER_PIN") and not secret:
            log.info("OWNER_SECRET not set: owner sessions end when the app restarts")
        return cls(os.environ.get("OWNER_PIN"), secret.encode() if secret else None,
                   trusted_proxies=os.environ.get("OWNER_TRUSTED_PROXIES", ""))

    # ---- tokens ----------------------------------------------------------------------

    def _sign(self, body: str) -> str:
        return hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        body = f"v1.{int(self._clock()) + TOKEN_TTL}.{secrets.token_hex(8)}"
        return f"{body}.{self._sign(body)}"

    def verify(self, token: str | None) -> bool:
        # isascii: compare_digest raises on non-ASCII text (a crafted cookie -> 500).
        if not self.enabled or not token or len(token) > 200 or not token.isascii():
            return False
        parts = token.split(".")
        if len(parts) != 4 or parts[0] != "v1" or not parts[1].isdigit():
            return False
        body = ".".join(parts[:3])
        if not hmac.compare_digest(parts[3], self._sign(body)):
            return False
        return int(parts[1]) > self._clock()

    def check_pin(self, pin: str) -> bool:
        # Compare digests: constant time and no length leak.
        given = hashlib.sha256(pin.encode()).digest()
        return self.enabled and hmac.compare_digest(given, self._pin_digest)

    # ---- rate limiting ---------------------------------------------------------------

    def client_id(self, request: Request) -> str:
        peer = request.client.host if request.client else "unknown"
        if not self._trusted or not self._is_trusted(peer):
            return peer
        hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
        for hop in reversed(hops):
            if not self._is_trusted(hop):
                try:
                    return str(ipaddress.ip_address(hop))
                except ValueError:
                    return peer  # garbage in the header: don't trust any of it
        return peer

    def _is_trusted(self, addr: str) -> bool:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        return any(ip in net for net in self._trusted)

    def retry_after(self, client: str) -> float:
        now = self._mono()
        w = self._clients.get(client)
        return max(self._global.retry_after(now), w.retry_after(now) if w else 0.0)

    def record_failure(self, client: str) -> tuple[int, int]:
        now = self._mono()
        if client not in self._clients and len(self._clients) >= MAX_TRACKED_CLIENTS:
            # Drop clients with no recent failures; the global window still counts.
            for k in [k for k, w in self._clients.items() if w.retry_after(now) == 0 and not w.hits]:
                del self._clients[k]
            if len(self._clients) >= MAX_TRACKED_CLIENTS:
                self._clients.pop(next(iter(self._clients)))
        w = self._clients.setdefault(client, _Window(*self._per_client_cfg))
        return w.add(now), self._global.add(now)

    def record_success(self, client: str) -> None:
        self._clients.pop(client, None)

    # ---- request helpers -------------------------------------------------------------

    def unlocked(self, request: Request) -> bool:
        return self.verify(request.cookies.get(COOKIE))

    def status(self, request: Request) -> dict[str, bool]:
        return {"enabled": self.enabled, "unlocked": self.enabled and self.unlocked(request)}

    def require_owner(self, request: Request) -> None:
        if not self.enabled:
            raise HTTPException(403, DISABLED_DETAIL)
        if not self.unlocked(request):
            raise HTTPException(401, LOCKED_DETAIL)


def _https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or proto == "https"


def _origin_key(scheme: str, netloc: str) -> tuple[str, str]:
    scheme, netloc = scheme.lower(), netloc.strip().lower()
    default = {"http": ":80", "https": ":443"}.get(scheme)
    if default and netloc.endswith(default):
        netloc = netloc[: -len(default)]
    return scheme, netloc


def same_origin(request: Request) -> None:
    """CSRF guard for owner writes (a dependency). The owner cookie is
    SameSite=Strict; this is the second wall. Safe methods pass. Otherwise refuse
    (403) when Sec-Fetch-Site is present and isn't same-origin/none, or when Origin
    is present and isn't this site (scheme per _https, host per the Host header or
    X-Forwarded-Host). Then require application/json (415): a cross-site form can't
    send it without a CORS preflight, which this app never answers."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    site = request.headers.get("sec-fetch-site")
    if site is not None and site.strip().lower() not in ("same-origin", "none"):
        log.warning("refused %s %s: Sec-Fetch-Site %r", request.method, request.url.path, site)
        raise HTTPException(403, CROSS_SITE_DETAIL)
    origin = request.headers.get("origin")
    if origin is not None:
        scheme, sep, netloc = origin.strip().partition("://")
        own_scheme = "https" if _https(request) else "http"
        hosts = {request.headers.get("host", ""),
                 request.headers.get("x-forwarded-host", "").split(",")[0]}
        own = {_origin_key(own_scheme, h) for h in hosts if h.strip()}
        if not sep or "/" in netloc or _origin_key(scheme, netloc) not in own:
            log.warning("refused %s %s: Origin %r", request.method, request.url.path, origin)
            raise HTTPException(403, CROSS_SITE_DETAIL)
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise HTTPException(415, JSON_DETAIL)


class Unlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pin: str = Field(min_length=1, max_length=64)


def router(gate: OwnerGate) -> APIRouter:
    r = APIRouter(prefix="/api/advanced", dependencies=[Depends(same_origin)])

    @r.post("/unlock")
    async def unlock(body: Unlock, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        if not gate.enabled:
            raise HTTPException(403, DISABLED_DETAIL)
        client = gate.client_id(request)
        wait = gate.retry_after(client)
        if wait:
            log.warning("owner unlock refused (rate limited) from %s", client)
            raise HTTPException(429, "Too many attempts; try again later",
                                headers={"Retry-After": str(int(wait + 0.999))})
        if not gate.check_pin(body.pin):
            mine, total = gate.record_failure(client)
            log.warning("owner unlock failed from %s (%d recent from it, %d overall)", client, mine, total)
            raise HTTPException(401, "Wrong PIN")
        gate.record_success(client)
        log.info("owner unlocked Advanced controls from %s", client)
        response.set_cookie(COOKIE, gate.issue(), max_age=TOKEN_TTL, path="/", httponly=True,
                            samesite="strict", secure=_https(request))
        return {"unlocked": True}

    @r.post("/lock")
    async def lock(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict", secure=_https(request))
        return {"unlocked": False}

    return r
