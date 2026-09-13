# autodlart2openai

> [中文](README.md) | English

A minimal protocol-translation gateway that wraps
[AutoDL's ComfyUI workflow API](https://www.autodl.art/) behind an
OpenAI-compatible `/v1/videos` interface. **Translation only** — no task
queue, no database, no user accounts.

```
Client (OpenAI shape) ──▶ this gateway ──▶ AutoDL ComfyUI
```

- Single `uvicorn` process, 4 runtime dependencies (`fastapi` / `uvicorn` / `httpx` / `python-multipart`)
- Runs comfortably on a $5/month Linux VPS, supervised by systemd
- Clients forward their AutoDL token verbatim via `Authorization: Bearer <autodl-token>`

## Contents

- [Quick start](#quick-start)
- [API overview](#api-overview)
- [Request examples](#request-examples)
- [Configuration](#configuration)
- [Deployment](#deployment)
- [Logs and operations](#logs-and-operations)
- [Architecture and layout](#architecture-and-layout)

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app                           # listens on 0.0.0.0:8765
curl -sS http://localhost:8765/healthz  # {"status":"ok"}
```

Need a different port or upstream URL? Override via env vars (see [Configuration](#configuration)).
For development with hot-reload:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8765 --reload
```

## API overview

| Client | Forwarded to AutoDL |
|---|---|
| `GET  /v1/models` | `POST .../api/v1/comfyui/workflows` (cached) |
| `POST /v1/videos` | `POST .../comfyui_workflow/{workflow_id}` |
| `GET  /v1/videos/{id}` | `GET  .../comfyui_workflow/result/{task_id}` |
| `GET  /v1/videos/{id}/content` | Query once, then `302` to the first result URL |
| `GET  /v1/workflows/{id}/schema` | Cached `size` whitelist + required fields |
| `GET  /healthz` | Liveness probe |

`{id}` **is** the AutoDL `task_id`. The gateway doesn't keep a task table — it only forwards.

**Request body mapping** (`POST /v1/videos`):

| OpenAI field | Upstream field | Notes |
|---|---|---|
| `model` | URL path only | never forwarded |
| `prompt` | `prompt` | passed through |
| `seconds` | `duration` | only if `duration` not in body |
| `size` | `resolution` | `"WxH"` → nearest whitelist label (e.g. `"480x480"` → `"480p(1:1)"`) |
| `metadata` | merged into top level | top-level keys win on collision |

The body then runs through a three-step normalization: whitelist filter (drops
client-UI fields), type coercion (`"6"` → `6`), and random `seed` injection
when missing.

**`model` → workflow mapping**: An AutoDL "workflow" is exactly what an
OpenAI-style client calls a "model", so the gateway uses the `model` field
verbatim as the `workflow_id` in the URL path
(`.../comfyui_workflow/{workflow_id}`). The gateway itself performs no semantic
model→workflow translation. The list of available workflows is fetched via
`GET /v1/models` (cached at startup or on first request).

**`size` → `resolution` mapping**: OpenAI sends pixel `WxH` (e.g. `"480x480"`,
`"720x1280"`); AutoDL wants a **human-readable label** (e.g. `"480p(1:1)"`,
`"736p竖"`), drawn from the workflow schema cache. The gateway first
classifies the aspect ratio (square / portrait / landscape), then picks the
nearest label in the same direction; if none matches, it falls back to the
nearest total-pixel tier.

**Status mapping**: `QUEUED→queued` / `RUNNING→in_progress` / `SUCCESS→completed` / `FAILED→failed`.

**Authentication**: clients send the AutoDL ComfyUI token in
`Authorization: Bearer <token>`. The gateway strips `Bearer` and forwards the
raw token verbatim. It stores no keys and signs nothing. Missing or malformed
headers → `401`.

## Request examples

Any OpenAI-style client works. Point its base URL at your gateway — the
public form is `http://<your-server-ip>:8765/v1` (the IP is whatever your
deployment returns; **don't** use the placeholder in this repo):

```python
# Python — openai SDK
from openai import OpenAI
client = OpenAI(
    base_url="http://<your-server-ip>:8765/v1",
    api_key="<your-autodl-comfyui-token>",   # forwarded to AutoDL
)
resp = client.videos.generate(model="<workflow-id>", prompt="a cat in space")
```

```bash
# curl
curl -X POST http://<your-server-ip>:8765/v1/videos \
  -H "Authorization: Bearer <your-autodl-comfyui-token>" \
  -H "Content-Type: application/json" \
  -d '{"model":"<workflow-id>","prompt":"a cat in space","size":"480x480"}'
```

Once you have an `id`, poll `GET /v1/videos/{id}`. When complete, hit
`GET /v1/videos/{id}/content` for a direct `302` to the result URL.

## Configuration

Everything is an environment variable with a sane default. **Nothing is required.**

| Variable | Default | Purpose |
|---|---|---|
| `GATEWAY_HOST` | `0.0.0.0` | uvicorn listen address |
| `GATEWAY_PORT` | `8765` | uvicorn listen port |
| `LOG_LEVEL` | `INFO` | root logger level |
| `AUTODL_BASE_URL` | `https://www.autodl.art/api/v1/comfyui` | upstream base URL |
| `AUTODL_BOOTSTRAP_TOKEN` | unset | token for eager cache warmup at startup |
| `SKIP_STARTUP_SYNC` | `0` | set `1` to skip eager sync (lazy on first request) |
| `TIMEOUT_CONNECT` / `READ` / `WRITE` / `POOL` | `5.0` / `30.0` / `10.0` / `5.0` | httpx timeouts (s) |
| `TIMEOUT_SUBMIT` | `60.0` | override for image-heavy submits |
| `MAX_CONNECTIONS` / `MAX_KEEPALIVE` | `100` / `20` | connection pool |
| `KEEPALIVE_EXPIRY` | `30.0` | idle keepalive TTL (s) |
| `CORS_ORIGINS` | `*` | comma-separated allowed origins |
| `LOG_FILE` | `logs/gateway.log` | rotating log file ("" = stderr only) |
| `LOG_MAX_BYTES` / `LOG_BACKUP_COUNT` | `10485760` / `5` | rotation policy |
| `LOG_UPSTREAM_RESPONSES` | `0` | set to `1` to log a DEBUG-level digest of every upstream AutoDL response (task_id / status / progress / result URLs / top-level key names) — never the full body, never base64 |

The workflow schema cache lives in memory and is single-flight-synced once
at startup (when `AUTODL_BOOTSTRAP_TOKEN` is set) or on the first request.
**Not persisted, not background-refreshed** — restart to refresh.

## Deployment

### Recommended: systemd (production VPS)

```bash
git clone <repo> autodlart2openai
cd autodlart2openai
bash deploy/install.sh                     # default: 0.0.0.0:8765
```

The installer: rsyncs the project to `/opt/autodl-openai-gateway` → builds
`.venv` and installs requirements → writes `/etc/autodl-openai-gateway.env`
→ installs the systemd unit (which reads that env file via `EnvironmentFile=`)
→ enables + starts the unit → hits `/healthz` to verify.

To change the port, upstream URL, CORS, etc., **pass them at install time**:

```bash
GATEWAY_PORT=9000 \
AUTODL_BASE_URL=https://www.autodl.art/api/v1/comfyui \
AUTODL_BOOTSTRAP_TOKEN=eyJhbGciOi... \
CORS_ORIGINS="https://my-frontend.example.com" \
bash deploy/install.sh
```

**After the service is running** — edit the env file and restart:

```bash
sudo $EDITOR /etc/autodl-openai-gateway.env
sudo systemctl restart autodl-openai-gateway
curl -sS http://127.0.0.1:9000/healthz
```

> **Ports < 1024**: this unit has no `CAP_NET_BIND_SERVICE`, so don't set
> `GATEWAY_PORT` to 80/443. Stay above 1024 (recommended) or put nginx / Caddy
> in front.

### Open the port on the VPS right after install

The gateway defaults to listening on `8765/TCP`. Once it's up on the VPS,
you must open the port in **both** layers:

```bash
# Ubuntu / Debian (UFW)
sudo ufw allow 8765/tcp                       # open to everyone
sudo ufw allow from 1.2.3.4 to any port 8765  # open to one IP only (safer)
sudo ufw reload

# CentOS / RHEL / Fedora (firewalld)
sudo firewall-cmd --permanent --add-port=8765/tcp
sudo firewall-cmd --reload

# Verify the port is actually up on the host
ss -ltnp | grep ':8765'
curl -sS http://127.0.0.1:8765/healthz        # {"status":"ok"}
```

**Cloud security group must also be opened** — this is where ~90% of "installed
but can't connect" failures live. AWS Lightsail / DigitalOcean / Vultr /
Aliyun ECS / Tencent CVM and most other providers block inbound traffic by
default; in the provider's web console, add an inbound TCP 8765 rule to the
instance's security group / firewall. Until you do, `curl 127.0.0.1` from
inside the VPS works, but `curl <public-ip>` from your laptop times out.

### Reaching it from the public internet

With the port open, the public base URL becomes
`http://<your-server-ip>:<port>/v1` (get the IP via `curl ifconfig.me` or
from your cloud console). For HTTPS / your own domain, put nginx or Caddy in
front; Caddy issues and renews certificates automatically:

```caddyfile
autodl.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

## Logs and operations

One structured JSON line per request, written to both stderr (captured by
systemd) and a rotating file at `logs/gateway.log`:

```json
{"ts":1700000000000,"level":"INFO","logger":"autodl-openai-gateway",
 "msg":"access","method":"POST","path":"/v1/videos","status":202,
 "upstream_status":200,"latency_ms":87,"client":"1.2.3.4"}
```

**Never logged**: request bodies, tokens, cookies. Upstream URLs are truncated
in error lines.

## Upgrading

The installer is **idempotent**. By default it runs a clean
**stop → swap → start** sequence:

1. `systemctl stop` the running service (if any)
2. rsync the new project files over the install dir
3. rebuild the venv and `pip install -U` the dependencies
4. rewrite `/etc/autodl-openai-gateway.env` from your current shell
5. `systemctl enable --now` brings the service back up

This avoids rsync clobbering a live Python process and causing
`ImportError` on the next restart.

To upgrade (run on the VPS):

```bash
# 1) Enter the install dir (default /opt/autodl-openai-gateway)
cd /opt/autodl-openai-gateway

# 2) Pull the latest code (only if you originally git-cloned the repo)
git pull

# 3) Re-run install.sh with the same env vars you used the first time
sudo bash deploy/install.sh
```

To skip the stop step (advanced — usually you do NOT want this):

```bash
# Keep the old service running across the file swap. Use only when
# you know no module-level imports change (e.g. README-only commits).
STOP_FIRST=0 sudo bash deploy/install.sh
```

> If you've hand-edited `/etc/autodl-openai-gateway.env` since installing
> (timeouts, port, `LOG_UPSTREAM_RESPONSES=1`, etc.), re-export the keys
> you want to keep into your shell before re-running the installer —
> otherwise they will be overwritten. The `KNOWN_KEYS` list at the top
> of `deploy/install.sh` is the authoritative whitelist of every key
> the gateway understands.

To roll back to a previous version:

```bash
cd /opt/autodl-openai-gateway
git checkout <old commit or tag>
sudo bash deploy/install.sh
```

Day-to-day:

```bash
sudo systemctl status autodl-openai-gateway
sudo journalctl -fu autodl-openai-gateway
sudo systemctl restart autodl-openai-gateway

sudo systemctl show autodl-openai-gateway -p Environment | tr ' ' '\n' | grep GATEWAY_
ss -ltnp | grep ':8765'                                  # confirm the port
```

## Architecture and layout

```
app/
├── main.py              FastAPI app + lifespan + CORS + middleware
├── settings.py          env-driven Settings dataclass
├── auth.py              Bearer-token extraction
├── errors.py            typed AppError hierarchy + exception handler
├── upstream.py          async AutoDL HTTP client (owns httpx.AsyncClient)
├── cache.py             workflow schema cache (synced at startup)
├── routes.py            5 endpoints + whitelist filter + seed auto-fill
├── mapping.py           pure functions: body / status / URL / size→resolution / type coercion
└── logging_config.py    JSON formatter + rotating file + access log
deploy/
├── autodl-openai-gateway.service    systemd unit (with hardening)
└── install.sh                       one-shot installer
tests/                   test_auth / test_cache / test_mapping / test_routes
```

### What this gateway deliberately does NOT do

- it does **not** wait for results — submission returns immediately
- it does **not** store task state — it always asks AutoDL
- it does **not** proxy media bytes — clients follow the `302`
- it does **not** proxy or transform reference images
- it does **not** manage users, billing, or rate limits
- it does **not** retry — clients poll the result endpoint
- it does **not** require server-side secrets — every request carries its own token

## Acknowledgements

`app/upstream.py` borrows the recursive `extract_urls` helper and the
`{code, data}` envelope unwrap from the `ComfyUI-AutoDL-API` ComfyUI node.
Everything ComfyUI-specific (torch, PIL, async progress widgets) was dropped
so this gateway has no heavy dependencies.