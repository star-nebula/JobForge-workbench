"""db 层回环测试（jobs / profile / resume_versions / scrape_log），用 tmp 库不动真实 jobs.db。"""
import pytest

import db


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
