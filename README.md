# Pool & Spa guest web UI

A one-page, phone-sized web UI for guests to run a Jandy iAqualink pool/spa: switch
between Pool and Spa mode, turn the light on and pick a color, set temperatures and
toggle Bubbles, Spillover and Water Features. It is a small FastAPI service on top of
[flz/iaqualink-py](https://github.com/flz/iaqualink-py) (the library Home Assistant
uses), talking to the iAqualink cloud API with your iAqualink account.

## What each control does on the Jandy

| Control | Jandy side |
|---|---|
| **Spa Mode** | `pool_pump` on if it isn't already, then `spa_pump` (spa mode) on, then `spa_heater` on |
| **Pool Mode** | `spa_heater` off, then `spa_pump` off. The filter pump and pool heater are left alone |
| **Light** | the first light the panel reports (or `JANDY_LIGHT_DEVICE`). Turning it on always starts on white |
| **Light color** | the light's effect list. The panel's own white effect ("Alpine White" or "Cloud White") is shown as **White** |
| **Spa Set Temp** | `spa_set_point`, capped at 103 |
| **Pool Heat / Chill** | `pool_set_point` (heat, the low set point, left slider) / `pool_chill_set_point` (chill, the high one, right slider). Heat ≥ 82, chill ≤ 92, chill ≥ heat + 5 |
| **Weather** | not the Jandy: [Open-Meteo](https://open-meteo.com) forecast for the next 6 h, cached 10 min; the card hides itself if the forecast is unavailable |
| **Water Temp / Air Temp** | `spa_temp` or `pool_temp` (by mode) / `air_temp` |
| **Bubbles** | `aux_2` |
| **Water Features** | the device labelled **Aux V1** |
| **Spillover** | the device labelled **Spillover** (an aux or a OneTouch scene). The toggle is hidden if there isn't one |

The limits are enforced by the server, not just the sliders. Spillover and Water
Features can't both be on: the server refuses to turn one on while the other is on.

### How often it talks to the Jandy

Pages ask the server for state every 5 s while they are visible (hidden tabs stop). The
server polls the iAqualink cloud at most every `POLL_SECONDS` (15 s), and only while some
page has asked in the last `IDLE_SECONDS` (60 s); with nobody looking it doesn't poll at
all, and the first page to open refreshes before it answers. Weather is fetched on demand
and cached for 10 minutes.

Jandy `set_*` commands are toggles, so the service refreshes state right before every
command and only sends the ones that change something. It refuses to act (and asks the
guest to try again) when the controller is offline or sends an incomplete update, and
for 20 seconds after a command it trusts what it sent over a cloud that hasn't caught up.
A wrong password is retried with backoff (1 minute, doubling to 30), not on every poll.
Spa Mode also lowers a panel spa set point above 103 before turning the heater on.

### Mapping devices to your panel

Device names come from the panel's labels. If yours differ, set any of these
(a device key such as `aux_3`, or a label, matched case-insensitively):

`JANDY_FILTER_PUMP_DEVICE`, `JANDY_SPA_MODE_DEVICE`, `JANDY_SPA_HEATER_DEVICE`,
`JANDY_POOL_HEATER_DEVICE`, `JANDY_BUBBLES_DEVICE`, `JANDY_WATER_FEATURES_DEVICE`,
`JANDY_SPILLOVER_DEVICE`, `JANDY_LIGHT_DEVICE`.

When a configured device isn't found, the log lists every device key and label the
panel reported.

## Configuration

| Variable | Default | |
|---|---|---|
| `IAQUALINK_USERNAME`, `IAQUALINK_PASSWORD` | — | iAqualink account (required unless mock) |
| `IAQUALINK_SERIAL` | first iaqua system | pick a system if the account has several |
| `JANDY_BACKEND` | `iaqualink` | `mock` for an in-memory fake |
| `POLL_SECONDS` | `15` | how often to poll the iAqualink cloud while someone has the page open (Home Assistant uses 15) |
| `IDLE_SECONDS` | `60` | stop polling this long after the last page request |
| `SPA_MIN` / `SPA_MAX` | `80` / `103` | spa set point range |
| `POOL_HEAT_MIN` | `82` | lowest pool heat set point |
| `POOL_CHILL_MAX` | `92` | highest pool chill set point |
| `POOL_MIN_SPREAD` | `5` | chill stays at least this far above heat |
| `POOL_HEAT_MAX` | `92` | highest heat set point; with a chiller heat is also capped at chill max − spread |

The service refuses to start if the limits contradict each other (e.g. `POOL_HEAT_MIN + POOL_MIN_SPREAD > POOL_CHILL_MAX`).
| `WEATHER_LAT` / `WEATHER_LON` | `34.3033` / `-77.8039` | weather location (zip 28411) |
| `WEATHER_LABEL` | `Wilmington, NC` | shown on the weather card |
| `WEATHER_TZ` | `America/New_York` | |
| `PORT` | `8080` | (container) |
| `LOG_LEVEL` | `INFO` | |
| `MOCK_LATENCY` | `0` | seconds of fake delay per command, mock only |

Temperatures and limits are in °F. A panel set to Celsius will refuse set point
changes until the limits are set in °C.

## Development

Needs Python 3.14 (the iaqualink-py commit we pin requires it) and `uv`.

```bash
uv sync
uv run pytest
JANDY_BACKEND=mock MOCK_LATENCY=1 uv run uvicorn app.main:app --reload --port 8080
```

`tests/test_iaqualink_backend.py` runs the real library against a fake iAqualink cloud
built from recorded panel responses, so the wire commands are checked without a pool.

### Why iaqualink-py is pinned to a commit

The PyPI release (0.7.0) sends a chill set point write as `set_temps temp2=…`, which
overwrites the pool **heat** set point
([flz/iaqualink-py#274](https://github.com/flz/iaqualink-py/issues/274)). Master sends
the heat pump command `setpoint_hpm_temp`. Bump the pin on purpose, and rerun the tests.

## Deploying to the lab k3s cluster

The image is `registry.lab.kleincogroup.com/jandy-home-webui/pool:<git-sha>`. Run one
replica only, because the pod serializes commands to the Jandy. The credentials Secret
is never in git (this repo is public), so create it by hand in each namespace. Don't
build it with `--from-env-file` from a file that quotes its values: kubectl keeps the
quotes, and iAqualink then rejects the password.

Build and push:

```bash
sha=$(git rev-parse HEAD)
docker build -t registry.lab.kleincogroup.com/jandy-home-webui/pool:$sha .
docker push registry.lab.kleincogroup.com/jandy-home-webui/pool:$sha
sed -i -E "s/^([[:space:]]*newTag:).*/\1 $sha/" k8s/kustomization.yaml
```

**Dev** runs the mock backend, never the live panel, at `https://pool-dev.lab.kleincogroup.com`
(`deploy/dev/` overlays `k8s/`; no Secret needed):

```bash
kubectl create ns dev-jandy-home-webui
kubectl kustomize deploy/dev | sed "s/:dev-image-tag/:$sha/" | kubectl -n dev-jandy-home-webui apply -f -
```

**Prod** (`https://pool.lab.kleincogroup.com` on the LAN, `https://pool.kleincogroup.com` from
outside behind Cloudflare Access) is Argo CD (`argocd/apps/pool.yaml` in `kleintech/lab-k3s`)
syncing `k8s/` from `main`, namespace `pool`. To ship: pin the pushed image tag in
`k8s/kustomization.yaml` (`newTag`) and merge to `main`. The Secret is created by hand once:

```bash
kubectl -n pool create secret generic iaqualink-credentials \
  --from-literal=IAQUALINK_USERNAME='you@example.com' --from-literal=IAQUALINK_PASSWORD='…'
```

CI runs the tests on GitHub-hosted runners. It doesn't use the lab's self-hosted
runners, because this repo is public.
