"""SQLite 持久化已抓岗位。

表 seen_jobs：去重缓存 + 投递状态跟踪。
- platform + job_id 联合主键（同一岗位可能跨平台但 BOSS 单平台下 job_id 唯一，仍保留双键防扩展）
- status: discovered / reviewing / applied / interviewing / rejected / offered
- first_seen_at / last_seen_at：判断"新岗位"用
- match_score / tags / 详情字段冗余存（避免每次重新抓）

表 messages：消息中心会话缓存（msg_id 主键，每会话存最后一条消息 + 未读数）。

表 profile：个人资料（单行 id=1，data 存 JSON 全量字段，轻量持久化）。
"""
import datetime
import hashlib
import json
import os
import sqlite3
import time
from typing import Dict, List, Optional

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobs.db")

VALID_STATUSES = {"discovered", "reviewing", "applied", "interviewing", "rejected", "offered"}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """建表 + 加索引（幂等）。在 server 启动时调一次。
    P3 升级：加 score_detail 字段（JSON 字符串存 4 维 + reasoning）。
    """
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS seen_jobs (
                platform TEXT NOT NULL,
                job_id TEXT NOT NULL,
                title TEXT,
                company TEXT,
                salary TEXT,
                city TEXT,
                experience TEXT,
                education TEXT,
                tags TEXT,           -- JSON array
                url TEXT,
                match_score INTEGER,
                score_detail TEXT,  -- P3: JSON {overall, skills_match, experience_match, salary_match, location_match, reasoning}
                status TEXT DEFAULT 'discovered',
                notes TEXT,
                first_seen_at INTEGER,
                last_seen_at INTEGER,
                PRIMARY KEY (platform, job_id)
            )
        """)
        # 兼容老库：如果 seen_jobs 已存在但缺 score_detail，自动 ALTER ADD
        cols = [r[1] for r in c.execute("PRAGMA table_info(seen_jobs)").fetchall()]
        if "score_detail" not in cols:
            c.execute("ALTER TABLE seen_jobs ADD COLUMN score_detail TEXT")
        # 面试日程：约定时间（毫秒时间戳）+ 备注；设了时间自动置 status='interviewing'
        if "interview_at" not in cols:
            c.execute("ALTER TABLE seen_jobs ADD COLUMN interview_at INTEGER")
        if "interview_note" not in cols:
            c.execute("ALTER TABLE seen_jobs ADD COLUMN interview_note TEXT")
        # 完整 JD 正文（岗位详情弹窗展示用；抓一次缓存，避免重复请求触发风控）
        if "jd_text" not in cols:
            c.execute("ALTER TABLE seen_jobs ADD COLUMN jd_text TEXT")
        c.execute("CREATE INDEX IF NOT EXISTS idx_seen_status ON seen_jobs(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_seen_first_seen ON seen_jobs(first_seen_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_seen_interview ON seen_jobs(interview_at)")
        # 消息中心：会话级缓存
        c.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                msg_id TEXT PRIMARY KEY,
                platform TEXT NOT NULL DEFAULT 'boss',
                friend_id TEXT,
                sender TEXT,
                job_name TEXT,
                company_name TEXT,
                content TEXT,
                time_ms INTEGER,
                unread INTEGER DEFAULT 0,
                first_seen_at INTEGER,
                last_seen_at INTEGER
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_messages_time ON messages(time_ms)")
        # 个人资料：单行表（id 恒为 1），data 存 JSON
        c.execute("""
            CREATE TABLE IF NOT EXISTS profile (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                data TEXT NOT NULL,
                updated_at INTEGER
            )
        """)
        # 智能抓取历史：每次抓取一条（含失败），用于小窗「历史记录」与防重复参考
        c.execute("""
            CREATE TABLE IF NOT EXISTS scrape_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                city TEXT,
                page INTEGER DEFAULT 1,
                query_used TEXT,
                total INTEGER DEFAULT 0,
                new_count INTEGER DEFAULT 0,
                source TEXT,
                error TEXT,
                created_at INTEGER
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_scrape_log_time ON scrape_log(created_at)")
        # 简历版本：个人资料快照（「一份资料多份简历」方向）
        c.execute("""
            CREATE TABLE IF NOT EXISTS resume_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                data TEXT NOT NULL,
                created_at INTEGER
            )
        """)


def upsert_jobs(jobs: List[Dict]) -> List[Dict]:
    """批量 upsert 岗位列表。返回每个岗位是否本次新增（is_new=true）。
    已存在的岗位更新 last_seen_at + match_score + 字段，但保留原 status/notes。"""
    now = int(time.time())
    is_new_map = {}
    if not jobs:
        return []
    with _conn() as c:
        for j in jobs:
            pid = j.get("platform") or "boss"
            jid = str(j.get("job_id") or "")
            if not jid:
                continue
            tags = j.get("tags") or []
            row = c.execute(
                "SELECT platform, job_id, status, notes, first_seen_at FROM seen_jobs WHERE platform=? AND job_id=?",
                (pid, jid)
            ).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO seen_jobs (platform, job_id, title, company, salary, city, "
                    "experience, education, tags, url, match_score, score_detail, status, notes, "
                    "first_seen_at, last_seen_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, jid, j.get("title", ""), j.get("company", ""), j.get("salary", ""),
                     j.get("city", ""), j.get("experience", ""), j.get("education", ""),
                     json.dumps(tags, ensure_ascii=False), j.get("url", ""),
                     int(j.get("match_score") or 0),
                     json.dumps(j.get("score_detail") or {}, ensure_ascii=False),
                     "discovered", "", now, now)
                )
                is_new_map[(pid, jid)] = True
            else:
                c.execute("""
                    UPDATE seen_jobs SET title=?, company=?, salary=?, city=?, experience=?,
                        education=?, tags=?, url=?, match_score=?, score_detail=?, last_seen_at=?
                    WHERE platform=? AND job_id=?
                """, (
                    j.get("title", ""), j.get("company", ""), j.get("salary", ""),
                    j.get("city", ""), j.get("experience", ""), j.get("education", ""),
                    json.dumps(tags, ensure_ascii=False), j.get("url", ""),
                    int(j.get("match_score") or 0),
                    json.dumps(j.get("score_detail") or {}, ensure_ascii=False),
                    now, pid, jid,
                ))
                is_new_map[(pid, jid)] = False
    return [is_new_map.get(((j.get("platform") or "boss"), str(j.get("job_id") or "")), False) for j in jobs]


def list_jobs(status: Optional[str] = None) -> List[Dict]:
    """列岗位。status 为空返回全部，按 last_seen_at 倒序。"""
    with _conn() as c:
        if status:
            rows = c.execute(
                "SELECT * FROM seen_jobs WHERE status=? ORDER BY last_seen_at DESC", (status,)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM seen_jobs ORDER BY last_seen_at DESC"
            ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_job(platform: str, job_id: str) -> Optional[Dict]:
    with _conn() as c:
        r = c.execute(
            "SELECT * FROM seen_jobs WHERE platform=? AND job_id=?", (platform, job_id)
        ).fetchone()
        return _row_to_dict(r) if r else None


def update_job_status(platform: str, job_id: str, status: str, notes: Optional[str] = None) -> Optional[Dict]:
    if status not in VALID_STATUSES:
        return None
    with _conn() as c:
        if notes is not None:
            c.execute(
                "UPDATE seen_jobs SET status=?, notes=? WHERE platform=? AND job_id=?",
                (status, notes, platform, job_id)
            )
        else:
            c.execute(
                "UPDATE seen_jobs SET status=? WHERE platform=? AND job_id=?",
                (status, platform, job_id)
            )
        r = c.execute(
            "SELECT * FROM seen_jobs WHERE platform=? AND job_id=?", (platform, job_id)
        ).fetchone()
        return _row_to_dict(r) if r else None


def set_interview(platform: str, job_id: str, interview_at: Optional[int], note: Optional[str]) -> Optional[Dict]:
    """设置/更新/清除某岗位的面试时间与备注。
    interview_at 为毫秒时间戳或 None（清除）；设了时间自动把 status 置为 interviewing。"""
    with _conn() as c:
        exists = c.execute(
            "SELECT 1 FROM seen_jobs WHERE platform=? AND job_id=?", (platform, job_id)
        ).fetchone()
        if not exists:
            return None
        c.execute(
            "UPDATE seen_jobs SET interview_at=?, interview_note=? WHERE platform=? AND job_id=?",
            (int(interview_at) if interview_at else None, note or "", platform, job_id)
        )
        if interview_at:
            c.execute(
                "UPDATE seen_jobs SET status='interviewing' WHERE platform=? AND job_id=?",
                (platform, job_id)
            )
        r = c.execute(
            "SELECT * FROM seen_jobs WHERE platform=? AND job_id=?", (platform, job_id)
        ).fetchone()
        return _row_to_dict(r) if r else None


def save_jd(platform: str, job_id: str, jd_text: str) -> bool:
    """缓存岗位完整 JD 正文（详情弹窗用）。"""
    with _conn() as c:
        cur = c.execute(
            "UPDATE seen_jobs SET jd_text=? WHERE platform=? AND job_id=?",
            (jd_text, platform, job_id)
        )
        return cur.rowcount > 0


def list_interviews() -> Dict:
    """面试日程：已安排（interview_at 非空，按时间正序）+ 待安排（status=interviewing 但没定时间）。"""
    with _conn() as c:
        scheduled = [
            _row_to_dict(r) for r in c.execute(
                "SELECT * FROM seen_jobs WHERE interview_at IS NOT NULL ORDER BY interview_at ASC"
            ).fetchall()
        ]
        pending = [
            _row_to_dict(r) for r in c.execute(
                "SELECT * FROM seen_jobs WHERE interview_at IS NULL AND status='interviewing' "
                "ORDER BY last_seen_at DESC"
            ).fetchall()
        ]
    return {"scheduled": scheduled, "pending": pending}


def get_stats() -> Dict:
    """看板聚合统计（全部来自 seen_jobs 真实数据）。

    返回：total / status_dist / avg_score / score_dist / trend_14d / top_jobs / recent_jobs
    """
    now = int(time.time())
    day = 86400
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM seen_jobs").fetchone()[0]
        status_dist = {s: 0 for s in VALID_STATUSES}
        for r in c.execute("SELECT status, COUNT(*) FROM seen_jobs GROUP BY status"):
            status_dist[r[0]] = r[1]
        avg = c.execute("SELECT AVG(match_score) FROM seen_jobs WHERE match_score > 0").fetchone()[0]
        score_dist = {"high": 0, "mid": 0, "low": 0}
        for (s,) in c.execute("SELECT match_score FROM seen_jobs WHERE match_score > 0"):
            if s >= 75:
                score_dist["high"] += 1
            elif s >= 50:
                score_dist["mid"] += 1
            else:
                score_dist["low"] += 1
        by_date = {
            r[0]: r[1]
            for r in c.execute(
                "SELECT date(first_seen_at, 'unixepoch', 'localtime') d, COUNT(*) n "
                "FROM seen_jobs WHERE first_seen_at >= ? GROUP BY d",
                (now - 13 * day,),
            )
        }
        top_jobs = [
            dict(r)
            for r in c.execute(
                "SELECT platform, job_id, title, company, city, salary, match_score, url "
                "FROM seen_jobs ORDER BY match_score DESC LIMIT 5"
            )
        ]
        recent_jobs = [
            dict(r)
            for r in c.execute(
                "SELECT platform, job_id, title, company, city, salary, match_score, url, first_seen_at "
                "FROM seen_jobs ORDER BY first_seen_at DESC LIMIT 6"
            )
        ]
    today = datetime.date.today()
    trend = []
    for i in range(13, -1, -1):
        d = (today - datetime.timedelta(days=i)).isoformat()
        trend.append({"date": d, "count": by_date.get(d, 0)})
    return {
        "total": total,
        "status_dist": status_dist,
        "avg_score": round(avg or 0),
        "score_dist": score_dist,
        "trend_14d": trend,
        "top_jobs": top_jobs,
        "recent_jobs": recent_jobs,
    }


def upsert_messages(messages: List[Dict], platform: str = "boss") -> Dict:
    """批量 upsert 会话消息（会话级：msg_id = platform:friend_id）。
    已存在的会话更新最后消息/未读数，新增会话记 first_seen_at。
    返回 {"total": n, "new": k}。"""
    now = int(time.time())
    total = new = 0
    if not messages:
        return {"total": 0, "new": 0}
    with _conn() as c:
        for m in messages:
            fid = str(m.get("friend_id") or "")
            if fid:
                msg_id = f"{platform}:{fid}"
            else:
                # 无 id 的会话用 name+content 生成稳定 id（BOSS 改结构时兜底）
                raw = f"{m.get('sender', '')}|{m.get('content', '')}"
                msg_id = f"{platform}:h:" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]
            exists = c.execute("SELECT 1 FROM messages WHERE msg_id=?", (msg_id,)).fetchone()
            c.execute("""
                INSERT INTO messages (msg_id, platform, friend_id, sender, job_name, company_name,
                    content, time_ms, unread, first_seen_at, last_seen_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(msg_id) DO UPDATE SET
                    sender=excluded.sender, job_name=excluded.job_name,
                    company_name=excluded.company_name, content=excluded.content,
                    time_ms=excluded.time_ms, unread=excluded.unread,
                    last_seen_at=excluded.last_seen_at
            """, (msg_id, platform, fid, m.get("sender", ""), m.get("job_name", ""),
                  m.get("company_name", ""), m.get("content", ""), m.get("time_ms"),
                  int(m.get("unread") or 0), now, now))
            total += 1
            if not exists:
                new += 1
    return {"total": total, "new": new}


def list_messages(limit: int = 200) -> List[Dict]:
    """列会话消息，按消息时间倒序（无时间的排最后）。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM messages ORDER BY COALESCE(time_ms, 0) DESC, last_seen_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_profile() -> Optional[Dict]:
    """读个人资料（单行表）。返回 {data, updated_at}，无记录返回 None。"""
    with _conn() as c:
        r = c.execute("SELECT data, updated_at FROM profile WHERE id=1").fetchone()
        if not r:
            return None
        try:
            data = json.loads(r["data"])
        except Exception:
            return None
        return {"data": data, "updated_at": r["updated_at"]}


def save_profile(data: Dict) -> int:
    """保存个人资料（单行 upsert）。返回 updated_at 时间戳。"""
    now = int(time.time())
    with _conn() as c:
        c.execute(
            "INSERT INTO profile (id, data, updated_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
            (json.dumps(data, ensure_ascii=False), now),
        )
    return now


# ---------- 简历版本（个人资料快照） ----------
def list_resume_versions() -> List[Dict]:
    """版本列表（不含 data 大字段），按创建时间倒序（id 倒序）。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, name, created_at FROM resume_versions ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def save_resume_version(name: str, data: Dict) -> int:
    """把 data 存为一个命名版本快照，返回版本 id。"""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO resume_versions (name, data, created_at) VALUES (?,?,?)",
            ((name or "").strip()[:40] or "未命名版本",
             json.dumps(data, ensure_ascii=False), int(time.time())),
        )
        return int(cur.lastrowid)


def get_resume_version(version_id: int) -> Optional[Dict]:
    """读单个版本（含 data）。不存在或 data 损坏返回 None。"""
    with _conn() as c:
        r = c.execute(
            "SELECT id, name, data, created_at FROM resume_versions WHERE id=?", (version_id,)
        ).fetchone()
        if not r:
            return None
        d = dict(r)
        try:
            d["data"] = json.loads(d["data"])
        except Exception:
            return None
        return d


def delete_resume_version(version_id: int) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM resume_versions WHERE id=?", (version_id,))
        return cur.rowcount > 0


def add_scrape_log(platform: str, city: Optional[str], page: int, query_used: str,
                   total: int, new_count: int, source: str, error: str = "") -> int:
    """记录一次智能抓取（含失败）。返回 created_at。"""
    now = int(time.time())
    with _conn() as c:
        c.execute(
            "INSERT INTO scrape_log (platform, city, page, query_used, total, new_count, source, error, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (platform, city or "", int(page or 1), query_used or "", int(total or 0),
             int(new_count or 0), source or "", (error or "")[:300], now),
        )
    return now


def list_scrape_logs(limit: int = 50) -> List[Dict]:
    """抓取历史，按时间倒序。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM scrape_log ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def _row_to_dict(r: sqlite3.Row) -> Dict:
    d = dict(r)
    if d.get("tags"):
        try:
            d["tags"] = json.loads(d["tags"])
        except Exception:
            d["tags"] = []
    else:
        d["tags"] = []
    if d.get("score_detail"):
        try:
            d["score_detail"] = json.loads(d["score_detail"])
        except Exception:
            d["score_detail"] = {}
    else:
        d["score_detail"] = {}
    return d
