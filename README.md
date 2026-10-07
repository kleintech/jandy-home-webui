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
| **Pool Chill / Heat** | `pool_chill_set_point` / `pool_set_point`. Chill ≥ 82, heat ≤ 92, heat ≥ chill + 5 |
| **Bubbles** | `aux_2` |
| **Water Features** | the device labelled **Aux V1** |
| **Spillover** | the device labelled **Spillover** (an aux or a OneTouch scene). The toggle is hidden if there isn't one |

The limits are enforced by the server, not just the sliders. Spillover and Water
Features can't both be on: the server refuses to turn one on while the other is on.

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
| `POLL_SECONDS` | `15` | how often to poll the cloud (Home Assistant uses 15) |
| `SPA_MIN` / `SPA_MAX` | `80` / `103` | spa set point range |
| `POOL_HEAT_MAX` | `92` | |
| `POOL_CHILL_MIN` | `82` | |
| `POOL_MIN_SPREAD` | `5` | |
| `POOL_HEAT_MIN` | `70` | only used when there is no chiller |
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
is never in git (this repo is public), so create it by hand in each namespace.

Build and push:

```bash
sha=$(git rev-parse HEAD)
docker build -t registry.lab.kleincogroup.com/jandy-home-webui/pool:$sha .
docker push registry.lab.kleincogroup.com/jandy-home-webui/pool:$sha
sed -i -E "s/^([[:space:]]*newTag:).*/\1 $sha/" k8s/kustomization.yaml
```

**Dev** (throwaway; delete the namespace when done):

```bash
kubectl create ns dev-jandy-home-webui
kubectl -n dev-jandy-home-webui create secret generic iaqualink-credentials \
  --from-literal=IAQUALINK_USERNAME='you@example.com' --from-literal=IAQUALINK_PASSWORD='…'
kubectl -n dev-jandy-home-webui apply -k k8s/
# if prod already exists, move the dev Ingress to pool-dev.lab.kleincogroup.com (see the lab-k3s skill)
```

**Prod** (`https://pool.lab.kleincogroup.com`, LAN only, Argo CD): create the same Secret
in namespace `pool`, commit the `newTag` bump to `main`, and add
`argocd/apps/pool.yaml` to `kleintech/lab-k3s` (copied from its
`templates/app/argocd-application.yaml`, pointing at this repo's `k8s/`).

CI runs the tests on GitHub-hosted runners. It doesn't use the lab's self-hosted
runners, because this repo is public.
