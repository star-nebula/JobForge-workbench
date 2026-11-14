"""真实 HTTP 冒烟：起真进程 → 打健康检查 → 发真实写请求 → 独立读库断言落库。

和仓库里 223 条 pytest 的分工（不要混为一谈）：
  tests/ 直接调 route 函数、DB 全 mock，能锁逻辑，但证明不了 uvicorn 起得来、
  路由真的挂上、中间件/异常兜底没把请求吃掉、SQLite 真写进去、页面真能返回。
  本检查全程走 socket + HTTP，断言只读磁盘上的临时 SQLite，不拿 HTTP 响应体
  去证明「写成功了」——否则「接口返回 ok、库里没写」这种坏法照样能绿。

断言的可观察点都是真实副作用：
  - /health/live、/health/ready 的 200 + body/头契约
  - PUT /api/profile 之后，另开 sqlite3 连接读 profile 表确有这次提交的行
  - POST /api/profile/versions 之后，resume_versions 表真的多一行且名字、内容对得上
  - PATCH /api/jobs/... 之后，seen_jobs 的 status 真的被改；非法值必须被拒且不动库
  - GET / 返回的确实是 web/job-workbench.html 的正文
  - 非法查询参数走 422 包络，且不把内部信息带回

任何一步失败都抛 AssertionError，消息里带「期望 / 实际」，供 verify.py 点名。
"""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

from verify_checks import CheckInconclusive

NAME = "http/smoke"
DESCRIPTION = "真进程 uvicorn：health live/ready 200、真实写请求落库到临时 SQLite、页面 200"

BOOT_TIMEOUT = 45.0
SHIM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_server_shim.py")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INDEX_TITLE = "<title>求职工作台 · JobForge</title>"


def _free_port() -> int:
    """让内核挑一个空闲端口，避免和用户正在跑的 8080/8090 撞车。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(url: str, method: str = "GET", payload=None, timeout: float = 15.0):
    """发一次真实 HTTP 请求，返回 (status, headers, raw_text)。4xx/5xx 不抛。"""
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8", "replace")


def _header(headers, name: str) -> str:
    """大小写无关取响应头：HTTP/1.1 线上头名一律小写，uvicorn 也按小写回。"""
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return ""


def _json(text: str, where: str):
    try:
        return json.loads(text)
    except ValueError as exc:
        raise AssertionError(f"{where} 返回的不是 JSON（{exc}）：{text[:200]!r}")


def _wait_until_live(port: int, proc: subprocess.Popen, deadline: float) -> None:
    """等端口真正应答 /health/live。

    进程先死就把它的输出带出来，别干等到超时。按死因分流：
      - 缺依赖 / 端口被占 → CheckInconclusive（环境问题，不是产品坏了）
      - 其它崩溃        → AssertionError（产品起不来就是坏了）
    两者都不算通过，区别只在「该修环境还是该修代码」。
    """
    last = ""
    url = f"http://127.0.0.1:{port}/health/live"
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else b""
            text = out.decode("utf-8", "replace")
            env_errors = ("ModuleNotFoundError", "Address already in use",
                          "No module named", "ImportError")
            if any(marker in text for marker in env_errors):
                raise CheckInconclusive(
                    f"服务进程因环境原因起不来（退出码 {proc.returncode}）：\n{text[-1500:]}")
            raise AssertionError(
                f"服务进程启动后立刻退出（退出码 {proc.returncode}）。输出：\n{text[-2000:]}")
        try:
            status, _headers, _text = _request(url, timeout=1.0)
            if status == 200:
                return
            last = f"HTTP {status}"
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.2)
    raise CheckInconclusive(
        f"/health/live 在 {BOOT_TIMEOUT:.0f}s 内没有就绪（最后一次：{last}），"
        f"端口 {port} 可能被占用或机器过慢")


def _db_rows(db_path: str, sql: str, params=()):
    """独立连接读临时库——不看 HTTP 返回体，只看磁盘上真写了什么。"""
    if not os.path.exists(db_path):
        raise AssertionError(f"库文件不存在：{db_path}")
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def run(ctx) -> None:
    case_dir = str(ctx["case_dir"])
    db_path = os.path.join(case_dir, "verify_jobs.db")
    log = ctx.get("log") or (lambda _m: None)
    port = _free_port()
    base = f"http://127.0.0.1:{port}"

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = os.path.join(PROJECT_ROOT, "src")
    env["JOBFORGE_ENV"] = "verify"
    env["JOBFORGE_LOG_FORMAT"] = "text"
    env["JOBFORGE_LOG_LEVEL"] = "WARNING"
    env.pop("JOBFORGE_API_KEY", None)          # 本机冒烟不开鉴权；鉴权另有单测覆盖
    env["JOBFORGE_GATE_FILE"] = os.path.join(case_dir, "fetch_gate.json")

    log(f"起真进程：{os.path.basename(sys.executable)} _server_shim.py --port {port}")
    proc = subprocess.Popen(
        [sys.executable, SHIM, "--port", str(port), "--db", db_path],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    failure = None
    try:
        _wait_until_live(port, proc, time.time() + BOOT_TIMEOUT)
        log("服务已就绪，开始发真实请求")
        _check_health(base, log)
        profile = _check_profile_write(base, db_path, log)
        _check_version_snapshot(base, db_path, profile, log)
        _check_job_patch(base, db_path, log)
        _check_page(base, log)
        _check_error_envelope(base, log)
        _check_identity(base, port, log)
        assert proc.poll() is None, f"服务进程中途退出，退出码 {proc.returncode}"
    except BaseException as exc:      # 记下首个失败，读完服务日志再抛，避免掩盖原始报错
        failure = exc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        out = proc.stdout.read() if proc.stdout else b""
        server_log = out.decode("utf-8", "replace")

    if failure is not None:
        raise failure
    if "Traceback" in server_log:
        raise AssertionError(f"服务端日志出现 Traceback（真进程里崩过）：\n{server_log[-2000:]}")


# ---------------------------------------------------------------- 各段检查

def _check_health(base: str, log) -> None:
    status, headers, text = _request(f"{base}/health/live")
    assert status == 200, f"/health/live 期望 200，实际 {status}：{text[:300]}"
    body = _json(text, "/health/live")
    assert body.get("status") == "ok", f"/health/live status 期望 ok，实际 {body.get('status')!r}"
    request_id = _header(headers, "X-Request-ID")
    assert request_id, "/health/live 缺 X-Request-ID 响应头"
    assert body.get("request_id") == request_id, (
        f"body.request_id ({body.get('request_id')!r}) 与响应头 ({request_id!r}) 不一致")
    log(f"health/live ok version={body.get('version')} request_id={request_id[:8]}…")

    status, _h, text = _request(f"{base}/health/ready")
    assert status == 200, f"/health/ready 期望 200，实际 {status}：{text[:300]}"
    body = _json(text, "/health/ready")
    assert body.get("status") == "ready", f"/health/ready status 期望 ready，实际 {body.get('status')!r}"
    db_check = (body.get("checks") or {}).get("database") or {}
    assert db_check.get("status") == "up", f"ready 里 database 期望 up，实际 {db_check!r}"
    log("health/ready ok database=up")


def _check_profile_write(base: str, db_path: str, log) -> dict:
    """真实 POST 走完整链路：写库 → 读回 → 评分。断言以磁盘上的行为准。"""
    token = uuid.uuid4().hex[:8]
    profile = {
        "name": f"验证用户{token}",
        "phone": "13800138000",
        "email": f"verify-{token}@example.com",
        "target_position": "后端工程师",
        "city": "深圳",
        "expected_salary": "30-40K",
        "skills": ["Python", "FastAPI", "SQLite", "Docker", "Redis", "Linux"],
        "summary": "验证脚本写入的个人简介，长度超过三十字以便评分引擎计入有效简介分项。",
        "experience": "负责订单系统重构，接口延迟降低 40%，QPS 提升 3 倍，节省机器成本 20 万。",
        "education": "某大学 计算机科学与技术 本科",
        "resume_text": "验证用简历原文：后端工程师，熟悉 Python 与 FastAPI。",
    }
    status, _h, text = _request(f"{base}/api/profile", "PUT", {"data": profile})
    assert status == 200, f"PUT /api/profile 期望 200，实际 {status}：{text[:400]}"
    body = _json(text, "PUT /api/profile")
    assert body.get("ok") is True, f"PUT /api/profile 期望 ok=true，实际 {body!r}"
    updated_at = body.get("updated_at")
    assert isinstance(updated_at, int) and updated_at > 0, (
        f"PUT /api/profile 返回的 updated_at 非法：{updated_at!r}")

    rows = _db_rows(db_path, "SELECT data, updated_at FROM profile WHERE id=1")
    assert len(rows) == 1, f"profile 表期望 1 行，实际 {len(rows)} 行（HTTP 说成功但库里没有）"
    stored = json.loads(rows[0]["data"])
    assert stored.get("name") == profile["name"], (
        f"库里 name 与提交不一致：{stored.get('name')!r} != {profile['name']!r}")
    assert stored.get("skills") == profile["skills"], "库里 skills 与提交不一致"
    assert rows[0]["updated_at"] == updated_at, (
        f"库里 updated_at ({rows[0]['updated_at']!r}) 与响应 ({updated_at!r}) 不一致")
    log(f"PUT /api/profile 落库确认：1 行，name={stored['name']}")

    status, _h, text = _request(f"{base}/api/profile")
    assert status == 200, f"GET /api/profile 期望 200，实际 {status}：{text[:300]}"
    body = _json(text, "GET /api/profile")
    assert ((body.get("profile") or {}).get("data") or {}).get("name") == profile["name"], (
        f"GET /api/profile 没读回刚写的资料：{str(body)[:300]}")

    status, _h, text = _request(f"{base}/api/profile/score")
    assert status == 200, f"GET /api/profile/score 期望 200，实际 {status}：{text[:300]}"
    score_body = _json(text, "GET /api/profile/score")
    assert isinstance(score_body.get("score"), int) and score_body["score"] > 0, (
        f"写入完整资料后评分仍为 {score_body.get('score')!r}，读写链路没接上")
    assert len(score_body.get("checks") or []) >= 10, (
        f"评分 checks 期望 ≥10 项，实际 {len(score_body.get('checks') or [])}")
    log(f"profile/score 链通：score={score_body['score']} level={score_body.get('level')}")
    return profile


def _check_version_snapshot(base: str, db_path: str, profile: dict, log) -> None:
    """第二个写接口（INSERT 路径，不是 upsert）：版本快照必须真插入。"""
    token = uuid.uuid4().hex[:8]
    version_name = f"verify-{token}"
    status, _h, text = _request(f"{base}/api/profile/versions", "POST", {"name": version_name})
    assert status == 200, f"POST /api/profile/versions 期望 200，实际 {status}：{text[:400]}"
    body = _json(text, "POST /api/profile/versions")
    version_id = body.get("id")
    assert isinstance(version_id, int), f"POST /api/profile/versions 没返回 id：{body!r}"

    rows = _db_rows(db_path, "SELECT name, data FROM resume_versions WHERE id=?", (version_id,))
    assert len(rows) == 1, f"resume_versions 期望有 id={version_id} 的行，实际 {len(rows)} 行"
    assert rows[0]["name"] == version_name, (
        f"库里版本名 {rows[0]['name']!r} != 提交的 {version_name!r}")
    snap = json.loads(rows[0]["data"])
    assert snap.get("name") == profile["name"], "版本快照里存的不是当前资料"
    log(f"POST /api/profile/versions 落库确认：id={version_id}")


def _check_job_patch(base: str, db_path: str, log) -> None:
    """岗位写路径。存量岗用直接入库的方式造（验证的是 PATCH 这条真实 HTTP 路径）。"""
    token = uuid.uuid4().hex[:8]
    job_id = f"verify-{token}"
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        conn.execute(
            "INSERT INTO seen_jobs (platform, job_id, title, company, city, status,"
            " first_seen_at, last_seen_at) VALUES ('boss', ?, '后端工程师', '验证公司',"
            " '深圳', 'discovered', 1, 1)",
            (job_id,),
        )
        conn.commit()
    finally:
        conn.close()

    status, _h, text = _request(
        f"{base}/api/jobs/boss/{job_id}", "PATCH", {"status": "applied", "notes": "verify"})
    assert status == 200, f"PATCH /api/jobs/boss/{job_id} 期望 200，实际 {status}：{text[:400]}"
    rows = _db_rows(db_path,
                    "SELECT status, notes FROM seen_jobs WHERE platform='boss' AND job_id=?",
                    (job_id,))
    assert len(rows) == 1, f"seen_jobs 期望有 {job_id}，实际 {len(rows)} 行"
    assert rows[0]["status"] == "applied", (
        f"PATCH 后库里 status 期望 applied，实际 {rows[0]['status']!r}")
    assert rows[0]["notes"] == "verify", f"PATCH 后库里 notes 期望 verify，实际 {rows[0]['notes']!r}"

    status, _h, text = _request(
        f"{base}/api/jobs/boss/{job_id}", "PATCH", {"status": "not-a-status"})
    assert status == 404, f"非法 status 期望 404，实际 {status}：{text[:300]}"
    rows = _db_rows(db_path,
                    "SELECT status FROM seen_jobs WHERE platform='boss' AND job_id=?", (job_id,))
    assert rows[0]["status"] == "applied", f"非法 status 不应动库，实际变成 {rows[0]['status']!r}"
    log("PATCH /api/jobs 落库确认：status=applied/notes=verify，非法值被拒且未动库")


def _check_page(base: str, log) -> None:
    """首页必须是真页面的完整正文——被截断、被别的响应顶掉都要能红。"""
    status, _h, text = _request(f"{base}/")
    assert status == 200, f"GET / 期望 200，实际 {status}：{text[:200]}"
    assert INDEX_TITLE in text, f"GET / 返回的不是工作台首页，正文开头：{text[:200]!r}"
    assert text.rstrip().endswith("</html>"), f"GET / 返回的页面不完整，结尾：{text[-200:]!r}"
    assert "/api/stats" in text, "GET / 页面里找不到 /api/stats 调用，疑似返回了占位页"
    log(f"GET / 页面 {len(text)} 字节，title/结尾/接口调用校验通过")


def _check_error_envelope(base: str, log) -> None:
    """坏请求必须按契约返回 422 包络，而不是 200 或裸 500。"""
    status, _h, text = _request(f"{base}/api/jobs?all_cities=definitely-not-bool")
    assert status == 422, f"非法查询参数期望 422，实际 {status}：{text[:300]}"
    body = _json(text, "GET /api/jobs?all_cities=bad")
    code = (body.get("error") or {}).get("code")
    assert code == "validation_error", f"422 包络缺 error.code=validation_error：{str(body)[:300]}"
    assert "definitely-not-bool" not in text, "422 响应把原始非法输入回显了"

    status, _h, text = _request(f"{base}/api/jobs/boss/definitely-missing")
    assert status == 404, f"不存在的岗位期望 404，实际 {status}：{text[:300]}"
    body = _json(text, "GET /api/jobs/boss/missing")
    assert (body.get("error") or {}).get("code") == "not_found", f"404 包络不对：{str(body)[:300]}"
    log("错误包络确认：422 validation_error / 404 not_found")


def _check_identity(base: str, port: int, log) -> None:
    status, _h, text = _request(f"{base}/api/whoami")
    assert status == 200, f"GET /api/whoami 期望 200，实际 {status}：{text[:300]}"
    who = _json(text, "GET /api/whoami")
    assert who.get("app") == "jobforge", f"/api/whoami 自报 app 不是 jobforge：{who!r}"
    assert who.get("port") == port, f"/api/whoami 报的端口 {who.get('port')!r} != 实际 {port}"
    log(f"whoami 校验通过 pid={who.get('pid')} port={who.get('port')}")
