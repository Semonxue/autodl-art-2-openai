# Plan: 极简稳健的 AutoDL → OpenAI Videos 网关

> 当前状态：v0.1 已经实现并通过 11 个 smoke test。新阶段的目标是把现有同步版
> 重构为生产级极简架构，并完成部署侧的所有稳健性细节。**不引入容器化**。

---

## 1. 设计目标

| 维度 | 目标 |
|---|---|
| 进程模型 | 单 worker + asyncio（FastAPI async 路由） |
| 资源占用 | 闲置 < 50 MB RSS，正常负载 < 200 MB |
| 启动时间 | < 1 秒 |
| 故障域 | 单进程崩溃由 systemd 自动拉起，in-flight 请求通过 graceful shutdown 兜底 |
| 依赖数 | 运行时 3 个 (`fastapi` / `uvicorn` / `httpx`)，dev 多 `pytest` 一个 |
| 配置面 | 全部环境变量 + 默认值，零配置文件 |
| 可观测性 | 结构化日志 → systemd journal；`/healthz` 探活；不引入 metrics 后端 |

---

## 2. 与 v0.1 的差异

| 主题 | v0.1（现状） | v0.2（目标） | 理由 |
|---|---|---|---|
| 路由函数 | `def` 同步 | `async def` | 异步 I/O 单 worker 即可吃满并发 |
| httpx 客户端 | 每次请求 `AutoDLClient` 新建 | 进程级 `httpx.AsyncClient`，FastAPI lifespan 托管 | 连接复用、避免 fd 泄漏 |
| 超时 | 单一 `timeout=60.0` | connect/read/write/pool 四档显式设置 | 故障定位更清楚 |
| 鉴权 | 函数内嵌 | 独立 `auth.py` | 容易做单测和替换 |
| 错误响应 | 内联 JSONResponse | 统一 exception handler | 减少重复代码 |
| 日志 | `logging.basicConfig` | 结构化 JSON（`method path status upstream_status latency_ms`） | journal 里好查、好告警 |
| 配置 | 写死 | `Settings` (dataclass) 从 env 读 | 改阈值不改代码 |
| 优雅退出 | uvicorn 默认 | systemd + `TimeoutStopSec=15` 显式约定 | 部署侧有数字 |
| 测试 | 同步 TestClient + MockTransport | 异步 AsyncClient + MockTransport（保留同步版作为对照） | 和生产代码形态一致 |

---

## 3. 目录结构（最终形态）

```
autodlart2openai/
├── PLAN.md                  ← 本文件
├── README.md                ← 客户端用法 + 部署步骤
├── .gitignore
├── requirements.txt
├── requirements-dev.txt     ← pytest, anyio
├── app/
│   ├── __init__.py
│   ├── main.py              ← FastAPI app + lifespan + 路由挂载
│   ├── settings.py          ← env-driven Settings dataclass
│   ├── auth.py              ← Bearer 解析 + 401 抛错
│   ├── upstream.py          ← httpx.AsyncClient 封装 (lifespan)
│   ├── routes.py            ← 4 个端点 handler
│   ├── mapping.py           ← OpenAI ↔ AutoDL 字段映射 (纯函数)
│   ├── errors.py            ← 统一异常 → JSON 响应
│   └── logging_config.py    ← 结构化 JSON formatter
├── scripts/
│   └── smoke.sh             ← 真实 curl 验证脚本 (against running gateway)
├── deploy/
│   ├── autodl-openai-gateway.service   ← systemd unit
│   └── install.sh           ← 一键安装：venv + pip + systemd enable
└── tests/
    ├── conftest.py          ← 共享 MockTransport fixture
    ├── test_auth.py
    ├── test_mapping.py
    ├── test_routes_models.py
    ├── test_routes_videos.py
    └── test_lifespan.py
```

代码总量目标：生产代码 ≤ 400 行（含注释），测试 ≤ 300 行。

---

## 4. 关键设计决策（已对齐）

### 4.1 进程模型
- 单 uvicorn worker，async I/O
- `uvicorn app.main:app --host $GATEWAY_HOST --port $GATEWAY_PORT --workers 1`
- 不上 Gunicorn / 多 worker（纯转发场景无状态、单进程足够，省一层进程管理）

### 4.2 HTTP 客户端生命周期
- `httpx.AsyncClient` 通过 FastAPI `lifespan` 上下文管理
- 配置：
  ```python
  AsyncClient(
      timeout=Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
      limits=Limits(max_connections=100, max_keepalive_connections=20,
                    keepalive_expiry=30.0),
      headers={"Accept": "application/json"},
  )
  ```
- 路由 handler 通过 `request.app.state.http` 拿到
- shutdown 时 `await client.aclose()`

### 4.3 超时
- 全局统一四档，定义在 `settings.py`
- 超时时 `httpx.TimeoutException` → 统一 504 Gateway Timeout
- 上游返回 5xx / `{code != Success}` → 统一 502 Bad Gateway

### 4.4 鉴权
- `Authorization: Bearer <token>` 必须严格
- 解析失败 → 401 + `WWW-Authenticate: Bearer`
- 解出后**直接放进单次 httpx 请求 header**，不存任何地方
- 不接受 scheme-less（"裸 token"也 401）

### 4.5 错误响应
统一 shape：
```json
{ "error": { "message": "...", "type": "<category>", "param": null, "code": null } }
```
分类：`invalid_request_error` / `upstream_error` / `internal_error` / `auth_error`

### 4.6 日志
- 一行 JSON 写到 stderr
- 每条请求一条 access log（method, path, status, upstream_status, latency_ms, client_ip）
- 上游失败单独一条 error log（含 upstream URL + status + body 截断）
- 不打 token（即使摘掉 Bearer，也只打 token 长度供诊断）

### 4.7 配置
```python
@dataclass(frozen=True)
class Settings:
    gateway_host: str = "0.0.0.0"
    gateway_port: int = 8000
    upstream_base_url: str = "https://www.autodl.art/api/v1/comfyui"
    log_level: str = "INFO"
    # timeout seconds
    timeout_connect: float = 5.0
    timeout_read: float = 30.0
    timeout_write: float = 10.0
    timeout_pool: float = 5.0
    # pool
    max_connections: int = 100
    max_keepalive: int = 20
    keepalive_expiry: float = 30.0
```
通过 `os.getenv("GATEWAY_PORT", "8000")` 覆盖。

### 4.8 优雅退出
- `uvicorn` 默认捕获 SIGTERM / SIGINT
- systemd unit：
  ```ini
  [Service]
  ExecStart=...uvicorn app.main:app --workers 1
  Restart=on-failure
  RestartSec=3
  TimeoutStopSec=15
  KillSignal=SIGTERM
  ```
- 进程收到 SIGTERM → uvicorn 停止 accept → drain in-flight → asyncio.close → exit
- 超 15s 强杀

---

## 5. 模块边界（v0.2 重构步骤）

按依赖顺序：

1. **`settings.py`** — `Settings` dataclass + `get_settings()`
2. **`mapping.py`** — `_build_upstream_body()`、`map_status()`、`extract_urls()` 三个纯函数（**无 import 业务模块**）
3. **`auth.py`** — `extract_token(request) -> str` + 自定义 `AuthError` exception
4. **`errors.py`** — `AppError` 基类 + 几个子类 + FastAPI exception handler
5. **`upstream.py`** — `class UpstreamClient`，封装 httpx.AsyncClient，提供 `list_workflows / submit / get_result`
6. **`logging_config.py`** — JSON formatter + `configure_logging(settings)`
7. **`routes.py`** — 4 个 endpoint handler（`request.app.state.upstream` 注入）
8. **`main.py`** — `lifespan` + `app = FastAPI(lifespan=...)` + 路由挂载 + exception handler 注册

每个模块 ≤ 80 行。

---

## 6. 测试策略

| 测试文件 | 覆盖 | 关键断言 |
|---|---|---|
| `test_auth.py` | Bearer 解析 | 正常 / 缺 header / 空 Bearer / 大小写 / 多空格 |
| `test_mapping.py` | 字段映射纯函数 | seconds→duration / size→resolution / metadata 合并 / model 不外泄 / 显式优先 |
| `test_routes_models.py` | `/v1/models` | 列表返回 / 跳过空 uuid / 跳过空 name / 上游 502 传播 |
| `test_routes_videos.py` | `/v1/videos` 三件套 | create 202 / retrieve 200 / content 302 / 404 当无结果 / 502 当上游炸 |
| `test_lifespan.py` | httpx 客户端生命周期 | 启动建连 / 关闭时 aclose 调用 / 配置参数生效 |

测试用具：`httpx.MockTransport` + `httpx.AsyncClient` + `fastapi.testclient.AsyncClient`。

跑测命令：`pytest -q`。

---

## 7. 部署流程（小 VPS）

```bash
# 在 VPS 上一次性执行
git clone <repo> /opt/autodl-openai-gateway
cd /opt/autodl-openai-gateway
bash deploy/install.sh    # 建 venv + pip install + systemctl enable --now

# 验证
curl http://127.0.0.1:8000/healthz
bash scripts/smoke.sh    # 需要真实 token

# 看日志
journalctl -u autodl-openai-gateway -f
```

`deploy/install.sh` 内容：
```bash
#!/usr/bin/env bash
set -euo pipefail
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt
sudo cp deploy/autodl-openai-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now autodl-openai-gateway
```

无 Nginx / Caddy / Docker / 任何中间件。

---

## 8. 不做的事（明确边界）

- ❌ Docker / docker-compose
- ❌ Nginx / Caddy（v0.2 不包含，HTTPS 留给用户按需）
- ❌ Prometheus / Grafana / OpenTelemetry
- ❌ 任务表 / Redis / SQLite / 任何持久化
- ❌ 限流 / 配额 / 用户体系
- ❌ 缓存（每次都问 AutoDL，结果 URL 是短时的，缓存无意义）
- ❌ 多 worker / Gunicorn
- ❌ 重试（让客户端轮询重试，网关不代重试上游）
- ❌ 鉴权重写 / token 校验

---

## 9. 验收清单（v0.2 Done Definition）

- [ ] `pytest -q` 全部通过
- [ ] `bash scripts/smoke.sh`（用真实 token）返回 4 步全 200/202/302
- [ ] 进程重启：杀 -9 后 systemd 拉起，30s 内恢复 200
- [ ] 优雅退出：`systemctl stop` 后 15s 内退出，无 in-flight 502
- [ ] 内存：idle RSS < 50 MB
- [ ] 启动时间：cold start < 1.5s
- [ ] 依赖：`pip freeze` 只有 fastapi / uvicorn / httpx / 传递依赖
- [ ] `journalctl -u autodl-openai-gateway -f` 输出可读结构化日志
- [ ] 文档：`README.md` + `PLAN.md` 与最终实现一致

---

## 10. 时间线（建议）

| 阶段 | 内容 | 估时 |
|---|---|---|
| 1 | 抽出 `settings.py` / `mapping.py` / `auth.py` / `errors.py` 四个纯模块 + 测试 | 0.5d |
| 2 | 把 `autodl_client.py` 改成 async 版作为 `upstream.py` | 0.3d |
| 3 | 改 `routes.py` 为 async + lifespan 注入 | 0.3d |
| 4 | 结构化日志 + access log 中间件 | 0.2d |
| 5 | 拆目录、补 `__init__.py`、写 `deploy/install.sh` | 0.2d |
| 6 | 全量 pytest 跑过 + scripts/smoke.sh 写完 | 0.3d |

总计 ~ 2 个工作日。

---

## 11. 待你拍板的开放项

1. **systemd unit 文件名**：用 `autodl-openai-gateway.service` 还是更通用的 `autodl-gateway.service`？
2. **listen 端口**：8000 还是 8080 还是 9000？
3. **`/v1/videos/{id}/content` 的命名**：OpenAI 实际没有这个端点（OpenAI 是直接给客户端 URL 字段），保留是为了"客户端懒得拼 URL 时也能下载"。要保留吗？
4. **日志格式**：JSON 一行 vs 人类可读 key=value？JSON 利于机器解析，但 journal 里肉眼略费劲；我可以做一个开发模式用可读、生产模式用 JSON 的开关。
5. **是否要 README 加一段"和 New-API / OneAPI / Bot 接入的截图/配置示例"？**

如果上面没有异议，我按这个计划进入 v0.2 重构阶段。
