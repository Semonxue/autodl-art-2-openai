# autodlart2openai

A thin, async-first gateway that exposes AutoDL's ComfyUI workflow API
through an OpenAI-compatible `/v1/videos` interface. It does **only**
protocol translation – no task queue, no database, no user accounts.

```
Client ── OpenAI shape ──▶  this gateway  ── AutoDL shape ──▶  AutoDL ComfyUI
```

Designed for a $5/month Linux VPS, single uvicorn process, supervised
by systemd.

## Endpoints

| Client                                          | Upstream                                              |
|-------------------------------------------------|-------------------------------------------------------|
| `GET  /v1/models`                               | `POST .../api/v1/comfyui/workflows` (cached)          |
| `POST /v1/videos`                               | `POST .../comfyui_workflow/{workflow_id}`             |
| `GET  /v1/videos/{id}`                          | `GET  .../comfyui_workflow/result/{task_id}`          |
| `GET  /v1/videos/{id}/content`                  | query once, then `302` to the first result URL        |
| `GET  /v1/workflows/{id}/schema`                | cached – returns legal `size` values & required fields |
| `GET  /healthz`                                 | – (liveness probe)                                    |

The path component `{id}` **is** the AutoDL `task_id`. The gateway does
not maintain a task table – it just forwards.

## Workflow schema cache

`/v1/models`, `/v1/workflows/{id}/schema`, and `size` validation in
`/v1/videos` are all served from an in-memory cache that is populated
once at startup by walking every workflow's `GET /workflows/{id}`
schema. The cache holds:

* workflow id + display name (for `/v1/models`)
* legal `resolution` labels (for `size` validation – e.g.
  `["480p横", "480p竖", "768p横", "768p竖", "480p(1:1)", "768p(1:1)"]`)
* required input fields (e.g. `["prompt", "ref_image_0"]`)
* the full accepted field list + field types (for whitelist filtering and
  type coercion)

When `AUTODL_BOOTSTRAP_TOKEN` is set in the environment, the cache is
populated eagerly at startup. When unset, the first authenticated
request triggers a lazy sync (single-flight via an asyncio.Lock). No
file persistence, no background refreshers – restart to refresh.

## Authentication

The client passes its AutoDL ComfyUI token the OpenAI way:

```
Authorization: Bearer <autodl-token>
```

The gateway strips `Bearer` and forwards the raw token verbatim to
AutoDL. No keys are stored, validated, or signed by the gateway.
Missing or malformed Authorization headers → `401`.

## Request body mapping (`POST /v1/videos`)

| OpenAI key | Sent upstream as        | Notes                              |
|------------|-------------------------|------------------------------------|
| `model`    | URL path only           | never forwarded                    |
| `prompt`   | `prompt`                | passed through                     |
| `seconds`  | `duration`              | only if `duration` not in body     |
| `size`     | `resolution`            | `"480x480"` → closest label (see below) |
| `metadata` | merged into top level   | top-level keys win on collision    |

The request then passes through a three-step normalization pipeline:

1. **size → resolution mapping** — OpenAI sends `WxH` (e.g. `"480x480"`);
   AutoDL wants a label (`"480p(1:1)"`, `"736p竖"`…). The gateway derives
   orientation (square/portrait/landscape) + nearest pixel tier from the
   cached whitelist, so `"480x480"` → `"480p(1:1)"` on a workflow that
   offers 480p, or `"736p(1:1)"` on a 736p-only workflow.

2. **whitelist filter** — only fields the workflow's `input_rules`
   declares are forwarded. Client-side UI fields (`preset`,
   `resolution_name`, `shotType`, `watermark`, …) are dropped.

3. **type coercion + seed auto-fill** — `integer`/`float` fields are
   coerced from strings (`"6"` → `6`); if the workflow declares `seed`
   but the client didn't send one, a random seed is injected so identical
   prompts don't produce identical output.

## Status mapping

| AutoDL     | OpenAI        |
|------------|---------------|
| `QUEUED`   | `queued`      |
| `RUNNING`  | `in_progress` |
| `SUCCESS`  | `completed`   |
| `FAILED`   | `failed`      |

## Configuration

Everything is an environment variable with a sane default. Nothing
required to start; everything optional.

| Variable                  | Default                                      | Purpose                          |
|---------------------------|----------------------------------------------|----------------------------------|
| `GATEWAY_HOST`            | `0.0.0.0`                                    | uvicorn listen address           |
| `GATEWAY_PORT`            | `8000`                                       | uvicorn listen port              |
| `LOG_LEVEL`               | `INFO`                                       | root logger level                |
| `AUTODL_BASE_URL`         | `https://www.autodl.art/api/v1/comfyui`      | upstream base URL                |
| `AUTODL_BOOTSTRAP_TOKEN`  | unset                                        | optional token for startup sync  |
| `SKIP_STARTUP_SYNC`       | `0`                                          | set `1` to skip the eager sync   |
| `TIMEOUT_CONNECT`         | `5.0`                                        | httpx connect timeout (s)        |
| `TIMEOUT_READ`            | `30.0`                                       | httpx read timeout (s)           |
| `TIMEOUT_SUBMIT`          | `60.0`                                       | override for image-heavy submits |
| `TIMEOUT_WRITE`           | `10.0`                                       | httpx write timeout (s)          |
| `TIMEOUT_POOL`            | `5.0`                                        | httpx pool acquisition timeout   |
| `MAX_CONNECTIONS`         | `100`                                        | total pool size                  |
| `MAX_KEEPALIVE`           | `20`                                         | idle keepalive connections       |
| `KEEPALIVE_EXPIRY`        | `30.0`                                       | idle keepalive TTL (s)           |
| `CORS_ORIGINS`            | `*`                                          | comma-separated allowed origins  |
| `LOG_FILE`                | `logs/gateway.log`                           | rotating log file ("" = stderr only) |
| `LOG_MAX_BYTES`           | `10485760`                                   | max bytes per log file           |
| `LOG_BACKUP_COUNT`        | `5`                                          | rotated log files to keep        |

## Running locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python -m app                 # binds 0.0.0.0:8000
# or:
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

Then point any OpenAI-style client at `http://localhost:8000/v1` with
your AutoDL token as the API key.

## Running the test suite

```bash
pip install -r requirements-dev.txt
pytest -q
```

The suite is fully offline (uses `httpx.MockTransport`) and covers
mapping, auth, cache validation, all routes, and lifespan-managed client
lifecycle. **73 test cases** as of v0.3.

## End-to-end smoke check (against a live gateway)

```bash
pip install -r requirements-dev.txt
AUTODL_TOKEN=... bash scripts/smoke.sh
```

The script verifies the four endpoints return the expected status codes
and prints the picked workflow id + task id.

## Deploying on a $5 VPS

```bash
# On the VPS, as a sudo-enabled user:
git clone <repo> autodlart2openai
cd autodlart2openai
bash deploy/install.sh
```

The installer:
1. rsyncs the project into `/opt/autodl-openai-gateway`
2. creates `.venv` and installs requirements
3. installs the systemd unit and enables it
4. verifies `/healthz` is reachable on the configured port

Useful follow-ups:
```bash
sudo systemctl status autodl-openai-gateway
sudo journalctl -fu autodl-openai-gateway
sudo systemctl restart autodl-openai-gateway
```

## Observability

The gateway emits one structured JSON log line per request to **both**
stderr (systemd captures this) and a rotating file at
`logs/gateway.log` (see `LOG_FILE`). Example:

```json
{"ts":1700000000000,"level":"INFO","logger":"autodl-openai-gateway",
 "msg":"access","method":"POST","path":"/v1/videos","status":202,
 "upstream_status":200,"latency_ms":87,"client":"1.2.3.4"}
```

Plus startup/shutdown lines and any `upstream_error` messages with the
underlying upstream URL truncated for safety. No request bodies, no
tokens, no cookies are ever logged.

## What this gateway deliberately does NOT do

* it does **not** wait for results – submission returns immediately
* it does **not** store task state – it always asks AutoDL
* it does **not** proxy media bytes – clients follow the `302`
* it does **not** proxy or transform reference images
* it does **not** manage users, billing, or rate limits
* it does **not** retry – clients poll the result endpoint
* it does **not** require server-side secrets – every request carries
  its own token

## Architecture

```
app/
├── main.py           FastAPI app + lifespan + CORS + middleware wiring
├── settings.py       env-driven Settings dataclass
├── auth.py           Bearer-token extraction
├── errors.py         typed AppError hierarchy + exception handler
├── upstream.py       async AutoDL HTTP client (owns httpx.AsyncClient)
├── cache.py          workflow schema cache (synced at startup)
├── routes.py         5 endpoints + whitelist filter + seed auto-fill
├── mapping.py        pure functions: body mapping, status mapping,
│                     URL extraction, size→resolution, type coercion
└── logging_config.py JSON formatter + rotating file + access log
deploy/
├── autodl-openai-gateway.service   systemd unit (with hardening)
└── install.sh                       one-shot installer
scripts/
└── smoke.sh                         end-to-end curl check
tests/
├── test_auth.py       10 cases
├── test_cache.py      7 cases
├── test_mapping.py    35 cases
└── test_routes.py     21 cases
```

Total runtime dependencies: **4** – `fastapi`, `uvicorn[standard]`,
`httpx`, `python-multipart`. No torch, no PIL, no numpy, no requests.

## Acknowledgements

`app/upstream.py` borrows the recursive `extract_urls` helper and the
`{code, data}` envelope unwrap from the
[`ComfyUI-AutoDL-API`](https://...) ComfyUI node. Everything
ComfyUI-specific (torch, PIL, async progress widgets) was dropped so
this gateway has no heavy dependencies.
