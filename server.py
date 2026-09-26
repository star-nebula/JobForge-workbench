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
  POST /api/cover-letter       求职信生成（本地模板，可选关联岗位）
  GET  /                       静态页面入口
"""
import io
import json
import os
import re
import subprocess
import sys
import time
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional

import spider
import db
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


def _build_cover_letter(pdata: Dict, job: Optional[Dict]) -> str:
    """求职信模板：全部取自个人资料与岗位真实字段；缺失信息留【】占位，不编造事实。"""
    def s(k: str) -> str:
        v = pdata.get(k)
        return v.strip() if isinstance(v, str) else ""
    skills = pdata.get("skills") or []
    if isinstance(skills, str):
        skills = [x.strip() for x in re.split(r"[，,、；;/\s]+", skills) if x.strip()]
    skills = [x for x in skills if x]
    name = s("name") or "【你的姓名】"
    job = job or {}
    title = (job.get("title") or "").strip()
    company = (job.get("company") or "").strip()
    tags = [str(t).strip() for t in (job.get("tags") or []) if str(t).strip()]

    lines = ["您好！", ""]
    if company and title:
        lines.append(f"我是{name}，从 BOSS 直聘看到贵司「{company} · {title}」岗位与我的方向十分契合，特此投递。")
    elif title:
        lines.append(f"我是{name}，看到贵司正在招聘「{title}」岗位，与我的方向十分契合，特此投递。")
    else:
        lines.append(f"我是{name}，正在寻找新的职业机会，特此投递简历，期待与您沟通。")
    lines.append("")
    if s("summary"):
        lines.append(s("summary"))
        lines.append("")
    if skills:
        fit = f"，与岗位要求的{'、'.join(tags[:5])}方向吻合" if tags else ""
        lines.append(f"技能方面，我熟悉{'、'.join(skills[:6])}等{fit}。")
        lines.append("")
    if s("experience"):
        exp = s("experience")
        lines.append(f"工作经历上：{exp[:120].rstrip()}{'……' if len(exp) > 120 else ''}")
        lines.append("")
    wants = []
    if s("city"):
        wants.append(f"期望工作地点为{s('city')}")
    if s("expected_salary"):
        wants.append(f"期望薪资{s('expected_salary')}")
    if wants:
        lines.append("，".join(wants) + "。")
        lines.append("")
    lines.append("简历中有更完整的经历与项目信息，期待有机会与您进一步沟通。")
    lines.append("")
    lines.append("此致")
    lines.append("敬礼！")
    lines.append("")
    lines.append(name)
    contact = " · ".join(x for x in [s("phone"), s("email")] if x)
    if contact:
        lines.append(contact)
    return "\n".join(lines)


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


# ---------- 求职信（本地模板生成，零 LLM） ----------
class CoverLetterReq(BaseModel):
    platform: str = Field("boss")
    job_id: Optional[str] = Field(None, description="关联岗位（可选；为空生成通用求职信）")


@app.post("/api/cover-letter")
def create_cover_letter(req: CoverLetterReq):
    """按个人资料真实信息套模板生成求职信文本。"""
    p = db.get_profile()
    pdata = (p or {}).get("data") or {}
    job = db.get_job(req.platform, req.job_id) if req.job_id else None
    if req.job_id and not job:
        raise HTTPException(status_code=404, detail="job not found")
    return {"ok": True, "letter": _build_cover_letter(pdata, job)}


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
    """岗位完整 JD 正文：优先读库缓存，无缓存（或 refresh=1）则带 cookie 抓详情页解析并入库。
    页面结构变化或被风控时返回 error，前端回退到已存字段 + 原始链接。"""
    job = db.get_job(platform, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
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
        import re as _re
        import requests as _rq
        cookies = spider._load_cookies("boss")
        headers = {**spider.HEADERS, "Referer": "https://www.zhipin.com/"}
        resp = _rq.get(job["url"], headers=headers, cookies=cookies, timeout=10)
        resp.encoding = resp.apparent_encoding or "utf-8"
        html = resp.text

        def _strip_tags(s: str) -> str:
            s = _re.sub(r"<br\s*/?>", "\n", s)
            s = _re.sub(r"<[^>]+>", "", s)
            return _re.sub(r"\n{3,}", "\n\n", s.strip())

        # BOSS 详情页结构：<h3>小节标题</h3><div class="job-sec-text">正文</div>
        sections = _re.findall(
            r"<h3[^>]*>([^<]{1,30})</h3>\s*<div[^>]*class=\"[^\"]*job-sec-text[^\"]*\"[^>]*>(.*?)</div>",
            html, _re.S)
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
    import uvicorn
    # 只监听本机回环：接口无鉴权且含个人求职数据，不暴露到局域网（2026-09-26）
    uvicorn.run(app, host="127.0.0.1", port=8080)
