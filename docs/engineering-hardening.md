# JobForge 服务化增强与验证

> 实施日期：2026-11-04。本文只记录本次已实现、已运行和有证据的部分；没有真实调用 BOSS、外部模型或计费接口。

## 1. 现状判断与取舍

JobForge 仍是单用户、本机优先的求职工作台：FastAPI 服务、SQLite WAL、后台线程、
Windows Chrome 键鼠抓取和外部 OpenAI 兼容模型混在同一条本地链路里。本次不改变这条
主线，只增强 API 边界和可运行性。

### 数据层

保留 SQLite，不引入 PostgreSQL/Redis：

- `data/jobs.db` 是当前岗位、资料、消息、设置的唯一持久化事实源；改成 PostgreSQL
  需要迁移 6 张表、JSON-in-column 兼容逻辑和现有备份路径，收益不覆盖风险。
- 服务刻意限制为单实例；SQLite WAL 能满足当前单进程/低并发写入，不适合被包装成
  “高并发生产数据库”。
- 抓取暂停/停止与节流是跨进程文件信号，进程内批量状态也是单实例设计；Redis 不会
  自动解决桌面自动化的单实例约束，反而增加第二个状态源。
- 可替换边界仍是 `src/jobforge/db.py`：所有 SQL 和连接管理集中在这里。未来若需要
  多实例或远程工作流，再抽 repository/unit-of-work，并先迁移 SQLite 数据。

## 2. 已实现

### FastAPI 服务边界

- 使用 lifespan 替代已弃用的 `on_event("startup")`，启动时幂等执行 `db.init_db()`。
- 新增统一请求关联 ID：`X-Request-ID`。只接受 1-64 位安全字符，否则生成 UUID。
- 新增结构化 JSON 访问日志，记录 request ID、方法、路径、状态码和耗时；不记录请求体、
  Query String 或密钥。
- 新增统一错误信封，同时保留旧 `detail` 字段以兼容现有前端：

```json
{
  "detail": "job not found",
  "error": {
    "code": "not_found",
    "message": "job not found",
    "request_id": "..."
  }
}
```

- 参数校验错误使用 `validation_error`，只返回字段位置、类型和安全消息，不回显原始输入。
- 未处理异常返回 `internal_error`，服务端记录堆栈，响应不泄露异常文本或数据库路径。

### 健康检查

| 端点 | 用途 | 行为 |
|---|---|---|
| `GET /health/live` | 进程存活 | 不依赖数据库，始终公开 |
| `GET /health/ready` | 可接流量 | 检查 SQLite 可连接且 `seen_jobs` 表存在 |

启用 API Key 后，两个健康端点仍保持公开，供 Compose/Docker 健康检查和本机探针使用。

### 基础鉴权

- 环境变量 `JOBFORGE_API_KEY` 非空时，所有 `/api/*` 请求需要：
  - `X-API-Key: <key>`，或
  - `Authorization: Bearer <key>`。
- 使用常量时间比较；未授权响应带 `WWW-Authenticate: Bearer`。
- 未配置 Key 时保持原桌面行为（本机回环、无鉴权）。
- 当前浏览器页面、HUD 和内部子进程不会自动注入该请求头，因此正常桌面工作流应保持
  Key 为空。这是已记录的集成边界，不声称“浏览器端鉴权已经完成”。

### 配置管理

| 变量 | 默认值 | 说明 |
|---|---|---|
| `JOBFORGE_ENV` | `local` | 环境标识，进入日志和健康响应 |
| `JOBFORGE_SERVICE_NAME` | `jobforge` | 服务名 |
| `JOBFORGE_HOST` | `127.0.0.1` | 监听地址；容器内显式设为 `0.0.0.0` |
| `JOBFORGE_PORT` | `8080` | 默认端口；`--port` 仍可覆盖命令行启动 |
| `JOBFORGE_API_KEY` | 空 | 非空时保护 `/api/*` |
| `JOBFORGE_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `JOBFORGE_LOG_FORMAT` | `json` | `json` 或 `text` |
| `JOBFORGE_CORS_ORIGINS` | `*` | 逗号分隔的允许来源 |

配置文件集中在 `src/jobforge/settings.py`，不写 `.env` 真值；`.env` 已加入忽略规则。

### 容器化

- `Dockerfile`：Python 3.11 slim、非 root 用户、独立 API 依赖、健康检查。
- Docker 基础镜像固定到 digest，API 直接依赖固定版本，避免构建时静默漂移。
- `compose.yaml`：只暴露到 `127.0.0.1`，使用命名卷 `jobforge-demo-data`，不挂载真实
  `data/`。
- `requirements-api.txt` 排除 `pywinauto` / `pyautogui` 等 Windows 桌面依赖。
- `spider.py` 对原生抓取模块改为懒加载，使 API/控制面能在 Linux 容器内导入；
  真正的原生抓取仍只在 Windows 宿主机运行。

## 3. 真实数据流与边界

```text
Browser / API client
        |
        | HTTP + X-Request-ID (+ optional API key)
        v
FastAPI server
  |-- request middleware / structured log
  |-- /health/live, /health/ready
  |-- routes
        |
        +--> SQLite data/jobs.db
        +--> external OpenAI-compatible LLM (only when configured/called)
        +--> Windows desktop Chrome (host-only native scraping)

Docker Compose
  -> Linux API/control-plane demo
  -> named volume for demo SQLite
  -> no Windows Chrome, no native keyboard/mouse automation
```

容器健康只证明 API 进程和 SQLite schema 可用；它不证明 BOSS 登录态、Windows UI
自动化、抓取节流或外部模型可用。

## 4. 验证证据

实施前基线：

```powershell
venv\Scripts\python.exe -m pytest -q
# 210 passed, 2 warnings
```

服务边界与容器导入测试加入后：

```powershell
venv\Scripts\python.exe -m pytest -q
# 223 passed, 1 warning
```

实际 smoke test 使用独立端口 `18080`、临时 API Key 和 JSON 日志，不触碰 BOSS 或模型：

```powershell
$env:PYTHONPATH="$PWD\src"
$env:JOBFORGE_API_KEY="smoke-test-key"
$env:JOBFORGE_LOG_FORMAT="json"
$env:JOBFORGE_PORT="18080"
venv\Scripts\python.exe -m jobforge.server
```

已验证的响应：

```text
GET /health/live                         -> 200, status=ok
GET /health/ready                        -> 200, database.status=up, engine=sqlite
GET /api/platforms (no key)              -> 401, error.code=unauthorized
GET /api/platforms (X-API-Key)           -> 200, boss platform
GET /api/jobs?all_cities=not-a-bool      -> 422, error.code=validation_error
```

服务日志包含独立 request ID、路径、状态码和耗时。`SIGINT` 停止时 lifespan 正常输出
`service.stopped`。

Compose 配置已验证：

```powershell
docker compose -f compose.yaml config
# exit 0; api service, healthcheck, loopback port binding and named volume parsed

docker compose up --build -d
# image built; container reported healthy

curl.exe http://127.0.0.1:8080/health/ready
# HTTP 200; database.status=up; engine=sqlite

docker compose logs --no-color --tail=20 api
# JSON service/request logs present

docker compose down
# container and network removed; named demo volume retained
```

容器实际运行状态为 `Up (healthy)`，Docker healthcheck 返回 `Status="healthy"`、
`FailingStreak=0`。容器只使用命名卷 `jobforge_jobforge-demo-data`，没有挂载真实
`data/` 目录。

## 5. 已知限制与后续真实环境门槛

1. 浏览器页面和 HUD 尚未接入 API Key；开启 Key 后只能由支持该请求头的 API 客户端访问。
2. Docker 版本只提供 API/控制面演示，不支持 Windows 原生抓取、Chrome 登录态或 HUD。
3. `_batch` 与抓取进度仍是进程内状态，服务重启会丢失“当前进度”；已完成数据在 SQLite。
4. 未引入 TLS、反向代理、限流、审计存储或分布式锁；这些应在真正对外开放前设计。
5. SQLite 不是多写高可用数据库；跨机器、多副本部署前必须做数据迁移和状态设计。
6. 未调用真实 LLM/BOSS，也未验证计费、速率限制、验证码或账号风控。
7. 数据库仍使用启动期增量补列，没有独立 migration 版本表；不可逆 schema 变更继续走
   `tools/` 下的显式迁移脚本。

## 6. 官方依据

- FastAPI lifespan：<https://fastapi.tiangolo.com/advanced/events/>
- FastAPI middleware：<https://fastapi.tiangolo.com/tutorial/middleware/>
- FastAPI exception handlers：<https://fastapi.tiangolo.com/tutorial/handling-errors/>
- FastAPI TestClient：<https://fastapi.tiangolo.com/tutorial/testing/>
- SQLite WAL：<https://www.sqlite.org/wal.html>
- Docker Compose health/startup order：<https://docs.docker.com/compose/how-tos/startup-order/>

当前环境检测到 FastAPI 0.141.1 / Starlette 1.6.0，Starlette TestClient 要求
`httpx2`；因此测试开发依赖显式加入 `httpx2==2.16.0`。API 镜像的直接依赖已固定，
运行镜像不包含测试依赖。
