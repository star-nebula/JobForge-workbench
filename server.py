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
  POST /api/analyze-batch      批量分析：抓 JD + 分析全部缺分析岗位（后台线程）
  GET  /api/analyze-batch/status / POST .../stop   批量进度查询 / 停止
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
from typing import Any, Dict, List, Optional

import spider
import db
import llm
import profile_score
from fetch_jd import clean_jd

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIES_FILE = os.path.join(PROJECT_DIR, "cookies.json")
GRAB_SCRIPT = os.path.join(PROJECT_DIR, "grab_cookies.py")

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


class CrawlReq(BaseModel):
    platform: str = Field("all", description="boss | all（目前只支持 BOSS，all 等同 boss）")
    query: str = Field(..., min_length=1, description="搜索关键词")
    city: str = Field("全国", description="城市名")
    page: int = Field(1, ge=1, description="页码（单页，不做区间）")
    use_mock: bool = Field(False, description="已废弃，保留为兼容签名")


class ScrapeFromResumeReq(BaseModel):
    resume_text: str = Field(..., min_length=1, description="简历纯文本")
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
CHROME_PROFILE_DIR = os.path.join(PROJECT_DIR, "chrome-profile")
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
    """用 venv python 跑 grab_cookies.py，从 CDP（127.0.0.1:9222）重抓 cookie 写 cookies.json。
    若因 Chrome 9222 未启动而失败，自动拉起调试 Chrome 后重试一次。"""
    if not os.path.exists(GRAB_SCRIPT):
        return {"ok": False, "error": "grab_cookies.py 不存在"}

    def _run():
        return subprocess.run(
            [sys.executable, GRAB_SCRIPT],
            capture_output=True, text=True, timeout=15,
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
            [sys.executable, os.path.join(PROJECT_DIR, "fetch_jd_native.py"), "--check-json"],
            capture_output=True, text=True, timeout=60, cwd=PROJECT_DIR,
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


# ---------- 批量分析：抓 JD + 逐个 LLM 分析（后台线程，前端轮询进度） ----------
class AnalyzeBatchReq(BaseModel):
    limit: Optional[int] = Field(None, ge=1, description="最多处理多少个岗位，缺省全部")
    force: bool = Field(False, description="true=已分析的也重新分析")
    model_id: Optional[Any] = None


_batch = {"running": False, "stop": False, "total": 0, "done": 0,
          "ok": 0, "failed": 0, "current": "", "errors": []}


@app.post("/api/analyze-batch")
def start_analyze_batch(req: AnalyzeBatchReq):
    """开始批量分析。每个岗位先确保 JD（无缓存走原生抓取通道，约 20~40s/个），再 LLM 分析。"""
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
    jobs = [j for j in db.list_jobs() if req.force or not j.get("llm_analysis")]
    if req.limit:
        jobs = jobs[:req.limit]
    if not jobs:
        return {"ok": True, "started": False, "message": "没有需要分析的岗位（已有分析的可用「强制重分析」）"}
    _batch.update({"running": True, "stop": False, "total": len(jobs), "done": 0,
                   "ok": 0, "failed": 0, "current": "", "errors": []})
    threading.Thread(target=_batch_worker, args=(jobs, cfg), daemon=True).start()
    return {"ok": True, "started": True, "total": len(jobs)}


def _batch_worker(jobs, cfg):
    consecutive_fail = 0
    for j in jobs:
        if _batch["stop"]:
            break
        _batch["current"] = f"{(j.get('title') or '')[:30]} · {(j.get('company') or '')[:16]}"
        try:
            jd_res = _fetch_jd_core(j, refresh=False)
            if not jd_res.get("ok"):
                raise llm.LLMError(jd_res.get("error") or "JD 抓取失败")
            fresh = db.get_job(j["platform"], j["job_id"]) or j
            analysis = llm.analyze_match(cfg, (db.get_profile() or {}).get("data") or {}, fresh)
            db.save_job_analysis(j["platform"], j["job_id"], analysis)
            _batch["ok"] += 1
            consecutive_fail = 0
        except Exception as e:
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
    _batch["running"] = False
    _batch["current"] = ""


@app.get("/api/analyze-batch/status")
def analyze_batch_status():
    return {**_batch}


@app.post("/api/analyze-batch/stop")
def analyze_batch_stop():
    _batch["stop"] = True
    return {"ok": True}


@app.post("/api/crawl")
def crawl_jobs(req: CrawlReq):
    result = spider.crawl(req.platform, req.query, req.city, req.page, req.use_mock)
    return result


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
    query = keywords["target_position"] or " ".join(keywords["skills"][:3]) or "前端工程师"
    city = req.city or keywords["city"] or "全国"

    result = spider.crawl(req.platform, query, city, req.page, req.use_mock)

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
def list_seen_jobs(status: Optional[str] = None):
    """列出已缓存的岗位。status 为空返回全部，按 last_seen_at 倒序。"""
    return {"jobs": db.list_jobs(status)}


@app.get("/api/stats")
def get_stats():
    """看板聚合统计（总数/状态分布/匹配度分布/14 天趋势/Top5/最近动态）。"""
    return db.get_stats()


@app.get("/api/messages")
def get_messages():
    """已缓存的会话消息（消息中心，按消息时间倒序）。"""
    return {"messages": db.list_messages()}


@app.post("/api/messages/refresh")
def refresh_messages():
    """跑 messages.py 从 CDP 浏览器监听 BOSS 聊天页会话响应，入库后返回最新列表。
    若因 Chrome 9222 未启动而失败，自动拉起调试 Chrome 后重试一次。"""
    script = os.path.join(PROJECT_DIR, "messages.py")
    if not os.path.exists(script):
        return {"ok": False, "error": "messages.py 不存在"}

    def _run():
        r = subprocess.run(
            [sys.executable, script],
            capture_output=True, text=True, timeout=45,
            cwd=PROJECT_DIR,
        )
        # 脚本崩溃与否都尝试读 messages.json（业务失败信息在文件里）
        out = os.path.join(PROJECT_DIR, "messages.json")
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
        return {
            "ok": True,
            "fetched": len(msgs),
            "new": stats["new"],
            "messages": db.list_messages(),
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
    """岗位完整 JD 正文：优先读库缓存，无缓存（或 refresh=1）则抓详情页解析并入库。"""
    job = db.get_job(platform, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return _fetch_jd_core(job, refresh)


def _fetch_jd_core(job: Dict, refresh: bool = False) -> Dict:
    """确保岗位有 JD：缓存直读（历史脏缓存出口兜底清洗并写回），缺失则
    原生通道 → CDP 兜底 → requests 直连三层降级。详情弹窗与批量分析共用。"""
    platform, job_id = job["platform"], job["job_id"]
    if job.get("jd_text") and not refresh:
        # 历史缓存可能含招聘者卡/公司介绍等冗余块（清洗上线前入库），出口兜底
        # 清洗一次并写回，之后即为纯缓存直读
        jd = clean_jd(job["jd_text"])
        if jd != job["jd_text"]:
            db.save_jd(platform, job_id, jd[:20000])
        return {"ok": True, "jd": jd, "cached": True}
    if not job.get("url"):
        return {"ok": False, "error": "该岗位没有原始链接，无法抓取 JD"}
    try:
        # 主路径：fetch_jd.py 内部自动调度 native→CDP（2026-09-25 决策：隐蔽优先，
        # 原生键鼠零浏览器痕迹）。不预启动调试 Chrome，让系统平时保持无 9222 端口
        fetch_err = None
        try:
            r2 = subprocess.run(
                [sys.executable, os.path.join(PROJECT_DIR, "fetch_jd.py"),
                 platform, job_id, job["url"],
                 job.get("title") or "", job.get("company") or ""],
                capture_output=True, text=True, timeout=150, cwd=PROJECT_DIR,
            )
            line = (r2.stdout or "").strip().splitlines()
            d2 = json.loads(line[-1]) if line else {}
            if d2.get("ok") and d2.get("jd"):
                db.save_jd(platform, job_id, d2["jd"][:20000])
                return {"ok": True, "jd": d2["jd"][:20000], "cached": False}
            fetch_err = d2.get("error") or "抓取失败"
        except Exception as e:
            fetch_err = f"抓取异常: {type(e).__name__}: {e}"

        # 兜底1：原生失败且 9222 不可达 → 此刻才拉调试 Chrome，用 CDP 通道重试一次
        if fetch_err and "9222 不可达" in fetch_err:
            ok, note = _ensure_cdp_chrome()
            if ok:
                try:
                    env = {**os.environ, "FETCH_JD_MODE": "cdp"}
                    r2 = subprocess.run(
                        [sys.executable, os.path.join(PROJECT_DIR, "fetch_jd.py"),
                         platform, job_id, job["url"],
                         job.get("title") or "", job.get("company") or ""],
                        capture_output=True, text=True, timeout=90, cwd=PROJECT_DIR, env=env,
                    )
                    line = (r2.stdout or "").strip().splitlines()
                    d2 = json.loads(line[-1]) if line else {}
                    if d2.get("ok") and d2.get("jd"):
                        db.save_jd(platform, job_id, d2["jd"][:20000])
                        return {"ok": True, "jd": d2["jd"][:20000], "cached": False}
                    fetch_err = d2.get("error") or "CDP 兜底抓取失败"
                except Exception as e:
                    fetch_err = f"CDP 兜底抓取异常: {type(e).__name__}: {e}"
            else:
                fetch_err = f"{fetch_err}；且无法启动调试浏览器：{note}"

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


@app.patch("/api/jobs/{platform}/{job_id}")
def update_seen_job(platform: str, job_id: str, req: UpdateJobReq):
    """更新岗位 status 与 notes（用于 Kanban 拖拽改状态）。"""
    job = db.update_job_status(platform, job_id, req.status, req.notes)
    if not job:
        raise HTTPException(status_code=404, detail="job not found or invalid status")
    return job


@app.get("/")
def index():
    return FileResponse("job-workbench.html")


if __name__ == "__main__":
    import urllib.error
    import urllib.request
    import uvicorn
    # 只监听本机回环：接口无鉴权且含个人求职数据，不暴露到局域网（2026-09-26）
    # 防双实例：Windows 下 asyncio 默认 SO_REUSEADDR 允许双绑同一端口，请求会随机落到
    # 新旧两个进程（2026-09-27 事故：状态与批量线程分家）。启动前先探活
    try:
        urllib.request.urlopen("http://127.0.0.1:8080/api/platforms", timeout=2)
        print("[JobForge] 8080 端口已有 JobForge 实例在运行，本次启动取消。如确需重启，请先结束旧的 server.py 进程。")
        sys.exit(1)
    except urllib.error.HTTPError:
        # 端口有 HTTP 响应（无论什么路径）＝ 已有服务在跑
        print("[JobForge] 8080 端口已有服务在响应，本次启动取消。")
        sys.exit(1)
    except Exception:
        pass  # 连不上 = 端口空闲，正常启动
    uvicorn.run(app, host="127.0.0.1", port=8080)
