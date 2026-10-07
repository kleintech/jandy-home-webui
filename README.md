# Pool & Spa — a simple guest web UI for Jandy iAqualink

A one-page, phone-sized web app for the people at your house who should be able to run
the pool and spa without the iAqualink app or your account. They can:

- switch between **Pool** and **Spa** mode
- turn the **light** on and pick a color
- set **temperatures**
- toggle **Bubbles**, **Spillover** and **Water Features**

It also shows the water and air temperature and an optional 6-hour **weather** forecast,
and can be added to a phone's home screen like an app.

It runs as one small container on your home network and talks to your pool through the
iAqualink cloud, using [flz/iaqualink-py](https://github.com/flz/iaqualink-py) (the
library Home Assistant uses). The server enforces house rules such as a 103 °F spa cap,
pool set point ranges, and Spillover never running together with Water Features; they
aren't just limits on the sliders.

> **There is no built-in login.** Anyone who can open the page can run your pool. Keep it
> on your home network, or put an authenticating proxy in front of it (see
> [Remote access](#remote-access)). Never port-forward it to the Internet.

## Contents

- [What you need](#what-you-need)
- [Quick start with Docker](#quick-start-with-docker)
- [Map the controls to your panel](#map-the-controls-to-your-panel)
- [Configuration reference](#configuration-reference)
- [What each control does](#what-each-control-does)
- [Safety, and how it talks to the Jandy](#safety-and-how-it-talks-to-the-jandy)
- [Add to Home Screen](#add-to-home-screen)
- [Remote access](#remote-access)
- [Deploying on Kubernetes](#deploying-on-kubernetes)
- [Security notes](#security-notes)
- [Development](#development)
- [Troubleshooting](#troubleshooting)

## What you need

- **A supported panel.** A Jandy **AquaLink RS** panel with an **iAqualink** module that
  already works in the iAqualink phone app. The app calls these "iaqua" systems. The
  newer eXO/iQ20-style systems aren't supported by this UI.
- **Your iAqualink account** email and password. Consider creating a separate iAqualink
  user for this, so you can revoke it on its own.
- **Something on your home network that runs Docker**, such as a NAS, a Raspberry Pi
  4/5, a mini PC, or a Kubernetes cluster.
- **A panel set to °F.** A panel set to °C works too, but then you must set the
  temperature limits in °C yourself (see [Configuration](#configuration-reference)).

## Quick start with Docker

```bash
git clone https://github.com/kleintech/jandy-home-webui.git
cd jandy-home-webui
cp .env.example .env
```

1. **Try it without touching your pool.** In `.env`, set `JANDY_BACKEND=mock`, then run:

   ```bash
   docker compose up -d --build
   ```

   Open `http://<that-machine's-IP>:8080` on your phone. Everything works against a
   pretend pool.

2. **Connect it to your pool.** Put your iAqualink login in `.env` (no quotes needed) and
   set `JANDY_BACKEND=iaqualink`. Then list what your panel has:

   ```bash
   docker compose run --rm pool python -m app.discover
   ```

   This is read-only: it logs in, lists every device key, label and state, and never
   sends a command. Use its output to [map the controls](#map-the-controls-to-your-panel),
   then start the app:

   ```bash
   docker compose up -d --build
   docker compose logs -f      # look for "using iAqualink system <name>"
   ```

3. **Optional:** set `WEATHER_ZIP` (or `WEATHER_LAT` and `WEATHER_LON`) to turn on the
   weather card, and give the machine a stable IP address or DNS name.

To update later, run `git pull && docker compose up -d --build`.

## Map the controls to your panel

Every panel is wired differently. Each control points at one device on your panel,
given either as a **device key** (`aux_3`) or as the **label** you gave it in iAqualink
(`Spillover`, matched case-insensitively).

| Control | Setting | Default | Typical value |
|---|---|---|---|
| Filter pump (turned on for spa) | `JANDY_FILTER_PUMP_DEVICE` | `pool_pump` | rarely changes |
| Spa mode | `JANDY_SPA_MODE_DEVICE` | `spa_pump` | rarely changes |
| Spa heat | `JANDY_SPA_HEATER_DEVICE` | `spa_heater` | rarely changes |
| Bubbles | `JANDY_BUBBLES_DEVICE` | `aux_2` | your blower / jets aux |
| Water Features | `JANDY_WATER_FEATURES_DEVICE` | `Aux V1` | the aux for fountains or sheer descents |
| Spillover | `JANDY_SPILLOVER_DEVICE` | `Spillover` | an aux or a OneTouch scene with that name |
| Light | `JANDY_LIGHT_DEVICE` | first light found | the aux with your color light |

Here is example output from `python -m app.discover` on a real panel:

```
aux_1   Pool Light    IaquaColorLightJL   '0' <- light
aux_2   Air Blower    IaquaAuxSwitch      '0' <- switch
aux_3   Spillover     IaquaAuxSwitch      '0' <- switch
aux_4   Wtr Feature   IaquaAuxSwitch      '0' <- switch
```

On this panel the defaults find Bubbles (`aux_2`), Spillover (by its label) and the
light. "Aux V1" had been renamed "Wtr Feature", so Water Features needs
`JANDY_WATER_FEATURES_DEVICE=aux_4`.

When a configured device isn't found, the Spillover toggle is hidden and other controls
return an error. The log then lists every key and label the panel reported.

## Configuration reference

All settings are environment variables: in `.env` for Docker, or in a ConfigMap and
Secret on Kubernetes.

| Variable | Default | Meaning |
|---|---|---|
| `IAQUALINK_USERNAME`, `IAQUALINK_PASSWORD` | — | **Secret.** iAqualink login. Required unless `JANDY_BACKEND=mock`. |
| `IAQUALINK_SERIAL` | first iaqua system | which system to use, if the account has several |
| `JANDY_BACKEND` | `iaqualink` | `mock` runs an in-memory pretend pool |
| `JANDY_*_DEVICE` | see above | which panel device each control drives |
| `SPA_MIN` / `SPA_MAX` | `80` / `103` | spa set point range |
| `POOL_HEAT_MIN` | `82` | lowest pool heat set point (the low set point) |
| `POOL_CHILL_MAX` | `92` | highest pool chill set point (the high set point; heat pumps with chill only) |
| `POOL_MIN_SPREAD` | `5` | chill always stays at least this far above heat |
| `POOL_HEAT_MAX` | `92` | highest heat set point. With a chiller, heat is also capped at chill max − spread |
| `WEATHER_ZIP` | — | zip code for the weather card, looked up once at zippopotam.us |
| `WEATHER_COUNTRY` | `us` | country code for `WEATHER_ZIP` (zippopotam.us supports about 60 countries) |
| `WEATHER_LAT`, `WEATHER_LON` | — | coordinates to use instead of a zip (both are needed) |
| `WEATHER_LABEL` | place name | text shown on the weather card |
| `WEATHER_TZ` | `auto` | IANA timezone for forecast times. `auto` uses the location's timezone |
| `POLL_SECONDS` | `15` | how often to poll iAqualink while someone has the page open |
| `IDLE_SECONDS` | `60` | stop polling this long after the last page request |
| `PORT` | `8080` | listen port inside the container |
| `LOG_LEVEL` | `INFO` | log verbosity |
| `MOCK_LATENCY` | `0` | seconds of fake delay per command (mock only) |

The service refuses to start if the limits contradict each other, for example when
`POOL_HEAT_MIN + POOL_MIN_SPREAD > POOL_CHILL_MAX`. If neither `WEATHER_ZIP` nor
`WEATHER_LAT` and `WEATHER_LON` are set, the weather card stays hidden and no weather
data is fetched.

## What each control does

| Control | On the Jandy |
|---|---|
| **Spa Mode** | Turns the filter pump on (only if it's off), then spa mode on, then spa heat on. If the panel's spa set point is above `SPA_MAX`, it's lowered to `SPA_MAX` first. |
| **Pool Mode** | Turns spa heat off, then spa mode off. The filter pump schedule and the pool heater or heat pump are left alone. |
| **Light** | Turning it on always starts on white. Off is off. |
| **Light color** | The light's own color list (Jandy WaterColors, Pentair, Hayward, …). The panel's white ("Alpine White", "Cloud White") is shown as **White**. |
| **Spa Set Temp** | `spa_set_point`, from `SPA_MIN` to `SPA_MAX`. |
| **Pool Set Temp** | One bar with two handles: **Heat** (the low set point, `pool_set_point`) and **Chill** (the high one, `pool_chill_set_point`; heat pumps with chill only). |
| **Bubbles / Water Features / Spillover** | Switches the mapped device on or off. Spillover and Water Features can't both be on. |
| **Water Temp / Air Temp** | `spa_temp` or `pool_temp` (depending on the mode) / `air_temp`. |
| **Weather** | Not from the Jandy: an [Open-Meteo](https://open-meteo.com) forecast for the next 6 hours, cached for 10 minutes. |

The dot next to the title shows whether the server can reach your pool controller: green
when it can, red when it can't.

## Safety, and how it talks to the Jandy

- **Jandy commands are toggles.** Sending "pump" twice turns it back off. So the server
  reads fresh state right before every command and only sends commands that change
  something.
- **It refuses rather than guesses.** If the controller is offline, or the cloud sends an
  incomplete update (which happens), the command is refused with "try again" instead of
  toggling blind.
- **Double taps don't undo themselves.** For 20 seconds after a command, the server
  trusts what it sent over a cloud that hasn't caught up yet.
- **Set point writes keep the spread.** Heat and chill are written in an order that keeps
  chill at least `POOL_MIN_SPREAD` above heat, even if one of the two writes fails.
- **It only polls while someone is looking.** Open pages ask the server for state every
  5 seconds; hidden tabs stop asking. The server polls iAqualink at most every
  `POLL_SECONDS`, and only if a page has asked within the last `IDLE_SECONDS`. When nobody
  is looking it doesn't poll at all, and the first page to open gets a fresh refresh
  before its answer.
- **A wrong password backs off** (1 minute, doubling up to 30) instead of retrying on
  every poll, so your account doesn't get locked.
- **Run only one instance.** It serializes commands to your pool, and two copies would
  race each other's toggles.

## Add to Home Screen

The page can be installed as an app, with an icon, the name "Pool" and a full-screen
view. Visitors see a small banner suggesting it, with **Remind me later** (7 days) and
**Don't show again**. The choice is stored in that phone's browser storage and is never
sent to the server.

- **iPhone (Safari):** works over plain `http://` too. Tap Share, then Add to Home
  Screen.
- **Android (Chrome):** the one-tap install button needs **HTTPS**, because browsers only
  allow service workers on secure origins. Over plain `http://<ip>:8080`, use Chrome's
  menu → Add to Home screen instead.

For guests, the easiest way in is a **QR code** pointing at your URL, printed and placed
by the pool.

## Remote access

Keep the app itself unauthenticated on your LAN, and put authentication in front of it
for access from outside. Good options:

- **Cloudflare Tunnel + Cloudflare Access** (free tier). No open ports, and a login page
  (one-time email PIN, Google, and others) in front of a public hostname. If your LAN DNS
  resolves the same hostname straight to your server, people at home skip the login.
- **Tailscale, WireGuard or your router's VPN.** Your phone reaches your LAN as if you
  were at home.
- **A reverse proxy with authentication**, such as Authelia, oauth2-proxy, or Traefik or
  NGINX basic auth.

Don't expose the container port directly to the Internet.

## Deploying on Kubernetes

`k8s/` is a kustomization (Deployment, Service, Ingress, ConfigMap) written for the
author's home lab, which uses Traefik ingress, wildcard TLS from a default TLSStore, an
in-cluster registry and Argo CD. Copy it and change these for your cluster:

| File | Change |
|---|---|
| `k8s/kustomization.yaml` | `images:` name and `newTag`: where you push the image |
| `k8s/ingress.yaml` | `ingressClassName`, hosts, and TLS (`secretName` if you don't have a default certificate) |
| `k8s/configmap.yaml` | your limits and `JANDY_*_DEVICE` mapping |

Then build, push and apply:

```bash
docker build -t <registry>/jandy-home-webui:<tag> .
docker push <registry>/jandy-home-webui:<tag>

kubectl create namespace pool
# Credentials, plus anything you'd rather not commit (for example your WEATHER_ZIP):
kubectl -n pool create secret generic iaqualink-credentials \
  --from-literal=IAQUALINK_USERNAME='you@example.com' \
  --from-literal=IAQUALINK_PASSWORD='…' \
  --from-literal=WEATHER_ZIP='12345'
kubectl -n pool apply -k k8s/
```

- **Any key in the Secret becomes a setting**, because the Secret is loaded with
  `envFrom`.
- **Don't build the Secret with `--from-env-file` from a file whose values are quoted.**
  kubectl keeps the quotes, and iAqualink then rejects the password.
- **Keep it at one replica.** The Deployment runs one replica with the `Recreate`
  strategy, as a non-root user, with a read-only root filesystem.
- **`deploy/dev/` is an overlay that always runs the mock backend.** It drops the Secret
  and forces `JANDY_BACKEND=mock`, so you can try changes without touching the pool:
  `kubectl kustomize deploy/dev | sed "s/:dev-image-tag/:<tag>/" | kubectl -n <dev-ns> apply -f -`

## Security notes

- **Credentials** live only in `.env` (git-ignored) or a Kubernetes Secret. They are
  never logged.
- **No request URLs in the logs.** The service doesn't log request URLs, because
  iAqualink URLs carry a session ID. Errors shown to users are generic; details go only
  to the log.
- **The API returns pool state only.** It never returns the account, serial or session.
  `python -m app.discover` prints only the last 4 characters of a serial.
- **There is no authentication of its own.** See [Remote access](#remote-access).
- **The container** runs as uid 1000 with a read-only root filesystem, and needs no extra
  Linux capabilities.
- **Outbound connections:**
  - iAqualink: `prod.zodiac-io.com` and `*.iaqualink.net`.
  - Weather, only if enabled: `api.open-meteo.com` and `api.zippopotam.us`.

## Development

Needs Python 3.14 (the pinned iaqualink-py commit requires it) and
[uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest
JANDY_BACKEND=mock MOCK_LATENCY=1 uv run uvicorn app.main:app --reload --port 8080
```

Where things are:

- `app/service.py`: the house rules.
- `app/backends/iaqualink.py`: maps the house rules onto iaqualink-py.
- `app/backends/mock.py`: the pretend pool.
- `app/weather.py`: the forecast.
- `app/discover.py`: the read-only device lister.
- `app/static/`: the UI, in plain HTML, CSS and JS with no build step and no CDNs.

`tests/test_iaqualink_backend.py` runs the real library against a fake iAqualink cloud
built from recorded panel responses, so the exact wire commands are checked without a
pool. CI runs the tests on GitHub-hosted runners.

### Why iaqualink-py is pinned to a commit

The PyPI release (0.7.0) sends a chill set point write as `set_temps temp2=…`, which
overwrites the pool **heat** set point
([flz/iaqualink-py#274](https://github.com/flz/iaqualink-py/issues/274)). The current
master sends the heat pump command `setpoint_hpm_temp` instead. Only bump the pin on
purpose, and rerun the tests when you do.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Red dot and "Can't reach the pool controller" | Wrong login (the log says "rejected the username/password"), the panel is offline in the iAqualink app, or there's no Internet connection |
| A toggle says "try again" | The controller sent an incomplete update or is offline. The server refuses rather than guessing |
| Spillover toggle missing | No device matches `JANDY_SPILLOVER_DEVICE`. Run `python -m app.discover` |
| Set point changes refused on a °C panel | Set the `SPA_*` and `POOL_*` limits in °C |
| Weather card missing | No `WEATHER_ZIP` or `WEATHER_LAT`/`WEATHER_LON` is set, or Open-Meteo is unreachable |
| No "Add to Home Screen" button on Android | The site isn't on HTTPS. Use Chrome's menu → Add to Home screen |
