# 架构文档：极简稳健的 AutoDL → OpenAI Videos 网关

> 状态：**v0.3 已实现并完成真实联调**（无限画布 / CherryStudio 均能拉模型、
> 提交任务、拿到真实 task_id、轮询结果）。本文件是最终架构记录，不再是计划稿。

---

## 1. 定位

薄协议翻译层，把 AutoDL ComfyUI 工作流 API 伪装成 OpenAI 兼容接口。
网关只做协议翻译和转发，不存储任务、不后台轮询、不管理用户。

```
客户端（OpenAI 形态） ──▶ 本网关 ──▶ AutoDL ComfyUI
```

---

## 2. 最终目录结构

```
autodlart2openai/
├── PLAN.md                  ← 本文件（架构文档）
├── README.md                ← 客户端用法 + 部署步骤
├── .gitignore
├── requirements.txt         ← fastapi / uvicorn / httpx / python-multipart
├── requirements-dev.txt     ← pytest / anyio / pytest-asyncio
├── pytest.ini
├── app/
│   ├── __init__.py
│   ├── __main__.py          ← python -m app 入口
│   ├── main.py              ← FastAPI app + lifespan + CORS + 中间件
│   ├── settings.py          ← env-driven Settings dataclass
│   ├── auth.py              ← Bearer 解析 + 401
│   ├── errors.py            ← 类型化 AppError + exception handler
│   ├── upstream.py          ← httpx.AsyncClient 封装（list/submit/result/schema）
│   ├── cache.py             ← 工作流 schema 缓存（启动同步）
│   ├── mapping.py           ← 纯函数：body 映射 / 状态映射 / URL 提取 / size→resolution / 类型规范化
│   ├── routes.py            ← 5 个端点 handler + 白名单过滤 + seed 自动随机
│   └── logging_config.py    ← JSON formatter + 轮转文件 + access log
├── scripts/
│   └── smoke.sh             ← curl 端到端验证脚本
├── deploy/
│   ├── autodl-openai-gateway.service
│   └── install.sh
└── tests/
    ├── conftest.py
    ├── test_auth.py         (10)
    ├── test_cache.py        (7)
    ├── test_mapping.py      (35)
    └── test_routes.py       (21)
```

测试总数：**73 个用例**，`pytest -q` 全绿。

---

## 3. 端点

| 客户端 | 上游 |
|---|---|
| `GET /v1/models` | `POST .../workflows`（读缓存，不重复打上游） |
| `POST /v1/videos` | `POST .../comfyui_workflow/{workflow_id}` |
| `GET /v1/videos/{id}` | `GET .../comfyui_workflow/result/{task_id}` |
| `GET /v1/videos/{id}/content` | 查一次 result，302 到首个结果 URL |
| `GET /v1/workflows/{id}/schema` | 读缓存，返回合法 resolution + 必填字段 |
| `GET /healthz` | 探活 |

任务 `id` 直接复用 AutoDL 的 `task_id`，网关无任务表。

---

## 4. 请求体处理管线（POST /v1/videos）

请求到达后按顺序执行：

1. **鉴权**：`Authorization: Bearer <token>` → 剥离 `Bearer` → 原样转发，不存储
2. **body 解析**：优先 JSON，fallback 到 multipart/form-urlencoded（浏览器画布客户端用）
3. **字段映射**（`build_upstream_body`）：
   - `model` → 路径，不上游
   - `prompt` → 透传
   - `seconds` → `duration`（若 body 无 `duration`）
   - `size` → `resolution`（若 body 无 `resolution`）
   - `metadata` → 合并进顶层
   - 其余透传
4. **size → resolution 映射**：`"480x480"`（OpenAI WxH）→ 按比例 + 短边像素，在缓存白名单里挑最接近的 label（如 `"480p(1:1)"`、`"736p竖"`）。档位从白名单动态解析，不硬编码。
5. **白名单过滤**：只保留工作流 `input_rules` 声明过的字段，客户端 UI 字段（`preset`/`resolution_name`/`shotType`/`watermark`…）全部丢弃。
6. **类型规范化**：按 schema 的字段类型，把 `integer`/`float` 字段从字符串强转成数字（`"6"` → `6`）。
7. **seed 自动随机**：工作流声明了 `seed` 但客户端未传时，注入随机整数（`1..999999999999999`），避免固定默认 seed 导致同 prompt 同结果。
8. **提交**：`timeout_submit`（默认 60s，图生任务上传参考图需要更长）。

---

## 5. 状态映射

| AutoDL | OpenAI |
|---|---|
| `QUEUED` | `queued` |
| `RUNNING` | `in_progress` |
| `SUCCESS` | `completed` |
| `FAILED`/`ERROR`/`CANCELLED` | `failed` |

---

## 6. 工作流 schema 缓存

- 启动时（有 `AUTODL_BOOTSTRAP_TOKEN`）或首次请求（懒同步，asyncio.Lock 单飞）拉取全部工作流 schema
- 每个 workflow 缓存：`name`、`resolution_labels`、`required_fields`、`input_fields`、`field_types`
- 只读、无文件持久化、无后台刷新；重启刷新

---

## 7. 设计决策

| 主题 | 决策 |
|---|---|
| 进程模型 | 单 uvicorn worker + asyncio |
| HTTP 客户端 | 进程级 `httpx.AsyncClient`，lifespan 托管，`trust_env=False`（绝不继承宿主代理） |
| 超时 | connect 5s / read 30s / **submit 60s** / write 10s / pool 5s |
| CORS | 默认 `*`，`allow_credentials=False`（Bearer 头鉴权，非 cookie） |
| 错误 | 统一 `{error:{message,type,param,code}}`，分类 invalid_request / upstream / internal |
| 日志 | JSON 一行，双写 stderr + 轮转文件 `logs/gateway.log`（10MB × 5） |
| 优雅退出 | uvicorn SIGTERM + systemd `TimeoutStopSec=15` |

---

## 8. 运行时依赖（4 个）

```
fastapi>=0.110
uvicorn[standard]>=0.27
httpx>=0.27
python-multipart>=0.0.9   # form/multipart 解析（浏览器画布客户端需要）
```

dev 依赖：`pytest`、`anyio`、`pytest-asyncio`。

---

## 9. 部署（小 VPS）

```bash
git clone https://github.com/Semonxue/autodl-art-2-openai.git /opt/autodl-openai-gateway
cd /opt/autodl-openai-gateway
bash deploy/install.sh   # venv + pip + systemd enable --now
```

systemd unit 带 hardening（`NoNewPrivileges`、`PrivateTmp`、`ProtectSystem=full`）。

---

## 10. 不做的事（明确边界）

- ❌ 等到生成完成 / 后台轮询
- ❌ 任务表 / Redis / 数据库 / 队列
- ❌ 中转或转存图片视频（结果走 302）
- ❌ 用户体系 / 计费 / 限流
- ❌ Docker / Nginx / Caddy（用户按需自加）
- ❌ 重试（客户端轮询重试）
- ❌ 鉴权重写 / token 校验 / 存储

---

## 11. 真实联调结果（已完成）

| 客户端 | 结果 |
|---|---|
| CherryStudio | ✅ 拉模型、提交、轮询 |
| 无限画布（OrbStack 容器内 Go 后端 + 浏览器前端） | ✅ 拉模型（浏览器直连）、提交成功拿到真实 task_id、自动轮询 |

联调中踩掉的问题（均已修复并测试覆盖）：

1. 容器内 `127.0.0.1` 不通宿主机 → base_url 需用 `192.168.1.29` 或 `host.docker.internal`
2. 浏览器跨域被 CORS 拦截 → 加 CORS 中间件
3. 画布发 multipart 非 JSON → 加 form 解析 + `python-multipart`
4. 客户端 UI 字段透传导致 AutoDL"未定义参数" → 白名单过滤
5. `duration` 字符串导致 AutoDL"参数值非法" → 类型规范化
6. size `480x480` 对不上 label → 动态档位映射
