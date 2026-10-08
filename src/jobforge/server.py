"""
JobForge 求职工作台 - 后端服务
FastAPI + spider.py

主要接口：
  POST /api/resume/extract     从简历文本提取关键词
  POST /api/crawl              按关键词抓取岗位（单页 page）
  POST /api/scrape-from-resume 一条龙：简历解析 → 抓取 → 匹配度排序
  GET  /api/scrape/logs       智能抓取历史（倒序，默认最近 50 条）
  POST /api/refresh-cookies    从 CDP 浏览器重抓 cookie 写 cookies.json
  GET  /api/cookie-status     查 cookies.json 当前状态
  GET  /api/native/status      原生通道就绪检查（Chrome 登录态 + 节流剩余）
  GET  /api/messages           已缓存会话消息（消息中心）
  POST /api/messages/refresh   从 CDP 浏览器监听 BOSS 会话消息并入库
  GET  /api/platforms          支持的平台列表
  GET  /api/profile            读个人资料（无则 null）
  PUT  /api/profile            保存个人资料（JSON 全量覆盖）
  GET  /api/profile/score      个人资料评分 + 优化建议（本地规则引擎）
  GET  /api/profile/versions   简历版本列表（快照，不含资料大字段）
  POST /api/profile/versions   当前资料存为命名版本快照
  POST /api/profile/versions/{id}/apply   用版本覆盖当前资料
  DELETE /api/profile/versions/{id}       删除版本
  GET  /api/llm/configs        AI 模型配置列表（密钥脱敏）
  PUT  /api/llm/configs        保存模型配置（多模型，掩码密钥自动沿用旧值）
  POST /api/llm/test           测试某模型配置连通性
  POST /api/greeting           BOSS 打招呼语生成（LLM，无配置时本地模板降级）
  POST /api/polish             简历润色（LLM，只改表达不添事实）
  POST /api/match-analysis     岗位匹配分析（LLM，结果缓存 llm_analysis）
  POST /api/triage             L1 智能粗筛：结构化字段批量判 keep/drop（只花 token，不抓 JD）
  POST /api/jobs/{platform}/{job_id}/jd-confirm   人工确认 JD 完整（疑似残缺 → 已获取）
  POST /api/analyze-batch      批量分析：抓 JD + 分析全部缺分析岗位（后台线程）
  GET  /api/analyze-batch/status / POST .../stop   批量进度查询 / 停止
  POST /api/pipeline           一键流水线：抓取 → 粗筛 → JD 精配（限额，后台线程）
  GET  /api/scrape-progress    悬浮窗轮询：当前抓取任务进度（含暂停/停止态）
  POST /api/scrape-control     悬浮窗按钮：pause / resume / stop
  POST /api/hud/launch         手动打开悬浮进度窗（设置页开关）
  GET  /                       静态页面入口
"""
import io
import json
import os
import re
import requests
import subprocess
import sys
import threading
import time
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional, Tuple

if __package__ in (None, ""):   # 直接跑脚本（python src/jobforge/server.py）时补齐包路径
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobforge import db, fetch_gate, llm, paths, profile_score, spider
from jobforge.fetch_jd import clean_jd

# 抓取/HUD 子进程按 `-m jobforge.*` 拉起，需要 src 在 PYTHONPATH 里（run.bat 已设，这里兜底）
_PYTHONPATH = os.environ.get("PYTHONPATH", "")
if paths.SRC_DIR not in _PYTHONPATH.split(os.pathsep):
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [p for p in [paths.SRC_DIR] + _PYTHONPATH.split(os.pathsep) if p])

PROJECT_DIR = paths.PROJECT_ROOT      # 子进程 cwd：抓取要在项目根起
COOKIES_FILE = paths.data("cookies.json")
GRAB_MODULE = "jobforge.tools.grab_cookies"
HUD_MODULE = "jobforge.tools.hud"
MESSAGES_MODULE = "jobforge.tools.messages"
FETCH_JD_MODULE = "jobforge.fetch_jd"
NATIVE_MODULE = "jobforge.fetch_jd_native"

app = FastAPI(title="JobForge 求职工作台 API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _on_startup():
    """启动时初始化 SQLite 库。"""
    db.init_db()


# ---------- 请求模型 ----------
class ResumeExtractReq(BaseModel):
    resume_text: str = Field(..., min_length=1, description="简历纯文本")


class JdConfirmReq(BaseModel):
    confirmed: bool = Field(True, description="true=确认 JD 完整（移出疑似残缺）；false=撤销确认")


class CrawlReq(BaseModel):
    platform: str = Field("all", description="boss | all（目前只支持 BOSS，all 等同 boss）")
    query: str = Field(..., min_length=1, description="搜索关键词")
    city: Optional[str] = Field(None, description="城市名；缺省跟随个人资料的期望城市，「全国」=不限城市")
    page: int = Field(1, ge=1, description="页码（单页，不做区间）")
    use_mock: bool = Field(False, description="已废弃，保留为兼容签名")


class ScrapeFromResumeReq(BaseModel):
    resume_text: str = Field(..., min_length=1, description="简历纯文本")
    query: Optional[str] = Field(None, description="覆盖自动推导的搜索关键词（A4：抓取词放开自由输入）；留空=从简历推导")
    platform: str = Field("all")
    city: Optional[str] = Field(None, description="覆盖简历中的城市")
    page: int = Field(1, ge=1)
    use_mock: bool = Field(False)


class InterviewReq(BaseModel):
    platform: str = Field("boss")
    job_id: str = Field(..., min_length=1)
    interview_at: Optional[int] = Field(None, description="面试时间（毫秒时间戳），null=清除")
    note: Optional[str] = Field(None, description="备注（地点/轮次/链接等）")


class ProfileReq(BaseModel):
    data: Dict[str, Any] = Field(..., description="个人资料全量 JSON（覆盖保存）")


# ---------- 路由 ----------
@app.get("/api/platforms")
def list_platforms():
    # 2026-09-22 用户决定：只用 BOSS，删 zhaopin/51job
    return {
        "platforms": [
            {"key": "boss", "name": "BOSS 直聘", "enabled": True},
        ]
    }


@app.get("/api/whoami")
def whoami():
    """实例身份。`--restart` 靠它确认「应答这个端口的是不是本项目的实例」，
    才敢去结束那个 PID——端口上跑的可能是完全不相干的程序。"""
    return {"app": "jobforge", "pid": os.getpid(), "port": _listen_port()}


@app.get("/api/cookie-status")
def cookie_status():
    """读 cookies.json 返回 BOSS cookie 状态（数量 / 关键 cookie 是否存在 / 文件 mtime 年龄）。"""
    if not os.path.exists(COOKIES_FILE):
        return {"ok": False, "exists": False, "error": "cookies.json 不存在"}
    try:
        with open(COOKIES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        boss = data.get("boss") or {}
        mtime = os.path.getmtime(COOKIES_FILE)
        age_sec = max(0, int(time.time() - mtime))
        return {
            "ok": True,
            "exists": True,
            "count": len(boss),
            "has_stoken": "__zp_stoken__" in boss,
            "has_wt2": "wt2" in boss,
            "age_sec": age_sec,
            "age_desc": _human_age(age_sec),
        }
    except Exception as e:
        return {"ok": False, "exists": True, "error": f"{type(e).__name__}: {e}"}


def _human_age(sec: int) -> str:
    if sec < 60:
        return f"{sec} 秒前"
    if sec < 3600:
        return f"{sec // 60} 分钟前"
    if sec < 86400:
        return f"{sec // 3600} 小时前"
    return f"{sec // 86400} 天前"


# ---------- LLM 模型配置（多模型，密钥存本机 SQLite，接口层脱敏） ----------
def _mask_key(k: str) -> str:
    return ("****" + k[-4:]) if k and len(k) > 4 else ("****" if k else "")


def _get_llm_config(model_id=None) -> Optional[Dict]:
    """取要用的模型配置：指定 model_id 优先，其次设置里的 active，再取第一个。"""
    llm_cfg = (db.get_app_settings() or {}).get("llm") or {}
    configs = llm_cfg.get("configs") or []
    if not configs:
        return None
    if model_id is not None:
        for c in configs:
            if str(c.get("id")) == str(model_id):
                return c
    for c in configs:
        if str(c.get("id")) == str(llm_cfg.get("active_id")):
            return c
    return configs[0]


def _profile_city() -> str:
    """个人资料里的期望城市，未填返回空串。

    这是抓取默认城市与 L0 城市门槛的唯一来源（2026-09-30 用户裁定「抓取默认城市
    跟随 profile.city」）。返回空串而非「全国」，让调用方自己决定兜底顺序。
    """
    pdata = (db.get_profile() or {}).get("data") or {}
    return str(pdata.get("city") or "").strip()


def _split_other_cities(jobs: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """按 L0 城市门槛把岗位分成（期望城市, 非期望城市）；期望城市未填时后者恒空。

    门槛零成本、不进评分：异地岗留着数据但默认不参与浏览与漏斗。
    """
    exp = _profile_city()
    ok: List[Dict] = []
    other: List[Dict] = []
    for j in jobs:
        (ok if spider.is_same_city(j.get("city") or "", exp) else other).append(j)
    return ok, other


class LLMConfigsReq(BaseModel):
    configs: List[Dict[str, Any]] = Field(..., description="全量模型配置列表")
    active_id: Optional[Any] = Field(None, description="当前使用的配置 id")


@app.get("/api/llm/configs")
def get_llm_configs():
    """模型配置列表（api_key 脱敏为 ****尾4位）。"""
    llm_cfg = (db.get_app_settings() or {}).get("llm") or {}
    configs = []
    for c in (llm_cfg.get("configs") or []):
        c2 = dict(c)
        c2["api_key"] = _mask_key(c.get("api_key"))
        configs.append(c2)
    return {"configs": configs, "active_id": llm_cfg.get("active_id")}


@app.put("/api/llm/configs")
def put_llm_configs(req: LLMConfigsReq):
    """保存模型配置。前端回传的掩码密钥（含 ****）自动沿用旧值，不会覆盖真密钥。"""
    old = {str(c.get("id")): c
           for c in ((db.get_app_settings() or {}).get("llm") or {}).get("configs") or []}
    cleaned = []
    for c in req.configs:
        c = dict(c)
        key = str(c.get("api_key") or "")
        if "****" in key:
            c["api_key"] = (old.get(str(c.get("id"))) or {}).get("api_key") or ""
        if not c.get("id"):
            c["id"] = f"m{int(time.time() * 1000) % 100000000}{len(cleaned)}"
        cleaned.append({k: c.get(k) for k in ("id", "name", "base_url", "api_key", "model")})
    settings = db.get_app_settings() or {}
    settings["llm"] = {"configs": cleaned, "active_id": req.active_id}
    db.save_app_settings(settings)
    return {"ok": True}


class LLMTestReq(BaseModel):
    id: Optional[Any] = Field(None, description="已保存配置的 id（与 config 二选一）")
    config: Optional[Dict[str, Any]] = Field(None, description="未保存的完整配置（表单直测）")


@app.post("/api/llm/test")
def test_llm_config(req: LLMTestReq):
    """测试连通性：表单直测传 config（真密钥），测已保存配置传 id。"""
    if req.config:
        cfg = dict(req.config)
        # 表单里未改动的已存密钥是掩码 → 用库里真密钥替换再测
        if "****" in str(cfg.get("api_key") or "") and req.id is not None:
            cfg["api_key"] = (_get_llm_config(req.id) or {}).get("api_key") or ""
    elif req.id is not None:
        cfg = _get_llm_config(req.id)
        if not cfg:
            raise HTTPException(status_code=404, detail="配置不存在")
    else:
        raise HTTPException(status_code=400, detail="需要 id 或 config")
    t0 = time.time()
    try:
        reply = llm.chat(cfg, [{"role": "user", "content": "只回复四个字：连接成功"}],
                         timeout=30, max_tokens=20)
    except llm.LLMError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "reply": (reply or "").strip()[:50],
            "latency_ms": int((time.time() - t0) * 1000), "model": cfg.get("model")}


# ---------- CDP Chrome 自动启动 ----------
# 独立 user-data-dir（不碰日常浏览器；profile 已迁入 data/chrome-profile，含 BOSS 登录态）
CHROME_PROFILE_DIR = paths.data("chrome-profile")
CDP_CHECK_URL = "http://127.0.0.1:9222/json/version"


def _find_chrome():
    """定位 chrome.exe：CHROME_PATH 环境变量 → 注册表 App Paths → 常见安装路径。"""
    candidates = []
    env = os.environ.get("CHROME_PATH")
    if env:
        candidates.append(env)
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
        ) as k:
            candidates.append(winreg.QueryValue(k, None))
    except Exception:
        pass
    candidates += [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None


def _cdp_alive(timeout=1.0):
    """探测 9222 调试端口是否在线（用 127.0.0.1，避免 localhost 解析成 ::1）。"""
    import urllib.request
    try:
        with urllib.request.urlopen(CDP_CHECK_URL, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _is_conn_refused(err: str) -> bool:
    e = err or ""
    return "ECONNREFUSED" in e or ("connect" in e.lower() and "9222" in e)


def _ensure_cdp_chrome(wait_sec=12):
    """9222 不在线时自动启动带调试端口的独立 profile Chrome。
    返回 (是否在线, 提示信息)。独立 user-data-dir 保证：
    ① 不影响日常 Chrome；② 日常 Chrome 开着也能正常拉起新进程。"""
    if _cdp_alive():
        return True, "Chrome 已在线"
    chrome = _find_chrome()
    if not chrome:
        return False, ("未找到 chrome.exe，请设置 CHROME_PATH 环境变量，"
                       "或手动启动：chrome --remote-debugging-port=9222")
    try:
        subprocess.Popen(
            [chrome,
             "--remote-debugging-port=9222",
             f"--user-data-dir={CHROME_PROFILE_DIR}",
             "--no-first-run",
             "--no-default-browser-check",
             "about:blank"],
            cwd=PROJECT_DIR,
        )
    except Exception as e:
        return False, f"自动启动 Chrome 失败: {e}"
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        time.sleep(0.5)
        if _cdp_alive():
            return True, "已自动启动 Chrome（独立调试 profile）"
    return False, "Chrome 已拉起但 9222 端口未就绪"


@app.post("/api/refresh-cookies")
def refresh_cookies():
    """用 venv python 跑 grab_cookies，从 CDP（127.0.0.1:9222）重抓 cookie 写 data/cookies.json。
    若因 Chrome 9222 未启动而失败，自动拉起调试 Chrome 后重试一次。"""
    if not os.path.exists(paths.module_file(GRAB_MODULE)):
        return {"ok": False, "error": "grab_cookies 模块不存在"}

    def _run():
        return subprocess.run(
            [sys.executable, "-m", GRAB_MODULE],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            cwd=PROJECT_DIR,
        )

    try:
        r = _run()
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "").strip()
            if _is_conn_refused(msg):
                ok, note = _ensure_cdp_chrome()
                if ok:
                    r = _run()   # Chrome 拉起后重试一次
                    if r.returncode == 0:
                        return cookie_status()
                    msg = (r.stderr or r.stdout or "").strip()
                return {"ok": False, "error": f"Chrome 9222 不可达：{note}"}
            return {"ok": False, "error": f"grab_cookies 退出 code={r.returncode}: {msg[:300]}"}
        # 读最新 cookies.json
        return cookie_status()
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "重抓超时（15s），CDP 连接可能卡住"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.get("/api/native/status")
def native_status():
    """原生通道就绪检查（Chrome 是否开着 zhipin 页 + 节流剩余秒数）。
    subprocess 跑 fetch_jd_native.py --check-json（不导航、不抢焦点，约 2~5 秒）。"""
    return _native_channel_status()


def _native_channel_status() -> Dict:
    try:
        r = subprocess.run(
            [sys.executable, "-m", NATIVE_MODULE, "--check-json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, cwd=PROJECT_DIR,
        )
        lines = (r.stdout or "").strip().splitlines()
        data = json.loads(lines[-1]) if lines else {}
        return {
            "ok": bool(data.get("ok")),
            "chrome_found": bool(data.get("chrome_found")),
            "chrome_ready": bool(data.get("chrome_ready")),
            "throttle_sec": data.get("throttle_sec") or 0,
            "zhipin_window": data.get("zhipin_window"),
            "error": data.get("error"),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "chrome_found": False, "chrome_ready": False, "throttle_sec": 0, "error": "通道检查超时（60s）"}
    except Exception as e:
        return {"ok": False, "chrome_found": False, "chrome_ready": False, "throttle_sec": 0, "error": f"{type(e).__name__}: {e}"}


@app.post("/api/resume/extract")
def extract_resume(req: ResumeExtractReq):
    keywords = spider.extract_resume_keywords(req.resume_text)
    return {"keywords": keywords}


@app.get("/api/profile")
def get_profile():
    """读个人资料（单行）。无记录返回 {profile: null}。"""
    p = db.get_profile()
    return {"profile": p}


@app.put("/api/profile")
def put_profile(req: ProfileReq):
    """保存个人资料（JSON 全量覆盖）。"""
    updated_at = db.save_profile(req.data)
    return {"ok": True, "updated_at": updated_at}


@app.get("/api/profile/score")
def get_profile_score():
    """个人资料评分 + 优化建议（profile_score 规则引擎，本地计算）。"""
    p = db.get_profile()
    if not p or not isinstance(p.get("data"), dict):
        return {"score": 0, "level": "未填写", "checks": []}
    return profile_score.compute_profile_score(p["data"])


# ---------- 简历版本（一份资料多份简历） ----------
class VersionReq(BaseModel):
    name: str = Field(..., min_length=1, max_length=40, description="版本名")


@app.get("/api/profile/versions")
def get_profile_versions():
    """简历版本列表（不含资料大字段），按创建时间倒序。"""
    return {"versions": db.list_resume_versions()}


@app.post("/api/profile/versions")
def create_profile_version(req: VersionReq):
    """把当前个人资料存为命名版本快照。当前无资料时 404。"""
    p = db.get_profile()
    if not p or not isinstance(p.get("data"), dict):
        raise HTTPException(status_code=404, detail="当前没有个人资料，先填写或采纳后再存版本")
    vid = db.save_resume_version(req.name, p["data"])
    return {"ok": True, "id": vid, "versions": db.list_resume_versions()}


@app.post("/api/profile/versions/{version_id}/apply")
def apply_profile_version(version_id: int):
    """用指定版本覆盖当前个人资料（覆盖前可在前端确认；当前资料可先「新建版本」备份）。"""
    v = db.get_resume_version(version_id)
    if not v:
        raise HTTPException(status_code=404, detail="版本不存在")
    updated_at = db.save_profile(v["data"])
    return {"ok": True, "updated_at": updated_at, "data": v["data"]}


@app.delete("/api/profile/versions/{version_id}")
def remove_profile_version(version_id: int):
    if not db.delete_resume_version(version_id):
        raise HTTPException(status_code=404, detail="版本不存在")
    return {"ok": True, "versions": db.list_resume_versions()}


# ---------- LLM 功能：打招呼语 / 简历润色 / 匹配分析 ----------
class GreetingReq(BaseModel):
    platform: str = Field("boss")
    job_id: Optional[str] = Field(None, description="关联岗位（可选；为空生成通用招呼语）")
    model_id: Optional[Any] = Field(None, description="指定模型配置 id，缺省用设置的当前模型")


@app.post("/api/greeting")
def create_greeting(req: GreetingReq):
    """BOSS 打招呼语：LLM 按岗位 JD 与资料生成；未配置 LLM 时本地模板降级（source=template）。"""
    pdata = (db.get_profile() or {}).get("data") or {}
    job = db.get_job(req.platform, req.job_id) if req.job_id else None
    if req.job_id and not job:
        raise HTTPException(status_code=404, detail="job not found")
    return {"ok": True, **llm.greeting(_get_llm_config(req.model_id), pdata, job)}


class PolishReq(BaseModel):
    summary: Optional[str] = Field(None, description="覆盖资料里的个人简介（不传用已保存值）")
    experience: Optional[str] = Field(None, description="覆盖资料里的工作经历")
    model_id: Optional[Any] = None


@app.post("/api/polish")
def polish(req: PolishReq):
    """润色简历自由文本（summary/experience）：只改表达不添事实，前端 diff 比对后由用户采纳。"""
    cfg = _get_llm_config(req.model_id)
    if not cfg:
        return {"ok": False, "error": "尚未配置 AI 模型，请到「设置 → AI 模型」添加"}
    pdata = (db.get_profile() or {}).get("data") or {}
    if req.summary is not None:
        pdata = {**pdata, "summary": req.summary}
    if req.experience is not None:
        pdata = {**pdata, "experience": req.experience}
    try:
        r = llm.polish_resume(cfg, pdata)
    except llm.LLMError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, **r}


class AnalyzeReq(BaseModel):
    platform: str = Field("boss")
    job_id: str = Field(..., min_length=1)
    refresh: bool = Field(False, description="true=忽略缓存重新分析")
    model_id: Optional[Any] = None


@app.post("/api/match-analysis")
def match_analysis(req: AnalyzeReq):
    """岗位匹配分析（LLM）。结果缓存到 seen_jobs.llm_analysis；需已有 JD 缓存。"""
    job = db.get_job(req.platform, req.job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.get("llm_analysis") and not req.refresh:
        return {"ok": True, "cached": True, "analysis": job["llm_analysis"]}
    cfg = _get_llm_config(req.model_id)
    if not cfg:
        return {"ok": False, "error": "尚未配置 AI 模型，请到「设置 → AI 模型」添加"}
    try:
        analysis = llm.analyze_match(cfg, (db.get_profile() or {}).get("data") or {}, job)
    except llm.LLMError as e:
        return {"ok": False, "error": str(e)}
    db.save_job_analysis(req.platform, req.job_id, analysis)
    return {"ok": True, "cached": False, "analysis": analysis}


# ---------- L1 打包粗筛：用列表页结构化字段批量判 keep/drop（纯 HTTP，不碰键鼠） ----------
class TriageReq(BaseModel):
    model_id: Optional[Any] = None
    limit: Optional[int] = Field(None, ge=1, description="最多粗筛多少个岗位，缺省全部")
    force: bool = Field(False, description="true=已粗筛过的重新筛")
    job_ids: Optional[List[str]] = Field(None, description="只粗筛这些 job_id（小样本验收用）")
    chunk_size: int = Field(llm.TRIAGE_CHUNK, ge=1, le=40, description="每次 LLM 调用带几个岗位")


# B1（2026-10-03）：粗筛全程同步且烧 token——服务端并发锁防连点/防刷新后重复起跑，
# 进度状态暴露给前端轮询（按钮文字实时显示第几批），队列口径抽出来供预览端点复用
_triage_lock = threading.Lock()
_triage_state = {"running": False, "done": 0, "total": 0}


def _triage_queue(req: TriageReq) -> Tuple[List[Dict], int]:
    """粗筛队列唯一口径：job_ids 指定 → L0 城市门槛 → 未筛过滤 → limit 截断。"""
    jobs = db.list_jobs()
    skipped_other_city = 0
    if req.job_ids:
        want = {str(x) for x in req.job_ids}
        jobs = [j for j in jobs if str(j.get("job_id")) in want]
    else:
        jobs, other = _split_other_cities(jobs)      # L0：异地岗不进粗筛队列
        skipped_other_city = len(other)
        if not req.force:
            jobs = [j for j in jobs if j.get("triage_keep") is None]
    if req.limit:
        jobs = jobs[:req.limit]
    return jobs, skipped_other_city


@app.get("/api/triage/preview")
def triage_preview(limit: Optional[int] = None, force: bool = False):
    """粗筛前预览：待筛岗位数与批次估算，供确认框展示（不发 LLM 调用）。"""
    jobs, skipped_other_city = _triage_queue(TriageReq(limit=limit, force=force))
    return {"pending": len(jobs), "chunks": -(-len(jobs) // llm.TRIAGE_CHUNK) if jobs else 0,
            "skipped_other_city": skipped_other_city}


@app.get("/api/triage/status")
def triage_status():
    """粗筛进行中状态（B1）：前端轮询显示第几批；页面刷新后也能据此恢复显示。"""
    return dict(_triage_state)


@app.post("/api/triage")
def start_triage(req: TriageReq):
    """L1 粗筛：按块把岗位的结构化字段交给 LLM 判 keep/drop，为昂贵的 JD 抓取定量。

    与批量精配的分工：本接口只花 token（按块一次带多个岗位），不需要桌面 Chrome、
    不接管键鼠、不吃 BOSS 风控；抓 JD 的额度约束由批量分析侧按 triage_keep 执行。"""
    if not _triage_lock.acquire(blocking=False):
        return {"ok": False, "busy": True,
                "error": "已有粗筛在跑，等当前一轮结束再点（进度见按钮）"}
    try:
        cfg = _get_llm_config(req.model_id)
        if not cfg:
            return {"ok": False, "error": "尚未配置 AI 模型，请到「设置 → AI 模型」添加"}
        try:
            llm.chat(cfg, [{"role": "user", "content": "ping"}], timeout=15, max_tokens=5)
        except llm.LLMError as e:
            return {"ok": False, "error": f"模型配置不可用：{e}"}
        jobs, skipped_other_city = _triage_queue(req)
        if not jobs:
            return {"ok": True, "total": 0, "skipped_other_city": skipped_other_city,
                    "message": "没有需要粗筛的岗位（已筛过的可用 force 重筛）"}
        _triage_state.update(running=True, done=0,
                             total=-(-len(jobs) // req.chunk_size))
        pdata = (db.get_profile() or {}).get("data") or {}
        try:
            results = llm.triage_jobs(cfg, pdata, jobs, chunk_size=req.chunk_size,
                                      progress=lambda d, t: _triage_state.update(done=d, total=t))
        except llm.LLMError as e:
            return {"ok": False, "error": str(e)}
        for r in results:
            db.save_job_triage(r["platform"], r["job_id"], r["keep"], r["reason"])
        kept = sum(1 for r in results if r["keep"])
        return {"ok": True, "total": len(results), "kept": kept,
                "dropped": len(results) - kept,
                "unanswered": sum(1 for r in results if not r["answered"]),
                "skipped_other_city": skipped_other_city,
                "chunks": -(-len(results) // req.chunk_size), "model": cfg.get("model")}
    finally:
        _triage_state["running"] = False
        _triage_lock.release()


# ---------- 批量分析：抓 JD + 逐个 LLM 分析（后台线程，前端轮询进度） ----------
class AnalyzeBatchReq(BaseModel):
    limit: Optional[int] = Field(None, ge=1, description="最多处理多少个岗位，缺省全部")
    force: bool = Field(False, description="true=已分析的也重新分析")
    include_dropped: bool = Field(False, description="true=连粗筛判不匹配的岗位一起分析（人工复核粗筛用，会烧 JD 抓取额度）")
    model_id: Optional[Any] = None


_batch = {"running": False, "stop": False, "total": 0, "done": 0,
          "ok": 0, "failed": 0, "current": "", "errors": [],
          "stage": "", "pipeline": False}
# 批量取消：Event 传给 worker 与 _fetch_jd_core；procs 收集批量期间在跑的子进程，
# stop 端点直接 kill（2026-09-27：标志位只在岗位边界检查 + 子进程最长阻塞 150s，
# 点停止后要等几分钟才生效——用户实测「点了确认不会停」）
_batch_cancel = threading.Event()
_batch_procs = set()
_batch_proc_lock = threading.Lock()


def _analyze_queue(rows: List[Dict], req: AnalyzeBatchReq) -> Tuple[List[Dict], Dict[str, int]]:
    """L2 精配队列：城市门槛（L0）→ 粗筛门禁（L1）→ 去已分析，最后截 limit。

    门禁的理由是成本，不是准确性：LLM 分析本身几乎免费，贵的是每个岗位背后那次原生
    JD 抓取（数十秒 + 节流 + 消耗 BOSS 风控额度，且期间接管键鼠）。「该不该花这次抓取」
    由 L1 粗筛回答，精配只吃 triage_keep=1（2026-09-30 三级漏斗）。
    triage_keep 为 NULL（没筛过）同样拦下——否则新抓的岗位整批绕过门禁，等于没门。

    返回 (队列, 各闸门拦下的数量)；按闸门顺序计数，一个岗只记在被它拦下的那一级。
    """
    jobs, other = _split_other_cities(rows)          # L0：异地岗不进精配队列
    stats = {"skipped_other_city": len(other), "skipped_dropped": 0,
             "skipped_untriaged": 0, "skipped_analyzed": 0}
    if not req.include_dropped:                      # L1：额度只花在粗筛留下的岗上
        kept = []
        for j in jobs:
            keep = j.get("triage_keep")
            if keep == 1:
                kept.append(j)
            elif keep == 0:
                stats["skipped_dropped"] += 1
            else:
                stats["skipped_untriaged"] += 1
        jobs = kept
    if not req.force:
        todo = [j for j in jobs if not j.get("llm_analysis")]
        stats["skipped_analyzed"] = len(jobs) - len(todo)
        jobs = todo
    return (jobs[:req.limit] if req.limit else jobs), stats


@app.post("/api/analyze-batch")
def start_analyze_batch(req: AnalyzeBatchReq):
    """开始批量精配（L2）：每个岗位先确保 JD（无缓存走原生抓取通道），再 LLM 分析。

    队列由 `_analyze_queue` 逐层收窄——只有过了 L0 城市门槛、且 L1 粗筛判 keep 的岗位，
    才会花掉一次 JD 抓取额度。"""
    if _batch["running"]:
        return {"ok": False, "error": "批量分析已在进行中"}
    cfg = _get_llm_config(req.model_id)
    if not cfg:
        return {"ok": False, "error": "尚未配置 AI 模型，请到「设置 → AI 模型」添加"}
    # 预检 1：先 ping 一次模型。批量一跑几十个岗位、每个几十秒，配置不可用必须当场拦下
    try:
        llm.chat(cfg, [{"role": "user", "content": "ping"}], timeout=15, max_tokens=5)
    except llm.LLMError as e:
        return {"ok": False, "error": f"模型配置不可用：{e}"}
    # 预检 2：原生 JD 通道就绪（桌面 Chrome 开着并登录 zhipin.com）。
    # 否则非缓存岗位会逐个走「原生失败→CDP 兜底被反爬→直连失败」全程白烧（2026-09-27 实测教训）
    ns = _native_channel_status()
    if not (ns.get("chrome_found") and ns.get("chrome_ready")):
        return {"ok": False, "error": "原生通道未就绪：" + (ns.get("error") or
                "未找到打开 zhipin.com 的桌面 Chrome。请先打开 Chrome 登录 BOSS 直聘（窗口不要最小化），再开始批量分析")}
    jobs, stats = _analyze_queue(db.list_jobs(), req)
    if not jobs:
        why = []
        if stats["skipped_untriaged"]:
            why.append(f"{stats['skipped_untriaged']} 个尚未粗筛（先在岗位市场跑「智能粗筛」，"
                       f"纯 HTTP 不抓 JD、秒级）")
        if stats["skipped_dropped"]:
            why.append(f"{stats['skipped_dropped']} 个被粗筛判为不匹配")
        if stats["skipped_other_city"]:
            why.append(f"{stats['skipped_other_city']} 个非期望城市")
        if stats["skipped_analyzed"]:
            why.append(f"{stats['skipped_analyzed']} 个已分析过（可强制重分析）")
        return {"ok": True, "started": False, **stats,
                "message": "没有可精配的岗位" + ("：" + "、".join(why) if why else "")}
    _batch.update({"running": True, "stop": False, "total": len(jobs), "done": 0,
                   "ok": 0, "failed": 0, "current": "", "errors": [],
                   "stage": "", "pipeline": False})
    _batch_cancel.clear()
    _ensure_hud()
    _progress_start("批量 AI 分析", "抓取 JD 并分析", total=len(jobs))
    _progress_update(avg_sec=_ANALYZE_AVG_SEC)
    threading.Thread(target=_batch_worker, args=(jobs, cfg, _batch_cancel), daemon=True).start()
    return {"ok": True, "started": True, "total": len(jobs), **stats}


def _batch_run(jobs, cfg, cancel: threading.Event):
    """批量精配循环体（抓 JD + 逐个分析）。手动批量与一键流水线共用：
    前者在线程里跑，后者在流水线线程里同步调。状态写 _batch，进度走 _progress_*。"""
    consecutive_fail = 0
    stopped_by_user = False
    for j in jobs:
        if _batch["stop"] or cancel.is_set():
            stopped_by_user = True
            break
        # 安全点：暂停在这里阻塞（页面/窗口都静止，不注入任何键鼠）；结束后置位则退出
        try:
            fetch_gate.checkpoint()
        except fetch_gate.Stopped:
            stopped_by_user = True
            break
        _batch["current"] = f"{(j.get('title') or '')[:30]} · {(j.get('company') or '')[:16]}"
        _progress_update(current=f"{(j.get('title') or '')[:30]} · {(j.get('company') or '')[:16]}",
                         phase="抓取 JD 正文")
        try:
            jd_res = _fetch_jd_core(j, refresh=False, cancel=cancel)
            if cancel.is_set():
                stopped_by_user = True
                break
            if not jd_res.get("ok"):
                if fetch_gate.stopped():
                    stopped_by_user = True
                    break
                raise llm.LLMError(jd_res.get("error") or "JD 抓取失败")
            try:
                fetch_gate.checkpoint()
            except fetch_gate.Stopped:
                stopped_by_user = True
                break
            _progress_update(phase="AI 匹配分析")
            fresh = db.get_job(j["platform"], j["job_id"]) or j
            analysis = llm.analyze_match(cfg, (db.get_profile() or {}).get("data") or {}, fresh)
            if cancel.is_set():
                stopped_by_user = True
                break
            db.save_job_analysis(j["platform"], j["job_id"], analysis)
            _batch["ok"] += 1
            consecutive_fail = 0
        except Exception as e:
            if cancel.is_set() or fetch_gate.stopped():
                stopped_by_user = True
                break
            _batch["failed"] += 1
            if len(_batch["errors"]) < 20:
                _batch["errors"].append(f"{(j.get('title') or '')[:24]}: {e}"[:160])
            consecutive_fail += 1
            if consecutive_fail >= 3:
                # 连续失败多半是环境问题（Chrome 关了/掉登录/风控），继续烧完只会浪费时间
                # 还加大风控可见面——熔断并说明原因（2026-09-27：Chrome 未开时空烧 14 个的教训）
                _batch["errors"].append(
                    f"已自动停止：连续 {consecutive_fail} 个岗位失败，请检查桌面 Chrome 是否打开并登录 "
                    f"zhipin.com 后重新开始（剩余 {_batch['total'] - _batch['done'] - 1} 个未处理）"[:160])
                break
        _batch["done"] += 1
        _progress_update(done=_batch["done"], ok=_batch["ok"], failed=_batch["failed"],
                         phase="抓取 JD 正文")
    errs = _batch["errors"]
    # 停止位也要看闸门本身：用户在最后一个岗位的分析期间点「结束」时，
    # 循环已结束、stopped_by_user 来不及置位，但结果理应算「已停止」
    return stopped_by_user or fetch_gate.stopped(), (errs[-1] if errs else "")


def _batch_worker(jobs, cfg, cancel: threading.Event):
    """手动批量精配的线程入口：跑循环体 + 收尾进度。"""
    _batch_run(jobs, cfg, cancel)
    _batch["running"] = False
    _batch["current"] = ""
    errs = _batch["errors"]
    _progress_finish(stopped=fetch_gate.stopped(),
                     last_error=errs[-1] if errs else "")


@app.get("/api/analyze-batch/status")
def analyze_batch_status():
    return {**_batch}


@app.post("/api/analyze-batch/stop")
def analyze_batch_stop():
    """停止批量分析：置标志位 + 立即 kill 正在跑的 JD 抓取子进程。
    标志位只在岗位边界检查，若当前岗位卡在子进程里（最长 150s）会等很久才停
    ——用户实测「点确认不会停」即此（2026-09-27）。kill 后 worker 秒级退出。"""
    _batch["stop"] = True
    _batch_cancel.set()
    killed = 0
    with _batch_proc_lock:
        procs = list(_batch_procs)
    for p in procs:
        try:
            p.kill()
            killed += 1
        except Exception:
            pass
    return {"ok": True, "killed_procs": killed, "note": "当前岗位的 JD 抓取已中断，稍候 1~2 秒即停"}


# ---------- 一键流水线：抓取 → 粗筛 → JD 精配（限额） ----------
class PipelineReq(BaseModel):
    query: Optional[str] = Field(None, description="抓取关键词，缺省按简历推导")
    city: Optional[str] = Field(None, description="城市，缺省跟随个人资料的期望城市")
    page: int = Field(1, ge=1, description="抓取页码")
    model_id: Optional[Any] = None
    resume_text: str = Field("", description="端点内部填充，调用方无需传")


_pipeline_lock = threading.Lock()   # 流水线与手动批量互斥（共用 _batch）


def _pipeline_worker(req: PipelineReq, cfg: Dict):
    """流水线主体（2026-10-08 简化，用户裁定）：抓列表 → 本次新岗位逐个抓 JD
    （跳过已有缓存）→ 全部到手后一次性 AI 分析。不再有粗筛与限额——用户裁定
    「抓全部 JD 只是多花时间，无所谓」，以流程简单换 JD 额度。
    停止语义=阶段边界：每段开始前检查闸门，段内停止则该段自然收尾、后续段不再开始。"""
    try:
        _progress_update(stage="抓取", phase="抓取岗位列表")
        scrape_req = ScrapeFromResumeReq(
            resume_text=req.resume_text, query=req.query, city=req.city, page=req.page)
        scrape_res = scrape_from_resume(scrape_req)
        if scrape_res.get("source") == "stopped":
            _batch["errors"].append("抓取阶段被停止，流水线到此结束（已抓岗位已入库）")
            return
        if scrape_res.get("source") != "real":
            _batch["errors"].append(f"抓取失败：{scrape_res.get('error') or '未知错误'}，流水线终止")
            return
        new_jobs = [j for j in (scrape_res.get("jobs") or []) if j.get("job_id")]
        if not new_jobs:
            _batch["errors"].append("本次抓取没有新岗位（可能都已入库），流水线结束")
            return
        if _batch["stop"] or _batch_cancel.is_set() or fetch_gate.stopped():
            return

        # 新岗位逐个抓 JD（_batch_run 的循环体自带：缓存直读/三层降级/连续失败熔断/
        # 每岗 checkpoint——正是「抓 JD 不分析」想要的，直接复用）
        _progress_update(stage="抓JD", phase="逐个抓取 JD 正文", total=len(new_jobs),
                         done=0, ok=0, failed=0, avg_sec=_ANALYZE_AVG_SEC)
        _batch["total"] = len(new_jobs)
        _batch["done"] = 0
        _batch["ok"] = 0
        _batch["failed"] = 0
        # 临时替换分析步：_batch_run 循环体里 JD 抓完即调 analyze_match；
        # 抓 JD 段只想要 JD。不复用循环体，改用精简循环（JD 缓存直读 + 降级都走 _fetch_jd_core）
        for j in new_jobs:
            if _batch["stop"] or _batch_cancel.is_set():
                break
            try:
                fetch_gate.checkpoint()
            except fetch_gate.Stopped:
                break
            _batch["current"] = f"{(j.get('title') or '')[:30]} · {(j.get('company') or '')[:16]}"
            _progress_update(current=_batch["current"], phase="抓取 JD 正文")
            jd_res = _fetch_jd_core(j, refresh=False, cancel=_batch_cancel)
            if jd_res.get("ok"):
                _batch["ok"] += 1
            else:
                if _batch_cancel.is_set() or fetch_gate.stopped():
                    break
                _batch["failed"] += 1
                if len(_batch["errors"]) < 20:
                    _batch["errors"].append(f"{(j.get('title') or '')[:24]}: {jd_res.get('error') or 'JD 抓取失败'}"[:160])
            _batch["done"] += 1
            _progress_update(done=_batch["done"], ok=_batch["ok"], failed=_batch["failed"])

        jd_failed = _batch["failed"]
        if _batch["stop"] or _batch_cancel.is_set() or fetch_gate.stopped():
            _batch["errors"].append(
                f"抓 JD 阶段被停止（成功 {_batch['ok']}，失败 {jd_failed}），已完成部分保留，不进入分析")
            return

        # 一次性 AI 分析：本次新岗位里所有拿到 JD 的（分析几乎免费，全部一起做）
        fresh = [db.get_job("boss", j["job_id"]) or j for j in new_jobs]
        todo = [j for j in fresh if (j.get("jd_text") or "").strip()]
        if not todo:
            _batch["errors"].append("新岗位没有一个 JD 抓取成功，无法分析")
            return
        _progress_update(stage="分析", phase="AI 逐岗匹配分析", total=len(todo),
                         done=0, ok=0, failed=0)
        _batch["total"] = len(todo)
        _batch["done"] = 0
        _batch["ok"] = 0
        _batch["failed"] = 0
        for j in todo:
            if _batch["stop"] or _batch_cancel.is_set():
                break
            _batch["current"] = f"{(j.get('title') or '')[:30]} · {(j.get('company') or '')[:16]}"
            _progress_update(current=_batch["current"], phase="AI 匹配分析")
            try:
                analysis = llm.analyze_match(cfg, (db.get_profile() or {}).get("data") or {}, j)
                db.save_job_analysis(j["platform"], j["job_id"], analysis)
                _batch["ok"] += 1
            except Exception as e:
                _batch["failed"] += 1
                if len(_batch["errors"]) < 20:
                    _batch["errors"].append(f"{(j.get('title') or '')[:24]}: {e}"[:160])
            _batch["done"] += 1
            _progress_update(done=_batch["done"], ok=_batch["ok"], failed=_batch["failed"])
        if jd_failed:
            _batch["errors"].append(f"注意：{jd_failed} 个岗位 JD 抓取失败，已跳过分析")
    except fetch_gate.Stopped as e:
        _batch["errors"].append(f"流水线被停止：{e}")
    except Exception as e:
        _batch["errors"].append(f"流水线异常：{type(e).__name__}: {e}"[:160])
    finally:
        _batch["running"] = False
        _batch["current"] = ""
        _batch["stage"] = ""
        _batch["pipeline"] = False
        _progress_update(stage="", phase="")
        _progress_finish(stopped=fetch_gate.stopped(),
                         last_error=_batch["errors"][-1] if _batch["errors"] else "")


@app.post("/api/pipeline")
def start_pipeline(req: PipelineReq):
    """一键智能抓取（简化版，2026-10-08 用户裁定）：抓列表 → 本次新岗位逐个
    抓 JD → 一次性 AI 分析。不再有粗筛/精配两级与限额——以 JD 额度换流程简单。
    预检在端点内做完（LLM 可用、原生通道就绪、有简历、无冲突任务在跑），
    worker 开后台线程；进度复用 _batch 状态与悬浮窗，停止复用现有端点。"""
    if _batch["running"]:
        return {"ok": False, "error": "已有批量任务/流水线在跑，等它结束再开始"}
    if _pipeline_lock.locked():
        return {"ok": False, "error": "已有流水线在跑"}
    pdata = (db.get_profile() or {}).get("data") or {}
    resume_text = str(pdata.get("resume_text") or "").strip()
    if not resume_text:
        return {"ok": False, "error": "个人资料里没有简历文本：先在「个人资料」粘贴简历并采纳保存"}
    cfg = _get_llm_config(req.model_id)
    if not cfg:
        return {"ok": False, "error": "尚未配置 AI 模型，请到「设置 → AI 模型」添加"}
    try:
        llm.chat(cfg, [{"role": "user", "content": "ping"}], timeout=15, max_tokens=5)
    except llm.LLMError as e:
        return {"ok": False, "error": f"模型配置不可用：{e}"}
    ns = _native_channel_status()
    if not (ns.get("chrome_found") and ns.get("chrome_ready")):
        return {"ok": False, "error": "原生通道未就绪：" + (ns.get("error") or
                "未找到打开 zhipin.com 的桌面 Chrome。请先打开 Chrome 登录 BOSS 直聘（窗口不要最小化）")}
    if not _pipeline_lock.acquire(blocking=False):
        return {"ok": False, "error": "已有流水线在跑"}
    _batch.update({"running": True, "stop": False, "total": 0, "done": 0,
                   "ok": 0, "failed": 0, "current": "", "errors": [],
                   "stage": "抓取", "pipeline": True})
    _batch_cancel.clear()
    _ensure_hud()
    _progress_start("一键流水线", "抓取岗位列表", total=1)
    req.resume_text = resume_text
    threading.Thread(target=_pipeline_worker, args=(req, cfg), daemon=True).start()
    return {"ok": True, "started": True}


# ---------- 抓取进度（悬浮窗数据源）：岗位列表抓取 + 批量分析统一视图 ----------
# 悬浮窗（hud.py，独立进程）轮询 /api/scrape-progress 取进度，按钮打
# /api/scrape-control。暂停/结束信号落 fetch_gate.json（跨进程文件）：
# 列表抓取在 server 线程内，JD 抓取在子进程内，文件是两者都能看到的单一信号源。
_progress = {
    "active": False, "title": "", "phase": "", "current": "",
    "total": 0, "done": 0, "ok": 0, "failed": 0,
    "started_at": 0.0, "detail_until_ts": 0.0, "finished_at": 0.0,
    "stopped": False, "last_error": "", "avg_sec": 0.0,
}
_progress_lock = threading.Lock()
_progress_depth = 0          # 嵌套深度：批量分析期间打开单个 JD 详情，不夺走进度显示
_hud_proc: Optional[subprocess.Popen] = None


def _progress_update(**kw):
    with _progress_lock:
        _progress.update(kw)


def _progress_get() -> Dict:
    with _progress_lock:
        return dict(_progress)


def _progress_start(title: str, phase: str, total: int = 0):
    """任务开始：复位文件闸门（上一次的 stopped 不能影响新任务）+ 进程内计数清零。

    嵌套规则：已有任务在跑时（如批量分析中打开 JD 弹窗），内层不再抢进度显示、
    也不复位闸门——悬浮窗始终展示外层那个「大任务」，内层的抓取就是它的阶段之一。
    """
    global _progress_depth
    _progress_depth += 1
    if _progress_depth > 1:
        return False
    fetch_gate.clear()
    _progress_update(active=True, title=title, phase=phase, current="",
                     total=total, done=0, ok=0, failed=0,
                     started_at=time.time(), detail_until_ts=0.0, finished_at=0.0,
                     stopped=False, last_error="", avg_sec=0.0)
    return True


def _progress_finish(stopped: bool, last_error: str = ""):
    """任务结束：置非活跃 + 记结束时间（悬浮窗停留展示后自动关闭）。
    嵌套时只有最外层结束才真正收尾，内层结束不改动外层进度。"""
    global _progress_depth
    _progress_depth = max(0, _progress_depth - 1)
    if _progress_depth > 0:
        return
    fetch_gate.set_state(paused=False)
    _progress_update(active=False, phase="", current="", finished_at=time.time(),
                     stopped=stopped, last_error=last_error, detail_until_ts=0.0)


class _ProgressScope:
    """抓取任务的进度作用域：enter 起任务，exit 无论如何都收尾（含异常/提前 return），
    避免进度窗卡在「运行中」、嵌套计数泄漏。用法：

        with _progress_scope("智能抓取", "打开搜索页", total=1) as sc:
            sc.set(current="关键词「前端」")
            ...
    """

    def __init__(self, title: str, phase: str, total: int = 0):
        self.title, self.phase, self.total = title, phase, total
        self.outer = False
        self.last_error = ""
        self.stopped = False      # 调用方可显式置位（如 slash 分支提前 return）

    def __enter__(self):
        _ensure_hud()
        self.outer = _progress_start(self.title, self.phase, self.total)
        return self

    def set(self, **kw):
        # 嵌套时内层不夺走外层的显示（比如批量分析里打开单个 JD 弹窗）
        if self.outer:
            _progress_update(**kw)

    def __exit__(self, exc_type, exc, tb):
        if exc is not None and not issubclass(exc_type, fetch_gate.Stopped):
            self.last_error = f"{exc_type.__name__}: {exc}"
        _progress_finish(stopped=self.stopped or fetch_gate.stopped(),
                         last_error=self.last_error)
        return False


def _progress_scope(title: str, phase: str, total: int = 0) -> "_ProgressScope":
    return _ProgressScope(title, phase, total)


def _progress_eta(done: int, started_at: float, avg_sec: float, total: int) -> Optional[float]:
    """剩余时间估算：有历史均值用均值，否则用本次已完成的平均耗时。"""
    if not total or total <= done or not started_at:
        return None
    if avg_sec:
        return round(avg_sec * (total - done), 1)
    elapsed = time.time() - started_at
    if done <= 0 or elapsed <= 0:
        return None
    return round(elapsed / done * (total - done), 1)


# 批量分析每个岗位约 30~50 秒（JD 抓取节流 18~35s + LLM 分析），用于进度条 ETA
_ANALYZE_AVG_SEC = 42.0


@app.get("/api/scrape-progress")
def scrape_progress():
    """悬浮窗轮询：当前抓取进度 + 暂停/停止态。无任务时 active=false。"""
    p = _progress_get()
    p["paused"] = fetch_gate.paused()
    p["stopping"] = bool(p["active"] and fetch_gate.stopped())
    need = _progress_eta(p["done"], p["started_at"], p["avg_sec"], p["total"])
    p["eta_sec"] = need
    return p


class ScrapeControlReq(BaseModel):
    action: str = Field(..., description="pause | resume | stop")


@app.post("/api/scrape-control")
def scrape_control(req: ScrapeControlReq):
    """悬浮窗按钮：暂停 / 继续 / 结束。
    暂停只挡「安全点」（抓取循环的岗位边界与节流等待），不会把正在注入的
    一次键鼠动作撕成两半；结束会同时置停止位并 kill 在跑的抓取子进程。"""
    act = (req.action or "").strip().lower()
    if act == "pause":
        if not fetch_gate.stopped():
            fetch_gate.set_state(paused=True)
        return {"ok": True, "paused": True}
    if act == "resume":
        fetch_gate.set_state(paused=False)
        return {"ok": True, "paused": False}
    if act == "stop":
        fetch_gate.set_state(stopped=True, paused=False)
        _batch["stop"] = True
        _batch_cancel.set()
        killed = 0
        with _batch_proc_lock:
            procs = list(_batch_procs)
        for p in procs:
            try:
                p.kill()
                killed += 1
            except Exception:
                pass
        return {"ok": True, "stopped": True, "killed_procs": killed}
    raise HTTPException(status_code=400, detail="action 必须是 pause / resume / stop")


@app.post("/api/hud/launch")
def launch_hud():
    """手动打开悬浮进度窗（前端开关；抓取时会自动拉起，这里是给用户先看位置用）。"""
    global _hud_proc
    if _hud_proc is not None and _hud_proc.poll() is None:
        return {"ok": True, "already_running": True}
    if not os.path.exists(paths.module_file(HUD_MODULE)):
        return {"ok": False, "error": "hud 模块不存在"}
    try:
        # 继承本实例的闸门文件与服务地址：换端口/换数据目录起实例时，
        # 悬浮窗读写的仍是这个实例的状态，不会串到默认 8080
        env = {**os.environ, "JOBFORGE_GATE_FILE": fetch_gate.gate_path(),
               "JOBFORGE_API": f"http://127.0.0.1:{_listen_port()}"}
        _hud_proc = subprocess.Popen([sys.executable, "-m", HUD_MODULE], cwd=PROJECT_DIR, env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "started": True}


def _listen_port() -> int:
    """本实例实际监听端口（__main__ 启动时按 --port/PORT 写入，缺省 8080）。
    悬浮窗要连对实例：同一台机器可能同时跑着正式实例与调试实例。"""
    return int(os.environ.get("JOBFORGE_PORT") or 8080)


def _ensure_hud():
    """抓取启动时自动拉起悬浮窗（已在运行则复用）。失败静默——进度窗是锦上添花，
    绝不能因为它起不来而挡住抓取。"""
    try:
        launch_hud()
    except Exception:
        pass


@app.post("/api/crawl")
def crawl_jobs(req: CrawlReq):
    """单页抓取（岗位列表）：带进度上报与暂停/结束支持。"""
    city = req.city or _profile_city() or "全国"
    try:
        with _progress_scope("岗位列表抓取", "打开搜索页并读取岗位列表", total=1) as sc:
            sc.set(current=f"关键词「{req.query}」· {city}")
            result = spider.crawl(req.platform, req.query, city, req.page, req.use_mock)
            ok = result.get("source") == "real"
            sc.set(done=1, ok=1 if ok else 0, failed=0 if ok else 1,
                   last_error="" if ok else (result.get("error") or "抓取失败"))
            return result
    except fetch_gate.Stopped as e:
        return {"jobs": [], "source": "stopped", "platform": req.platform, "error": str(e)}


@app.post("/api/resume/upload-pdf")
async def upload_resume_pdf(file: UploadFile = File(...)):
    """上传 PDF 简历，抽文本 + 解析关键词（含 sections 分段）。
    返回 {resume_text, keywords}，前端可直接用 resume_text 调 /api/scrape-from-resume。
    """
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="只支持 PDF 文件")
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="PDF 超过 10MB 限制")
    try:
        import pdfplumber
        text = ""
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                text += (page.extract_text() or "") + "\n"
    except ImportError:
        raise HTTPException(status_code=500, detail="pdfplumber 未安装")
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"PDF 解析失败: {type(e).__name__}: {e}")
    if not text.strip():
        raise HTTPException(status_code=422, detail="PDF 没抽到文本（可能是扫描件）")
    keywords = spider.extract_resume_keywords(text)
    return {"resume_text": text, "keywords": keywords, "char_count": len(text)}


@app.post("/api/scrape-from-resume")
def scrape_from_resume(req: ScrapeFromResumeReq):
    """根据简历自动抓取并按匹配度排序的岗位。
    抓完后 upsert 到 seen_jobs（SQLite 去重缓存），返回的每个岗位带 is_new + score_detail 字段。
    """
    keywords = spider.extract_resume_keywords(req.resume_text)
    # 搜索词：显式指定（自由关键词入口）优先，留空才从简历推导；匹配度评分始终用简历关键词
    query = (req.query or "").strip() \
        or keywords["target_position"] or " ".join(keywords["skills"][:3]) or "前端工程师"
    city = req.city or _profile_city() or keywords["city"] or "全国"

    with _progress_scope("智能抓取", "打开搜索页并读取岗位列表", total=1) as sc:
        sc.set(current=f"关键词「{query}」· {city}")
        try:
            result = spider.crawl(req.platform, query, city, req.page, req.use_mock)
        except fetch_gate.Stopped as e:
            sc.stopped = True
            return {"keywords": keywords, "query_used": query, "city_used": city,
                    "jobs": [], "source": "stopped", "platform": req.platform,
                    "error": str(e), "stats": {"total": 0, "new": 0, "seen": 0}}
        ok = result.get("source") == "real"
        sc.set(done=1, ok=1 if ok else 0, failed=0 if ok else 1,
               last_error="" if ok else (result.get("error") or "抓取失败"))

    # 计算匹配度（P3：4 维 + reasoning）+ 拍平到 job
    for job in result["jobs"]:
        sd = spider.calc_match_score(job, keywords)
        job["match_score"] = sd["overall"]
        job["score_detail"] = sd
    result["jobs"].sort(key=lambda j: j["match_score"], reverse=True)

    # upsert 到 SQLite + 标 is_new
    is_new_flags = db.upsert_jobs(result["jobs"])
    for j, is_new in zip(result["jobs"], is_new_flags):
        j["is_new"] = is_new

    new_count = sum(1 for f in is_new_flags if f)
    # 抓取历史（含失败），供前端「历史记录」与防重复参考
    db.add_scrape_log(req.platform, city, req.page, query, len(result["jobs"]), new_count,
                      result.get("source", ""), "" if result.get("source") == "real" else result.get("error", ""))
    return {
        "keywords": keywords,
        "query_used": query,
        "city_used": city,
        "jobs": result["jobs"],
        "source": result["source"],
        "platform": result["platform"],
        "error": result.get("error"),
        "stats": {"total": len(result["jobs"]), "new": new_count, "seen": len(result["jobs"]) - new_count},
    }


@app.get("/api/scrape/logs")
def get_scrape_logs(limit: int = 50):
    """智能抓取历史，按时间倒序（默认 50 条，上限 200）。"""
    return {"logs": db.list_scrape_logs(min(max(limit, 1), 200))}


@app.get("/api/jobs")
def list_seen_jobs(status: Optional[str] = None, all_cities: bool = False):
    """列出已缓存的岗位。status 为空返回全部，按 last_seen_at 倒序。

    默认只返回期望城市的岗位（L0 硬门槛，2026-09-30 用户裁定）：异地岗是 09-22/09-25
    三批「静默按全国抓」的残留，数据留着但默认不参与浏览与漏斗；all_cities=true 看全部。
    每个岗位带 city_ok 供前端打标。
    """
    jobs = db.list_jobs(status)
    exp = _profile_city()
    for j in jobs:
        j["city_ok"] = spider.is_same_city(j.get("city") or "", exp)
    other_count = sum(1 for j in jobs if not j["city_ok"])
    if not all_cities:
        jobs = [j for j in jobs if j["city_ok"]]
    return {"jobs": jobs, "expected_city": exp, "other_city_count": other_count}


@app.get("/api/jobs/jd-stats")
def get_jd_stats():
    """岗位市场 JD 覆盖统计：已获取 / 未获取 JD 的岗位数（含疑似残缺计数）。"""
    return db.get_jd_stats()


# ---------- 数据备份（C 组）：求职库是不可再生资产，一键落 data/backups/ ----------
_BACKUP_KEEP = 10   # 自动保留的最近份数


def _backup_dir():
    import pathlib
    return pathlib.Path(paths.data("backups"))


@app.get("/api/backup/list")
def list_backups():
    """已有备份清单（新→旧），供设置弹窗展示。"""
    bdir = _backup_dir()
    out = []
    if bdir.exists():
        for d in sorted(bdir.iterdir(), reverse=True):
            if d.is_dir() and d.name.startswith("backup-"):
                size = sum(f.stat().st_size for f in d.iterdir() if f.is_file())
                out.append({"name": d.name,
                            "files": sorted(f.name for f in d.iterdir() if f.is_file()),
                            "size_bytes": size})
    return {"backups": out, "keep": _BACKUP_KEEP}


@app.post("/api/backup")
def create_backup():
    """一键备份：SQLite backup API 热备 jobs.db（WAL 模式下一致性安全）+ 复制 messages.json。

    不备份 cookies.json（登录态凭据，且可随时重抓）；只保留最近 _BACKUP_KEEP 份。"""
    import shutil
    import sqlite3
    bdir = _backup_dir()
    bdir.mkdir(parents=True, exist_ok=True)
    name = time.strftime("backup-%Y%m%d-%H%M%S")
    dest = bdir / name
    if dest.exists():   # 同一秒连按：幂等返回
        return {"ok": True, "name": name, "note": "该时刻备份已存在"}
    dest.mkdir()
    try:
        src = sqlite3.connect(paths.data("jobs.db"))
        dst = sqlite3.connect(str(dest / "jobs.db"))
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
        msg = _backup_dir().parent / "messages.json"
        if msg.exists():
            shutil.copy2(msg, dest / "messages.json")
    except Exception as e:
        shutil.rmtree(dest, ignore_errors=True)   # 失败不留半份坏备份
        return {"ok": False, "error": f"备份失败：{e}"}
    dirs = sorted([d for d in bdir.iterdir()
                   if d.is_dir() and d.name.startswith("backup-")], reverse=True)
    for old in dirs[_BACKUP_KEEP:]:
        shutil.rmtree(old, ignore_errors=True)
    return {"ok": True, "name": name}


@app.get("/api/stats")
def get_stats():
    """看板聚合统计（总数/状态分布/匹配度分布/14 天趋势/Top5/最近动态）。

    口径 = 全库（含异地岗）——看板是求职总览；岗位市场默认只看期望城市，
    两边数字不同是设计使然，前端据此标注「含异地岗」避免同屏数字对不上（A3）。"""
    s = db.get_stats()
    exp = _profile_city()
    s["other_city_count"] = sum(
        1 for j in db.list_jobs() if not spider.is_same_city(j.get("city") or "", exp))
    return s


@app.get("/api/messages")
def get_messages():
    """已缓存的会话消息（消息中心，按消息时间倒序）。

    limit 提到 500 并如实返回 total/truncated——此前 limit=200 静默截断，
    会话超过 200 条时后 100 条悄悄消失，用户无从知晓（C 组体检条）。"""
    msgs = db.list_messages(limit=500)
    total = db.count_messages()
    return {"messages": msgs, "total": total, "truncated": total > len(msgs)}


@app.post("/api/messages/refresh")
def refresh_messages():
    """跑 messages 模块从 CDP 浏览器监听 BOSS 聊天页会话响应，入库后返回最新列表。
    若因 Chrome 9222 未启动而失败，自动拉起调试 Chrome 后重试一次。"""
    if not os.path.exists(paths.module_file(MESSAGES_MODULE)):
        return {"ok": False, "error": "messages 模块不存在"}

    def _run():
        r = subprocess.run(
            [sys.executable, "-m", MESSAGES_MODULE],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45,
            cwd=PROJECT_DIR,
        )
        # 脚本崩溃与否都尝试读 messages.json（业务失败信息在文件里）
        out = paths.data("messages.json")
        data = {}
        if os.path.exists(out):
            try:
                with open(out, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        return data, r

    try:
        data, r = _run()
        launched_note = ""
        if not data.get("ok") and _is_conn_refused(data.get("error") or
                                                   (r.stderr or "") + (r.stdout or "")):
            # Chrome 9222 未启动：自动拉起后重试一次
            ok, note = _ensure_cdp_chrome()
            launched_note = note
            if ok:
                data, r = _run()
        if not data.get("ok"):
            err = data.get("error") or (r.stderr or r.stdout or "").strip()[:300] or "未知错误"
            if _is_conn_refused(err):
                return {"ok": False, "error": f"Chrome 9222 不可达：{launched_note}"}
            if launched_note:
                # Chrome 刚被拉起（首次使用是全新 profile），多半还没登录 BOSS
                return {
                    "ok": False,
                    "error": f"{err}。（{launched_note}；若为首次使用，请在弹出的浏览器里登录"
                             f" BOSS 直聘后再次刷新）",
                    "debug_urls": data.get("debug_urls") or [],
                }
            return {"ok": False, "error": err, "debug_urls": data.get("debug_urls") or []}
        msgs = data.get("messages") or []
        stats = db.upsert_messages(msgs)
        _all = db.list_messages(limit=500)
        _total = db.count_messages()
        return {
            "ok": True,
            "fetched": len(msgs),
            "new": stats["new"],
            "messages": _all,
            "total": _total,
            "truncated": _total > len(_all),
            "fetched_at": data.get("fetched_at"),
            "note": launched_note or None,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "刷新超时（45s），CDP 连接可能卡住"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.get("/api/jobs/{platform}/{job_id}")
def get_seen_job(platform: str, job_id: str):
    job = db.get_job(platform, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.get("/api/jobs/{platform}/{job_id}/jd")
def get_job_jd(platform: str, job_id: str, refresh: bool = False):
    """岗位完整 JD 正文：优先读库缓存，无缓存（或 refresh=1）则抓详情页解析并入库。
    详情弹窗的抓取同样会接管键鼠，故沿用悬浮窗进度（缓存直读不上报，避免闪窗）。"""
    job = db.get_job(platform, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.get("jd_text") and not refresh:
        return _fetch_jd_core(job, refresh)
    with _progress_scope("抓取 JD 正文", "打开岗位详情页", total=1) as sc:
        sc.set(current=f"{(job.get('title') or '')[:30]} · {(job.get('company') or '')[:16]}")
        r = _fetch_jd_core(job, refresh)
        ok = bool(r.get("ok"))
        sc.set(done=1, ok=1 if ok else 0, failed=0 if ok else 1,
               last_error="" if ok else (r.get("error") or "抓取失败"))
        return r


def _communicate_pausable(proc: subprocess.Popen, timeout: float = 150) -> Tuple[Optional[Dict], str]:
    """带暂停/停止语义地等子进程输出，返回 (解析后的 JSON, 错误说明)。

    与 subprocess.communicate(timeout) 的区别：暂停时子进程可能停在安全点不动，
    超时倒计时不应继续走（否则「暂停 → 恢复」会被 150s 超时误杀）；停止时立刻
    kill 并返回，不再等满超时（子进程自己也会在安全点退出）。

    「真超时」与「子进程退了但没吐出可解析 JSON」必须分开返回：两者都报成超时会把
    编码崩溃一类真故障伪装成风控超时（2026-10-01 实测：JD 含 U+FFFC → 子进程 rc=1
    且 stdout 空，界面却显示「抓取超时（150s）」，白烧两次键鼠与风控额度还查不到方向）。
    """
    deadline = time.time() + timeout
    while True:
        try:
            out, err = proc.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if fetch_gate.stopped():
                _kill_proc(proc)
                return None, "已停止"
            if fetch_gate.paused():
                deadline = time.time() + timeout      # 暂停期间不计时
            elif time.time() >= deadline:
                _kill_proc(proc)
                return None, f"抓取超时（{int(timeout)}s）"
    text = (out or "").strip()
    if not text:
        tail = _tail(err)
        return None, f"抓取子进程无输出（退出码 {proc.returncode}）" + (f"：{tail}" if tail else "")
    try:
        return json.loads(text.splitlines()[-1]), ""
    except Exception:
        return None, "抓取子进程输出不是 JSON：" + (_tail(text) or _tail(err))


def _kill_proc(proc: subprocess.Popen):
    try:
        proc.kill()
        proc.communicate()
    except Exception:
        pass


def _tail(text, n: int = 220) -> str:
    """取输出尾部并压成一行：报错要看得见真因（traceback 末行），但不能整段灌进界面。"""
    return " ".join((text or "").split())[-n:]


def _fetch_jd_core(job: Dict, refresh: bool = False,
                   cancel: Optional[threading.Event] = None) -> Dict:
    """确保岗位有 JD：缓存直读（历史脏缓存出口兜底清洗并写回），缺失则
    原生通道 → CDP 兜底 → requests 直连三层降级。详情弹窗与批量分析共用。
    cancel：批量停止时置位——正在跑的子进程会被登记并在 stop 端点直接 kill，
    使停止在秒级生效（标志位只在岗位边界检查，子进程最长阻塞 150s）。"""
    def _run_fetch_jd(env_extra=None):
        """跑 fetch_jd 子进程；登记到 _batch_procs 供 stop 端点 kill。"""
        proc = subprocess.Popen(
            [sys.executable, "-m", FETCH_JD_MODULE,
             platform, job_id, job["url"],
             job.get("title") or "", job.get("company") or ""],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            # 管道的两端都钉 UTF-8：不写 encoding 时 Python 按系统区域设置选码
            # （中文机 = cp936），JD 里的非 GBK 字符会让两端各出一次事
            encoding="utf-8", errors="replace",
            cwd=PROJECT_DIR, env=({**os.environ, **(env_extra or {})} if env_extra else None),
        )
        with _batch_proc_lock:
            _batch_procs.add(proc)
        try:
            d2, note = _communicate_pausable(proc, timeout=150)
            if d2 is None:
                if note == "已停止":
                    return {"ok": False, "error": "已停止", "stopped": True}, None
                return None, note
            return d2, None
        finally:
            with _batch_proc_lock:
                _batch_procs.discard(proc)
    platform, job_id = job["platform"], job["job_id"]
    if job.get("jd_text") and not refresh:
        # 历史缓存可能含招聘者卡/公司介绍等冗余块（清洗上线前入库），出口兜底
        # 清洗一次并写回，之后即为纯缓存直读（内容来源未变，保留人工确认标记）
        jd = clean_jd(job["jd_text"])
        if jd != job["jd_text"]:
            db.save_jd(platform, job_id, jd[:20000], reset_confirm=False)
        return {"ok": True, "jd": jd, "cached": True}
    if not job.get("url"):
        return {"ok": False, "error": "该岗位没有原始链接，无法抓取 JD"}
    try:
        # 主路径：fetch_jd.py 内部自动调度 native→CDP（2026-09-25 决策：隐蔽优先，
        # 原生键鼠零浏览器痕迹）。不预启动调试 Chrome，让系统平时保持无 9222 端口
        fetch_err = None
        try:
            d2, terr = _run_fetch_jd()
            if terr:
                fetch_err = terr
            elif d2.get("ok") and d2.get("jd"):
                db.save_jd(platform, job_id, d2["jd"][:20000])
                return {"ok": True, "jd": d2["jd"][:20000], "cached": False}
            elif (d2 or {}).get("stopped"):
                # 用户点了「结束」：不再走 CDP/直连兜底，立即返回
                return {"ok": False, "error": d2.get("error") or "已停止", "stopped": True}
            else:
                fetch_err = (d2 or {}).get("error") or "抓取失败"
        except Exception as e:
            fetch_err = f"抓取异常: {type(e).__name__}: {e}"

        # 停止信号已在子进程 kill 后置位时，直接返回（不再走兜底链路）
        if (cancel is not None and cancel.is_set()) or fetch_gate.stopped():
            return {"ok": False, "error": "已停止", "stopped": True}

        # 兜底1：原生失败且 9222 不可达 → 此刻才拉调试 Chrome，用 CDP 通道重试一次
        if fetch_err and "9222 不可达" in fetch_err:
            ok, note = _ensure_cdp_chrome()
            if ok:
                try:
                    d2, terr = _run_fetch_jd({"FETCH_JD_MODE": "cdp"})
                    if terr:
                        fetch_err = terr
                    elif d2.get("ok") and d2.get("jd"):
                        db.save_jd(platform, job_id, d2["jd"][:20000])
                        return {"ok": True, "jd": d2["jd"][:20000], "cached": False}
                    else:
                        fetch_err = (d2 or {}).get("error") or "CDP 兜底抓取失败"
                except Exception as e:
                    fetch_err = f"CDP 兜底抓取异常: {type(e).__name__}: {e}"
            else:
                fetch_err = f"{fetch_err}；且无法启动调试浏览器：{note}"

        if cancel is not None and cancel.is_set():
            return {"ok": False, "error": "已停止", "stopped": True}

        # 兜底：requests 直连详情页 HTML（可能被「请稍候」挑战拦截）
        cookies = spider._load_cookies("boss")
        headers = {**spider.HEADERS, "Referer": "https://www.zhipin.com/"}
        resp = requests.get(job["url"], headers=headers, cookies=cookies, timeout=10)
        resp.encoding = resp.apparent_encoding or "utf-8"
        html = resp.text

        def _strip_tags(s: str) -> str:
            s = re.sub(r"<br\s*/?>", "\n", s)
            s = re.sub(r"<[^>]+>", "", s)
            return re.sub(r"\n{3,}", "\n\n", s.strip())

        # BOSS 详情页结构：<h3>小节标题</h3><div class="job-sec-text">正文</div>
        sections = re.findall(
            r"<h3[^>]*>([^<]{1,30})</h3>\s*<div[^>]*class=\"[^\"]*job-sec-text[^\"]*\"[^>]*>(.*?)</div>",
            html, re.S)
        jd = ""
        for sec_title, body in sections:
            t = sec_title.strip()
            if t in ("职位描述", "职位详情", "岗位职责"):
                jd = _strip_tags(body)
                break
        if not jd and sections:
            jd = _strip_tags(sections[0][1])
        if jd:
            db.save_jd(platform, job_id, jd[:20000])
            return {"ok": True, "jd": jd[:20000], "cached": False}
        return {"ok": False, "error": fetch_err + "；直连亦未解析到 JD，可点下方链接在 BOSS 查看"}
    except Exception as e:
        return {"ok": False, "error": f"抓取失败: {type(e).__name__}: {e}"}


# ---------- 面试日程 ----------
@app.get("/api/interviews")
def get_interviews():
    """面试日程：已安排（有时间，正序）+ 待安排（面试中但未定时间）。"""
    return db.list_interviews()

@app.post("/api/interviews")
def set_interview(req: InterviewReq):
    """设置/更新/清除某岗位的面试时间与备注。设时间自动把岗位状态置为「面试中」。"""
    job = db.set_interview(req.platform, req.job_id, req.interview_at, req.note)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return {"ok": True, "job": job}


class UpdateJobReq(BaseModel):
    status: str
    notes: Optional[str] = None


@app.post("/api/jobs/{platform}/{job_id}/jd-confirm")
def confirm_job_jd(platform: str, job_id: str, req: JdConfirmReq):
    """人工确认/撤销确认 JD 完整。

    疑似残缺（正文 <100 字）岗位由用户在详情弹窗核阅后确认：确认后移出「疑似残缺」、
    计入「已获取」；重新抓取落库会自动作废旧确认（save_jd reset_confirm）。"""
    job = db.set_jd_confirmed(platform, job_id, req.confirmed)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.patch("/api/jobs/{platform}/{job_id}")
def update_seen_job(platform: str, job_id: str, req: UpdateJobReq):
    """更新岗位 status 与 notes（用于 Kanban 拖拽改状态）。"""
    job = db.update_job_status(platform, job_id, req.status, req.notes)
    if not job:
        raise HTTPException(status_code=404, detail="job not found or invalid status")
    return job


@app.get("/")
def index():
    return FileResponse(os.path.join(paths.WEB_DIR, "job-workbench.html"))


# ---------- 一键重启（run.bat restart → python -m jobforge.server --restart） ----------
def _parse_argv(argv: List[str]) -> Tuple[int, bool]:
    """端口：默认 8080；可用 --port 8090 或 --port=8090 覆盖
    （调试/自测起第二个实例用；悬浮窗按此端口连回来，不会串到正式实例）。
    --restart：同端口已有实例时先安全结束它，而不是放弃启动。"""
    port, restart = 8080, False
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
        elif a.startswith("--port="):
            port = int(a.split("=", 1)[1])
        elif a == "--restart":
            restart = True
    return port, restart


def _http_get(port: int, path: str) -> Tuple[Optional[int], bytes]:
    """本机探活：返回 (HTTP 状态码, 响应体)；连不上返回 (None, b'')。
    非 2xx 也返回状态码——「有人在应答」本身就说明端口被占。"""
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:
        return None, b""


def _json_get(port: int, path: str) -> Optional[Dict]:
    status, body = _http_get(port, path)
    if status != 200:
        return None
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _run_cli(cmd: List[str], timeout: int = 10) -> str:
    """跑 Windows 命令行工具（netstat/tasklist），输出按文本返回。

    encoding 不能交给系统默认：PYTHONUTF8=1 环境下强制 utf-8，而中文系统
    netstat 输出是 GBK（列头「活动连接」），reader 线程直接 UnicodeDecodeError
    崩掉 → stdout 为空 → 重启保护误判。errors=replace 双保险。"""
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    if isinstance(out.stdout, str):        # 已是文本（如测试桩直接给 CompletedProcess(str)）
        return out.stdout
    for enc in ("gbk", "utf-8"):
        try:
            return out.stdout.decode(enc)
        except UnicodeDecodeError:
            continue
    return out.stdout.decode("utf-8", errors="replace")


def _port_owner_pid(port: int) -> Optional[int]:
    """netstat 查出正在 LISTENING 该端口的 PID。查不到返回 None（调用方据此拒绝动手）。"""
    out = _run_cli(["netstat", "-ano", "-p", "TCP"])
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 5 and f[0] == "TCP" and f[1].endswith(f":{port}") and f[3] == "LISTENING":
            try:
                return int(f[4])
            except ValueError:
                return None
    return None


def _kill_plan(info: Optional[Dict], listening_pid: Optional[int],
               busy: List[str]) -> Tuple[bool, str]:
    """结束旧实例前三重判定：① 身份确实是本项目 ② 它的自报 PID 与端口占用者一致
    ③ 当前没在跑抓取/批量。任一不满足就不碰任何进程——误杀的代价（杀掉别人开的
    程序、或撕断一段正在注入键鼠的抓取）远大于「让人手动关一次窗口」。
    info 里没有 pid 字段 = 旧版实例（还没装 /api/whoami），身份由 HTTP 指纹确认，
    PID 只取 netstat 那一个来源。"""
    if not info or info.get("app") != "jobforge":
        return False, "端口上有服务但认不出是 JobForge 实例，未结束任何进程"
    if listening_pid is None:
        return False, ("端口占用者与实例对不上（netstat 查不到该端口的 LISTENING 行），"
                       "拒绝结束进程")
    if info.get("pid") is not None and info["pid"] != listening_pid:
        return False, (f"PID 对不上（实例自报 {info['pid']}，端口占用者是 {listening_pid}），"
                       f"拒绝结束进程")
    if busy:
        return False, f"旧实例正在跑 {'、'.join(busy)}，请先在悬浮窗或页面上停手再重启"
    return True, f"结束旧实例 PID {listening_pid}"


def _busy_tasks(port: int) -> List[str]:
    """旧实例手上有没有活：批量分析/流水线在跑 / 抓取任务活跃（含原生键鼠注入）。"""
    checks = (("批量分析", "/api/analyze-batch/status", "running"),
              ("抓取任务", "/api/scrape-progress", "active"))
    return [name for name, path, key in checks if (_json_get(port, path) or {}).get(key)]


def _instance_identity(port: int) -> Optional[Dict]:
    """端口上应答的是不是本项目：优先问 /api/whoami（自报 PID），
    它不存在时退一步认 /api/platforms 的结构（老版本实例没装 whoami）。"""
    info = _json_get(port, "/api/whoami")
    if info is not None:
        return info if info.get("app") == "jobforge" else None
    if isinstance((_json_get(port, "/api/platforms") or {}).get("platforms"), list):
        return {"app": "jobforge"}
    return None


def _stop_existing(port: int) -> bool:
    """--restart 的执行体：判定通过才 taskkill，并等端口真正释放。返回能否继续启动。"""
    ok, why = _kill_plan(_instance_identity(port), _port_owner_pid(port), _busy_tasks(port))
    print(f"[JobForge] {why}")
    if not ok:
        return False
    _run_cli(["taskkill", "/PID", str(_port_owner_pid(port)), "/T", "/F"], timeout=15)
    for _ in range(50):                    # 最长 10 秒：端口不释放就别硬启
        status, _body = _http_get(port, "/api/whoami")
        if status is None:
            return True
        time.sleep(0.2)
    print(f"[JobForge] 已发出结束命令但 {port} 仍在应答，本次启动取消（请手动关掉那个窗口）")
    return False


if __name__ == "__main__":
    import uvicorn
    port, restart = _parse_argv(sys.argv[1:])
    os.environ["JOBFORGE_PORT"] = str(port)
    # 只监听本机回环：接口无鉴权且含个人求职数据，不暴露到局域网（2026-09-26）
    # 防重复启动（如连点两次 run.bat）：启动前探活同端口。
    # 注意：venv 的 python.exe 会派生出基础解释器子进程，任务管理器里两个 python
    # 属同一实例的正常父子形态（2026-09-27 曾误判为「双实例双绑」，已更正）。
    if _http_get(port, "/api/platforms")[0] is not None:
        if not restart:
            print(f"[JobForge] {port} 端口已有实例在运行，本次启动取消。"
                  f"要一键重启请跑 run.bat restart（或加 --restart）。")
            sys.exit(1)
        if not _stop_existing(port):
            sys.exit(1)
        print(f"[JobForge] 旧实例已结束，正在以新代码启动 {port} …")
    uvicorn.run(app, host="127.0.0.1", port=port)

