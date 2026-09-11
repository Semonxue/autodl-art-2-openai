# autodlart2openai

> [English](README.en.md) | 中文

一个极薄的协议网关，把 [AutoDL ComfyUI 工作流 API](https://www.autodl.art/) 包装成
OpenAI 兼容的 `/v1/videos` 接口。**只做翻译**——没有任务队列、没有数据库、没有用户系统。

```
Client (OpenAI shape) ──▶ this gateway ──▶ AutoDL ComfyUI
```

- 单进程 `uvicorn`，4 个运行时依赖（`fastapi` / `uvicorn` / `httpx` / `python-multipart`）
- $5/月 Linux VPS 即可跑，systemd 托管
- 客户端用 `Authorization: Bearer <autodl-token>` 把 AutoDL token 直接透传

## 目录

- [快速上手](#快速上手)
- [API 概览](#api-概览)
- [请求示例](#请求示例)
- [配置](#配置)
- [部署](#部署)
- [日志与运维](#日志与运维)
- [架构与目录](#架构与目录)

## 快速上手

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app                           # 监听 0.0.0.0:8765
curl -sS http://localhost:8765/healthz  # {"status":"ok"}
```

需要改端口或上游地址？通过环境变量覆盖（见 [配置](#配置)）。需要热重载：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8765 --reload
```

## API 概览

| 客户端调用 | 转发到 AutoDL |
|---|---|
| `GET  /v1/models` | `POST .../api/v1/comfyui/workflows`（带缓存） |
| `POST /v1/videos` | `POST .../comfyui_workflow/{workflow_id}` |
| `GET  /v1/videos/{id}` | `GET  .../comfyui_workflow/result/{task_id}` |
| `GET  /v1/videos/{id}/content` | 查一次结果，`302` 到首个产物 URL |
| `GET  /v1/workflows/{id}/schema` | 缓存中的 `size` 白名单与必填字段 |
| `GET  /healthz` | 健康检查 |

`{id}` 就是 AutoDL 的 `task_id`，网关不维护任务表，只做转发。

**请求体字段映射**（`POST /v1/videos`）：

| OpenAI 字段 | 上游字段 | 说明 |
|---|---|---|
| `model` | 仅作 URL 路径 | 永不下发 |
| `prompt` | `prompt` | 原样透传 |
| `seconds` | `duration` | 仅当 body 中无 `duration` |
| `size` | `resolution` | `"WxH"` → 最近的白名单标签（如 `"480x480"` → `"480p(1:1)"`） |
| `metadata` | 合并到顶层 | 同名时顶层键优先 |

随后过三步规范化：白名单过滤（丢掉客户端 UI 字段）、类型强转（`"6"` → `6`）、
缺 `seed` 时随机填充。

**`model` → workflow 映射**：AutoDL 的"工作流"就是 OpenAI 客户端概念里的"模型"，
所以 `model` 字段直接当 `workflow_id` 拼到 URL 路径
（`.../comfyui_workflow/{workflow_id}`），网关本身不做 model→workflow 的语义翻译。
可用列表通过 `GET /v1/models` 拉（启动期或首次请求时从 AutoDL 缓存）。

**`size` → `resolution` 映射**：OpenAI 给的是像素 `WxH`（如 `"480x480"` / `"720x1280"`），
AutoDL 要的是**人类可读标签**（如 `"480p(1:1)"` / `"736p竖"`），标签来自工作流 schema 缓存。
网关先按宽高比判断方向（square / portrait / landscape），再在缓存中取同方向
且像素最接近的一档下发；找不到同方向标签才回退到最近的总像素档。

**状态映射**：`QUEUED→queued` / `RUNNING→in_progress` / `SUCCESS→completed` / `FAILED→failed`。

**鉴权**：客户端把 AutoDL ComfyUI token 放在 `Authorization: Bearer <token>`，
网关剥掉 `Bearer` 后原样转发，自身不存任何 key。缺失或畸形 → `401`。

## 请求示例

任何 OpenAI 风格的客户端都能用，base URL 指向你的网关，公网地址形如
`http://<your-server-ip>:8765/v1`（IP 是部署时拿到的，**不要**用本仓库里的占位值）：

```python
# Python — openai SDK
from openai import OpenAI
client = OpenAI(
    base_url="http://<your-server-ip>:8765/v1",
    api_key="<your-autodl-comfyui-token>",   # 透传给 AutoDL
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

拿到 `id` 后轮询 `GET /v1/videos/{id}`；完成时调 `GET /v1/videos/{id}/content`
直接拿到产物 URL。

## 配置

全部环境变量，都有默认值，**没有必填项**：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `GATEWAY_HOST` | `0.0.0.0` | uvicorn 监听地址 |
| `GATEWAY_PORT` | `8765` | uvicorn 监听端口 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `AUTODL_BASE_URL` | `https://www.autodl.art/api/v1/comfyui` | 上游基地址 |
| `AUTODL_BOOTSTRAP_TOKEN` | 未设置 | 启动期预热缓存的 token |
| `SKIP_STARTUP_SYNC` | `0` | `1` 跳过启动预热（首次请求懒加载） |
| `TIMEOUT_CONNECT` / `READ` / `WRITE` / `POOL` | `5.0` / `30.0` / `10.0` / `5.0` | httpx 超时（秒） |
| `TIMEOUT_SUBMIT` | `60.0` | 提交任务专用超时（含图） |
| `MAX_CONNECTIONS` / `MAX_KEEPALIVE` | `100` / `20` | 连接池 |
| `KEEPALIVE_EXPIRY` | `30.0` | 空闲连接 TTL |
| `CORS_ORIGINS` | `*` | 允许的来源，逗号分隔 |
| `LOG_FILE` | `logs/gateway.log` | 滚动日志；空串=只走 stderr |
| `LOG_MAX_BYTES` / `LOG_BACKUP_COUNT` | `10485760` / `5` | 滚动策略 |

工作流 schema 缓存在内存中，启动期（设了 `AUTODL_BOOTSTRAP_TOKEN` 时）或首次请求时
单飞同步一次。**不落盘、不后台刷新**——重启即重拉。

## 部署

### 推荐：systemd（生产 VPS）

```bash
git clone <repo> autodlart2openai
cd autodlart2openai
bash deploy/install.sh                     # 默认监听 0.0.0.0:8765
```

安装器会：rsync 到 `/opt/autodl-openai-gateway` → 建 `.venv` 装依赖 →
写 `/etc/autodl-openai-gateway.env` → 装 systemd unit（用 `EnvironmentFile=` 读 env 文件）
→ `enable` + `start` → 命中 `/healthz` 验证。

需要改端口、上游地址、CORS 等，**安装时**直接传：

```bash
GATEWAY_PORT=9000 \
AUTODL_BASE_URL=https://www.autodl.art/api/v1/comfyui \
AUTODL_BOOTSTRAP_TOKEN=eyJhbGciOi... \
CORS_ORIGINS="https://my-frontend.example.com" \
bash deploy/install.sh
```

**已运行后**想改一个值，编辑 env 文件再重启：

```bash
sudo $EDITOR /etc/autodl-openai-gateway.env
sudo systemctl restart autodl-openai-gateway
curl -sS http://127.0.0.1:9000/healthz
```

> **端口 < 1024**：本 unit 不带 `CAP_NET_BIND_SERVICE`，不要把 `GATEWAY_PORT`
> 设成 80/443；要么保持在 1024 以上，要么前面挂反向代理。

### 上 VPS 后立刻开放端口

gateway 默认监听 `8765/TCP`。在 VPS 上启动后，必须**同时**在两层放行：

```bash
# Ubuntu / Debian（UFW）
sudo ufw allow 8765/tcp                       # 对全部 IP 开放
sudo ufw allow from 1.2.3.4 to any port 8765  # 只对你的 IP 开放（更安全）
sudo ufw reload

# CentOS / RHEL / Fedora（firewalld）
sudo firewall-cmd --permanent --add-port=8765/tcp
sudo firewall-cmd --reload

# 验证本机端口已起
ss -ltnp | grep ':8765'
curl -sS http://127.0.0.1:8765/healthz        # {"status":"ok"}
```

**云控制台安全组**也要放行——这一步 90% 的"装好连不上"都栽在这里。AWS Lightsail /
DigitalOcean / Vultr / 阿里云 ECS / 腾讯 CVM 等厂商默认拦入站，要在它们的 Web 控制台
给该实例的 security group / 防火墙添一条 inbound TCP 8765 的规则。不放行的话，
VPS 内 `curl 127.0.0.1` 通，外网 `curl <公网 IP>` 永远超时。

### 公网可达

放行端口后，公网 base URL 变成 `http://<your-server-ip>:<port>/v1`
（IP 用 `curl ifconfig.me` 或云控制台拿）。HTTPS / 自有域名时，前面挂
nginx 或 Caddy，Caddy 自动签发+续证书：

```caddyfile
autodl.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

## 日志与运维

每请求一行结构化 JSON，同时写 stderr（systemd 收）和滚动文件 `logs/gateway.log`：

```json
{"ts":1700000000000,"level":"INFO","logger":"autodl-openai-gateway",
 "msg":"access","method":"POST","path":"/v1/videos","status":202,
 "upstream_status":200,"latency_ms":87,"client":"1.2.3.4"}
```

**绝不打**请求体、token、cookie；上游 URL 在错误日志里会被截断。

日常命令：

```bash
sudo systemctl status autodl-openai-gateway
sudo journalctl -fu autodl-openai-gateway
sudo systemctl restart autodl-openai-gateway

sudo systemctl show autodl-openai-gateway -p Environment | tr ' ' '\n' | grep GATEWAY_
ss -ltnp | grep ':8765'                                  # 确认端口
```

## 架构与目录

```
app/
├── main.py              FastAPI app + lifespan + CORS + 中间件
├── settings.py          env 驱动的 Settings dataclass
├── auth.py              Bearer token 解析
├── errors.py            类型化 AppError + 异常处理
├── upstream.py          async AutoDL HTTP 客户端（持有 httpx.AsyncClient）
├── cache.py             工作流 schema 缓存（启动期同步）
├── routes.py            5 个端点 + 白名单 + seed 随机
├── mapping.py           纯函数：body/状态/URL/size→resolution/类型强转
└── logging_config.py    JSON formatter + 滚动文件 + access log
deploy/
├── autodl-openai-gateway.service    systemd unit（含 hardening）
└── install.sh                       一键安装器
tests/                   test_auth / test_cache / test_mapping / test_routes
```

### 这个网关**故意不做**的事

- 不等待结果（提交即返回）
- 不存任务状态，永远问 AutoDL
- 不代理媒体字节，客户端跟 `302`
- 不代理 / 转换参考图
- 不管用户、计费、限流
- 不重试，客户端自己轮询
- 不存服务端密钥——每个请求自带 token

## 致谢

`app/upstream.py` 的递归 `extract_urls` 与 `{code, data}` 信封解包来自
`ComfyUI-AutoDL-API` ComfyUI 节点，去掉了 torch / PIL / async 进度控件等重依赖。