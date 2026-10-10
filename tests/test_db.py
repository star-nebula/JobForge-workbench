"""db 层回环测试（jobs / profile / resume_versions / scrape_log），用 tmp 库不动真实 jobs.db。"""
import pytest

from jobforge import db


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "test_jobs.db"))
    db.init_db()
    return db


def _job(job_id="abc123", **over):
    j = {"platform": "boss", "job_id": job_id, "title": "前端工程师", "company": "X 公司",
         "salary": "20-30K", "city": "上海", "tags": ["React"], "url": "https://zhipin.com/x",
         "match_score": 77}
    j.update(over)
    return j


def test_job_upsert_and_roundtrip(tmp_db):
    flags = tmp_db.upsert_jobs([_job(), _job(job_id="def456")])
    assert flags == [True, True]
    flags = tmp_db.upsert_jobs([_job()])
    assert flags == [False]                  # 二次 upsert 非新增
    job = tmp_db.get_job("boss", "abc123")
    assert job["title"] == "前端工程师"
    assert job["tags"] == ["React"]
    assert job["status"] == "discovered"
    # upsert 保留原 status/notes
    tmp_db.update_job_status("boss", "abc123", "applied", "已投")
    tmp_db.upsert_jobs([_job()])
    job = tmp_db.get_job("boss", "abc123")
    assert job["status"] == "applied" and job["notes"] == "已投"


def test_job_status_validation(tmp_db):
    tmp_db.upsert_jobs([_job()])
    assert tmp_db.update_job_status("boss", "abc123", "不存在的状态") is None
    assert tmp_db.update_job_status("boss", "nope", "applied") is None


def test_terminal_statuses_roundtrip(tmp_db):
    """两个终态可写、可改判，并计入仪表盘 status_dist（前端曾根本没有通向它们的路径）。"""
    tmp_db.upsert_jobs([_job()])
    assert tmp_db.update_job_status("boss", "abc123", "rejected")["status"] == "rejected"
    assert tmp_db.update_job_status("boss", "abc123", "offered")["status"] == "offered"
    dist = tmp_db.get_stats()["status_dist"]
    assert dist["offered"] == 1 and dist["rejected"] == 0 and dist["discovered"] == 0


def test_notes_none_keeps_and_empty_clears(tmp_db):
    """notes=None 只改状态不动备注；空串才是清除（弹窗改状态时不能顺手抹掉用户备注）。"""
    tmp_db.upsert_jobs([_job()])
    tmp_db.update_job_status("boss", "abc123", "applied", "内推人：李工")
    assert tmp_db.update_job_status("boss", "abc123", "interviewing")["notes"] == "内推人：李工"
    assert tmp_db.update_job_status("boss", "abc123", "interviewing", "")["notes"] == ""


def test_interview_sets_status(tmp_db):
    tmp_db.upsert_jobs([_job()])
    job = tmp_db.set_interview("boss", "abc123", 1770000000000, "二面 · 现场")
    assert job["status"] == "interviewing"
    listed = tmp_db.list_interviews()
    assert [j["job_id"] for j in listed["scheduled"]] == ["abc123"]
    assert not listed["pending"]
    job = tmp_db.set_interview("boss", "abc123", None, "")
    assert job["interview_at"] is None       # 清除后仍在 pending 列
    assert [j["job_id"] for j in tmp_db.list_interviews()["pending"]] == ["abc123"]


def test_jd_cache_roundtrip(tmp_db):
    tmp_db.upsert_jobs([_job()])
    assert tmp_db.save_jd("boss", "abc123", "岗位职责：\n1. 开发")
    assert tmp_db.get_job("boss", "abc123")["jd_text"].startswith("岗位职责")
    assert tmp_db.save_jd("boss", "nope", "x") is False


def test_profile_and_versions(tmp_db):
    assert tmp_db.get_profile() is None
    data = {"name": "张三", "skills": ["React", "Vue"]}
    tmp_db.save_profile(data)
    p = tmp_db.get_profile()
    assert p["data"]["name"] == "张三" and p["updated_at"] > 0

    vid = tmp_db.save_resume_version("偏后端版", data)
    versions = tmp_db.list_resume_versions()
    assert [v["id"] for v in versions] == [vid]          # 列表不含 data 大字段
    assert "data" not in versions[0]
    v = tmp_db.get_resume_version(vid)
    assert v["name"] == "偏后端版" and v["data"]["skills"] == ["React", "Vue"]
    # 应用 = 覆盖当前资料
    tmp_db.save_profile(v["data"])
    assert tmp_db.get_profile()["data"]["name"] == "张三"
    assert tmp_db.delete_resume_version(vid) is True
    assert tmp_db.get_resume_version(vid) is None
    assert tmp_db.delete_resume_version(vid) is False
    assert tmp_db.get_resume_version(999) is None


def test_scrape_log(tmp_db):
    tmp_db.add_scrape_log("boss", "上海", 1, "前端", 30, 12, "real")
    tmp_db.add_scrape_log("boss", "", 2, "前端", 0, 0, "error", "风控")
    logs = tmp_db.list_scrape_logs()
    assert len(logs) == 2
    assert logs[0]["source"] == "error" and logs[0]["error"] == "风控"   # 倒序


def test_app_settings_roundtrip(tmp_db):
    assert tmp_db.get_app_settings() == {}
    tmp_db.save_app_settings({"llm": {"configs": [{"id": "a", "model": "m"}], "active_id": "a"}})
    s = tmp_db.get_app_settings()
    assert s["llm"]["active_id"] == "a" and s["llm"]["configs"][0]["model"] == "m"


def test_job_analysis_cache(tmp_db):
    tmp_db.upsert_jobs([_job()])
    assert tmp_db.get_job("boss", "abc123")["llm_analysis"] is None
    assert tmp_db.save_job_analysis("boss", "abc123", {"verdict": "匹配", "score": 66}) is True
    assert tmp_db.get_job("boss", "abc123")["llm_analysis"]["score"] == 66
    assert tmp_db.save_job_analysis("boss", "nope", {}) is False


def test_analysis_auto_promotes_reviewing(tmp_db):
    """带 review_floor 的分析落库：达标把 discovered 升入 reviewing，且只向前不回退。
    2026-10-09 粗筛退出主线后，漏斗入口改由 AI 分析分驱动，这里就是那条边界的唯一落点。"""
    tmp_db.upsert_jobs([_job(job_id="a"), _job(job_id="b"), _job(job_id="c")])
    tmp_db.save_job_analysis("boss", "a", {"score": 75}, review_floor=70)
    assert tmp_db.get_job("boss", "a")["status"] == "reviewing"            # 达标 → 升档
    tmp_db.save_job_analysis("boss", "b", {"score": 69}, review_floor=70)
    assert tmp_db.get_job("boss", "b")["status"] == "discovered"           # 差 1 分不动
    tmp_db.update_job_status("boss", "c", "applied", "已投")
    tmp_db.save_job_analysis("boss", "c", {"score": 99}, review_floor=70)
    assert tmp_db.get_job("boss", "c")["status"] == "applied"              # 更后段不回退


def test_analysis_without_floor_never_moves_status(tmp_db):
    """不传 review_floor＝纯缓存分析，绝不动状态；score 缺失或类型不对也不炸。"""
    tmp_db.upsert_jobs([_job(job_id="a"), _job(job_id="b")])
    tmp_db.save_job_analysis("boss", "a", {"score": 99})
    assert tmp_db.get_job("boss", "a")["status"] == "discovered"
    tmp_db.save_job_analysis("boss", "b", {"score": None}, review_floor=70)
    assert tmp_db.get_job("boss", "b")["status"] == "discovered"
    tmp_db.save_job_analysis("boss", "b", {"score": True}, review_floor=70)
    assert tmp_db.get_job("boss", "b")["status"] == "discovered"   # bool 是 int 子类，不当分数


def test_jd_stats(tmp_db):
    """JD 覆盖统计：空库 / 有 JD / 无 JD / 空白串不算有 / 疑似残缺单列。"""
    assert tmp_db.get_jd_stats() == {
        "total": 0, "with_jd": 0, "without_jd": 0, "thin": 0, "analyzed": 0, "coverage": 0.0,
    }
    tmp_db.upsert_jobs([_job(job_id="a"), _job(job_id="b"), _job(job_id="c"), _job(job_id="d")])
    tmp_db.save_jd("boss", "a", "岗位职责：\n" + "负责前端开发与维护。" * 12)   # 正常 JD
    tmp_db.save_jd("boss", "b", "太短")                    # 疑似残缺（<100 字）
    tmp_db.save_jd("boss", "c", "   ")                     # 空白串＝未获取
    tmp_db.save_job_analysis("boss", "a", {"score": 80})
    s = tmp_db.get_jd_stats()
    assert s["total"] == 4
    assert s["with_jd"] == 2 and s["without_jd"] == 2      # c 的空白串与 d 的空值都算未获取
    assert s["thin"] == 1
    assert s["analyzed"] == 1
    assert s["coverage"] == 50.0


def test_jd_confirm(tmp_db):
    """人工确认闭环：确认后移出「疑似残缺」仍算「已获取」；重抓落库作废旧确认；
    清洗回写（reset_confirm=False）保留确认。"""
    tmp_db.upsert_jobs([_job(job_id="a"), _job(job_id="b")])
    tmp_db.save_jd("boss", "a", "短 JD 内容")               # <100 字 → 疑似残缺
    assert tmp_db.get_jd_stats()["thin"] == 1
    # 未知岗位返回 None
    assert tmp_db.set_jd_confirmed("boss", "nope", True) is None
    # 确认 → thin 归零，with_jd 不变（仍是已获取）
    job = tmp_db.set_jd_confirmed("boss", "a", True)
    assert job["jd_confirmed"] == 1
    s = tmp_db.get_jd_stats()
    assert s["thin"] == 0 and s["with_jd"] == 1
    # 重抓落库（默认 reset_confirm=True）→ 旧确认作废，回到疑似残缺
    tmp_db.save_jd("boss", "a", "重新抓到的还是短")
    assert tmp_db.get_job("boss", "a")["jd_confirmed"] == 0
    assert tmp_db.get_jd_stats()["thin"] == 1
    # 清洗回写（reset_confirm=False）→ 确认保留
    tmp_db.set_jd_confirmed("boss", "a", True)
    tmp_db.save_jd("boss", "a", "清洗后的短内容", reset_confirm=False)
    assert tmp_db.get_job("boss", "a")["jd_confirmed"] == 1
    assert tmp_db.get_jd_stats()["thin"] == 0
    # 撤销确认
    tmp_db.set_jd_confirmed("boss", "a", False)
    assert tmp_db.get_job("boss", "a")["jd_confirmed"] == 0
    assert tmp_db.get_jd_stats()["thin"] == 1


def _seed_legacy_triage(tmp_db, job_id, keep, reason="历史判定"):
    """粗筛写入函数已随 L1 接口族下线（2026-10-09），这里直接落 SQL 造历史数据，
    钉住用户裁定的那一半：列与已有判定保留，只是不再有写的入口。"""
    with tmp_db._conn() as c:
        c.execute("UPDATE seen_jobs SET triage_keep=?, triage_reason=?, triage_at=? "
                  "WHERE job_id=?", (keep, reason, 1_700_000_000, job_id))


def test_legacy_triage_survives_rescrape(tmp_db):
    """重抓同一岗位走 upsert 的 UPDATE 分支，历史粗筛判定不能被列表落库冲掉。"""
    tmp_db.upsert_jobs([_job()])
    _seed_legacy_triage(tmp_db, "abc123", 0, "方向无关")
    tmp_db.upsert_jobs([_job(title="前端工程师（改）")])
    j = tmp_db.get_job("boss", "abc123")
    assert j["title"] == "前端工程师（改）"
    assert j["triage_keep"] == 0 and j["triage_reason"] == "方向无关"
    assert j["status"] == "discovered"          # keep=0 的历史岗不被动状态


def test_init_db_backfills_legacy_triage_kept(tmp_db):
    """启动回填只兜老库残留：keep=1 却仍停在 discovered 的升入评估列，再跑一遍不抖动。
    新的升档入口是 AI 分析分（test_analysis_auto_promotes_reviewing），不再依赖粗筛。"""
    tmp_db.upsert_jobs([_job()])
    _seed_legacy_triage(tmp_db, "abc123", 1)
    tmp_db.init_db()
    assert tmp_db.get_job("boss", "abc123")["status"] == "reviewing"
    tmp_db.init_db()
    assert tmp_db.get_job("boss", "abc123")["status"] == "reviewing"
