"""server 层纯函数测试：密钥脱敏 + 模型配置选型 + 批量分析预检 + JD 确认端点 + L0 城市门槛
+ AI 分析队列门禁 + restart 三重判定 + 抓取子进程输出分支
（db/llm mock，不碰真实库、绝不触发真实 JD 抓取）。"""
import subprocess
import sys
import threading
import time

import pytest
from fastapi import HTTPException

from jobforge import llm, server
from jobforge.server import (AnalyzeBatchReq, AnalyzeReq, CrawlReq, JdConfirmReq, PipelineReq, RetryTarget,
                             ScrapeFromResumeReq, _get_llm_config, _mask_key)


def test_mask_key():
    assert _mask_key("sk-abcdef123456") == "****3456"
    assert _mask_key("abc") == "****"
    assert _mask_key("") == ""


def test_jd_confirm_endpoint(monkeypatch):
    """确认端点透传 confirmed 到 db 层；岗位不存在时 404。"""
    seen = {}
    monkeypatch.setattr(server.db, "set_jd_confirmed",
                        lambda p, j, c: seen.update(args=(p, j, c)) or {"job_id": j, "jd_confirmed": 1 if c else 0})
    job = server.confirm_job_jd("boss", "x1", JdConfirmReq())
    assert job["jd_confirmed"] == 1 and seen["args"] == ("boss", "x1", True)
    job = server.confirm_job_jd("boss", "x1", JdConfirmReq(confirmed=False))
    assert job["jd_confirmed"] == 0 and seen["args"] == ("boss", "x1", False)

    monkeypatch.setattr(server.db, "set_jd_confirmed", lambda p, j, c: None)
    with pytest.raises(HTTPException) as ei:
        server.confirm_job_jd("boss", "ghost", JdConfirmReq())
    assert ei.value.status_code == 404


@pytest.fixture
def llm_settings(monkeypatch):
    configs = [
        {"id": "a", "name": "DeepSeek", "base_url": "https://x/v1", "model": "m1", "api_key": "k1"},
        {"id": "b", "name": "Qwen", "base_url": "https://y/v1", "model": "m2", "api_key": "k2"},
    ]
    monkeypatch.setattr(server.db, "get_app_settings",
                        lambda: {"llm": {"configs": configs, "active_id": "b"}})
    return configs


def test_get_llm_config_active_first(llm_settings):
    assert _get_llm_config()["id"] == "b"


def test_get_llm_config_explicit_id(llm_settings):
    assert _get_llm_config("a")["id"] == "a"


def test_get_llm_config_unknown_id_falls_back_to_active(llm_settings):
    assert _get_llm_config("不存在的")["id"] == "b"


def test_get_llm_config_empty(monkeypatch):
    monkeypatch.setattr(server.db, "get_app_settings", lambda: {})
    assert _get_llm_config() is None


def test_analyze_batch_preflight_rejects_bad_config(monkeypatch):
    """批量启动前必须 ping 通模型，配置不可用当场拦下（2026-09-26 假配置空转事故的回归）。"""
    monkeypatch.setattr(server, "_get_llm_config",
                        lambda mid=None: {"base_url": "https://x/v1", "model": "m"})
    monkeypatch.setattr(server.llm, "chat",
                        lambda *a, **k: (_ for _ in ()).throw(llm.LLMError("HTTP 401")))
    r = server.start_analyze_batch(AnalyzeBatchReq())
    assert r["ok"] is False and "配置不可用" in r["error"]


def test_analyze_batch_preflight_rejects_native_not_ready(monkeypatch):
    """原生通道预检的适用范围＝「本次真的会去抓 JD」（2026-09-27 事故回归）。

    判据是队列里的 need_jd，不是按钮种类：缺 JD 的岗不真抓就没法分析，通道没就绪就得
    当场拦下，否则逐个走「原生失败→CDP 兜底被反爬→直连失败」全程白烧；
    反过来队列里每个岗都已有 JD 时，抓取环节走缓存短路，Chrome 关着照样能跑。
    """
    monkeypatch.setattr(server, "_get_llm_config",
                        lambda mid=None: {"base_url": "https://x/v1", "model": "m"})
    monkeypatch.setattr(server.llm, "chat", lambda *a, **k: "pong")
    monkeypatch.setattr(server, "_native_channel_status",
                        lambda: {"ok": False, "chrome_found": False, "chrome_ready": False,
                                 "error": "未找到 Chrome"})
    monkeypatch.setattr(server.db, "get_profile", lambda: {"data": {"city": "上海"}})
    monkeypatch.setattr(server.db, "list_jobs",
                        lambda status=None: [{"platform": "boss", "job_id": "j1", "city": "上海"}])
    r = server.start_analyze_batch(AnalyzeBatchReq(retry=[{"platform": "boss", "job_id": "j1"}]))
    assert r["ok"] is False and "原生通道未就绪" in r["error"]
    r = server.start_analyze_batch(AnalyzeBatchReq())
    assert r["ok"] is False and "原生通道未就绪" in r["error"], "缺 JD 的常规批次同样要预检"

    monkeypatch.setattr(server.db, "list_jobs",
                        lambda status=None: [{"platform": "boss", "job_id": "j1",
                                              "city": "上海", "jd_text": "正文"}])
    monkeypatch.setattr(server, "_ensure_hud", lambda: None)
    monkeypatch.setattr(server, "_progress_start", lambda *a, **k: True)
    monkeypatch.setattr(server, "_progress_update", lambda **k: None)
    monkeypatch.setattr(server, "_batch_worker",
                        lambda jobs, cfg, cancel, mode="fetch_and_analyze": None)
    r = server.start_analyze_batch(AnalyzeBatchReq())
    assert r["ok"] is True and r["started"] is True, f"全有 JD 的批次不碰键鼠，不该被 Chrome 拦住：{r}"
    server._batch.update({"running": False, "stop": False, "total": 0, "done": 0, "ok": 0,
                          "failed": 0, "current": "", "errors": [], "failed_jobs": [],
                          "stage": "", "pipeline": False, "note": ""})


def test_analyze_batch_no_jobs_no_start(monkeypatch):
    monkeypatch.setattr(server, "_get_llm_config",
                        lambda mid=None: {"base_url": "https://x/v1", "model": "m"})
    monkeypatch.setattr(server.llm, "chat", lambda *a, **k: "pong")
    monkeypatch.setattr(server, "_native_channel_status",
                        lambda: {"ok": True, "chrome_found": True, "chrome_ready": True, "throttle_sec": 0})
    monkeypatch.setattr(server.db, "list_jobs", lambda status=None: [])
    r = server.start_analyze_batch(AnalyzeBatchReq())
    assert r["ok"] is True and r["started"] is False


def test_batch_stop_kills_running_procs(monkeypatch):
    """停止必须立即 kill 在跑的子进程——旧实现只置标志位，卡在 150s 子进程里
    时点「确认」要等几分钟才停（2026-09-27 用户实测）。"""
    killed = []

    class FakeProc:
        def kill(self):
            killed.append(1)

    server._batch.update({"running": True, "stop": False})
    server._batch_cancel.clear()
    with server._batch_proc_lock:
        p = FakeProc()
        server._batch_procs.add(p)
    try:
        r = server.analyze_batch_stop()
        assert r["ok"] is True and r["killed_procs"] == 1
        assert killed == [1]                 # 子进程被 kill
        assert server._batch["stop"] is True  # 标志位同时置位
        assert server._batch_cancel.is_set()  # Event 置位（worker 边界检查）
    finally:
        with server._batch_proc_lock:
            server._batch_procs.discard(p)
        server._batch_cancel.clear()
        server._batch.update({"running": False, "stop": False})   # 复位全局态，别污染后续用例


# ---------- L0 城市门槛（2026-09-30 用户裁定 A：异地岗保留数据但不进列表/队列） ----------

class _NullScope:
    """替掉 _progress_scope：真作用域会拉起悬浮窗进程，单测里不能碰。"""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def set(self, **kw):
        pass


def _stub_crawl(monkeypatch, city):
    monkeypatch.setattr(server.db, "get_profile", lambda: {"data": {"city": city}})
    monkeypatch.setattr(server, "_progress_scope", lambda *a, **k: _NullScope())
    seen = {}
    monkeypatch.setattr(server.spider, "crawl",
                        lambda plat, q, c, page, mock: seen.update(city=c) or
                        {"jobs": [], "source": "real", "platform": "boss"})
    return seen


def test_crawl_default_city_follows_profile(monkeypatch):
    """抓取默认城市跟随 profile.city（旧版缺省「全国」，异地岗就是从这儿进来的）。"""
    seen = _stub_crawl(monkeypatch, "上海")
    server.crawl_jobs(CrawlReq(query="前端"))
    assert seen["city"] == "上海"


def test_crawl_explicit_city_still_overrides(monkeypatch):
    seen = _stub_crawl(monkeypatch, "上海")
    server.crawl_jobs(CrawlReq(query="前端", city="北京"))
    assert seen["city"] == "北京"


def test_crawl_falls_back_to_nationwide_without_profile_city(monkeypatch):
    seen = _stub_crawl(monkeypatch, "")
    server.crawl_jobs(CrawlReq(query="前端"))
    assert seen["city"] == "全国"


def test_jobs_default_hides_other_cities(monkeypatch):
    rows = [{"platform": "boss", "job_id": "sh", "city": "上海·闵行区"},
            {"platform": "boss", "job_id": "bj", "city": "北京·朝阳区"}]
    monkeypatch.setattr(server.db, "list_jobs", lambda status=None: [dict(r) for r in rows])
    monkeypatch.setattr(server.db, "get_profile", lambda: {"data": {"city": "上海"}})
    d = server.list_seen_jobs()
    assert [j["job_id"] for j in d["jobs"]] == ["sh"]
    assert d["expected_city"] == "上海" and d["other_city_count"] == 1
    d = server.list_seen_jobs(all_cities=True)
    assert [j["job_id"] for j in d["jobs"]] == ["sh", "bj"]
    assert [j["city_ok"] for j in d["jobs"]] == [True, False]


def test_jobs_no_city_gate_without_profile_city(monkeypatch):
    """profile 没填城市 = 不限城市，不能再偷偷按某地过滤。"""
    monkeypatch.setattr(server.db, "list_jobs",
                        lambda status=None: [{"platform": "boss", "job_id": "bj", "city": "北京"}])
    monkeypatch.setattr(server.db, "get_profile", lambda: {"data": {}})
    d = server.list_seen_jobs()
    assert len(d["jobs"]) == 1 and d["other_city_count"] == 0


def test_analyze_batch_skips_other_cities(monkeypatch):
    """异地岗不进 AI 分析队列：全部被门槛滤掉 → 直接不开跑（不开线程、不拉悬浮窗）。"""
    monkeypatch.setattr(server, "_get_llm_config",
                        lambda mid=None: {"base_url": "https://x/v1", "model": "m"})
    monkeypatch.setattr(server.llm, "chat", lambda *a, **k: "pong")
    monkeypatch.setattr(server, "_native_channel_status",
                        lambda: {"ok": True, "chrome_found": True, "chrome_ready": True})
    monkeypatch.setattr(server.db, "get_profile", lambda: {"data": {"city": "上海"}})
    monkeypatch.setattr(server.db, "list_jobs",
                        lambda status=None: [{"platform": "boss", "job_id": "bj", "city": "北京"}])
    r = server.start_analyze_batch(AnalyzeBatchReq())
    assert r["ok"] is True and r["started"] is False and r["skipped_other_city"] == 1


# ---------- AI 分析队列门禁（2026-10-09：L1 粗筛下线；JD 门同日加了又撤，见下方注释） ----------
_QROWS = [{"platform": "boss", "job_id": "j1", "city": "上海", "jd_text": "JD 正文"},
          {"platform": "boss", "job_id": "j2", "city": "上海", "jd_text": "JD 正文",
           "llm_analysis": {"score": 9}},
          {"platform": "boss", "job_id": "j3", "city": "上海", "jd_text": "JD 正文",
           "triage_keep": 0},
          {"platform": "boss", "job_id": "j4", "city": "上海"},
          {"platform": "boss", "job_id": "j5", "city": "上海", "llm_analysis": {"score": 3}},
          {"platform": "boss", "job_id": "bj", "city": "北京", "jd_text": "JD 正文"}]


def _ids(rows):
    return [r["job_id"] for r in rows]


def test_analyze_queue_gates_only_city_and_analyzed(monkeypatch):
    """只剩两级闸门，且历史 triage_keep=0 的老岗不再被拦：
    粗筛门若还留着，这批岗会被判成「尚未粗筛」，手动批量分析直接空转。"""
    monkeypatch.setattr(server.db, "get_profile", lambda: {"data": {"city": "上海"}})
    jobs, stats = server._analyze_queue([dict(r) for r in _QROWS], AnalyzeBatchReq())
    assert _ids(jobs) == ["j1", "j3", "j4"]
    assert stats == {"skipped_other_city": 1, "skipped_analyzed": 2, "need_jd": 1}


def test_analyze_queue_keeps_jobs_missing_jd(monkeypatch):
    """缺 JD 的未分析岗必须留在分析队列里（先抓 JD 再分析）。

    2026-10-09 晚曾把缺 JD 的一律挡下，理由是「点分析不等于重抓全库」；代价是
    智能抓取只抓到列表、JD 没抓到的那批岗在岗位市场再无入口补齐，只能重跑整条流水线
    （抓列表约 1 分钟/城）。正确口径是如实播报代价（need_jd）而不是砍功能：
    已有 JD 的岗在抓取环节走缓存短路，不会因为队列里混了缺 JD 的就重复花额度。
    """
    monkeypatch.setattr(server.db, "get_profile", lambda: {"data": {"city": "上海"}})
    rows = [{"platform": "boss", "job_id": "has_jd", "city": "上海", "jd_text": "正文"},
            {"platform": "boss", "job_id": "no_jd", "city": "上海"},
            {"platform": "boss", "job_id": "blank_jd", "city": "上海", "jd_text": "   "},
            {"platform": "boss", "job_id": "done", "city": "上海", "jd_text": "正文",
             "llm_analysis": {"score": 7}},
            {"platform": "boss", "job_id": "done_no_jd", "city": "上海",
             "llm_analysis": {"score": 2}}]
    jobs, stats = server._analyze_queue(rows, AnalyzeBatchReq())
    assert _ids(jobs) == ["has_jd", "no_jd", "blank_jd"], "缺 JD 的未分析岗要进队列"
    assert stats["need_jd"] == 2, stats
    assert stats["skipped_analyzed"] == 2, "已有分析结果的（含「已分析但缺 JD」）默认不重做"


def test_analyze_queue_force_reincludes_analyzed_jobs(monkeypatch):
    """force=连已分析的也重做：「已分析但缺 JD」的岗这时才进队列（没正文没法重分析，只能真抓）。"""
    monkeypatch.setattr(server.db, "get_profile", lambda: {"data": {"city": "上海"}})
    rows = [{"platform": "boss", "job_id": "has_jd", "city": "上海", "jd_text": "正文"},
            {"platform": "boss", "job_id": "no_jd", "city": "上海", "llm_analysis": {"score": 1}}]
    jobs, stats = server._analyze_queue(rows, AnalyzeBatchReq(force=True))
    assert _ids(jobs) == ["has_jd", "no_jd"]
    assert stats["need_jd"] == 1, stats


def test_analyze_queue_need_jd_counted_after_limit(monkeypatch):
    """need_jd 要说的是「本次真会抓几个」，limit 截断之后再数，否则确认框里的数字是虚的。"""
    monkeypatch.setattr(server.db, "get_profile", lambda: {"data": {"city": "上海"}})
    rows = [{"platform": "boss", "job_id": f"a{n}", "city": "上海"} for n in range(4)]
    jobs, stats = server._analyze_queue(rows, AnalyzeBatchReq(limit=2))
    assert len(jobs) == 2 and stats["need_jd"] == 2, stats


def test_analyze_queue_force_and_limit(monkeypatch):
    monkeypatch.setattr(server.db, "get_profile", lambda: {"data": {"city": "上海"}})
    jobs, _ = server._analyze_queue([dict(r) for r in _QROWS], AnalyzeBatchReq(force=True))
    assert _ids(jobs) == ["j1", "j2", "j3", "j4", "j5"]
    jobs, _ = server._analyze_queue([dict(r) for r in _QROWS],
                                    AnalyzeBatchReq(force=True, limit=2))
    assert _ids(jobs) == ["j1", "j2"]


def test_analyze_queue_no_city_gate_without_profile_city(monkeypatch):
    """profile 没填城市 → 不限城市，北京岗照样进队列。"""
    monkeypatch.setattr(server.db, "get_profile", lambda: {"data": {}})
    jobs, stats = server._analyze_queue([dict(r) for r in _QROWS], AnalyzeBatchReq())
    assert _ids(jobs) == ["j1", "j3", "j4", "bj"] and stats["skipped_other_city"] == 0


def test_analyze_queue_only_scopes_to_visible_set(monkeypatch):
    """only＝岗位市场「屏幕上看得见的那批」。队列只能在它内部排：
    把筛掉的岗捎进队列，用户看到「本次 4 个」却实际点了 4 个的 BOSS 额度，
    而其中 2 个是他刚刚明确筛掉不要看的。"""
    monkeypatch.setattr(server.db, "get_profile", lambda: {"data": {"city": "上海"}})
    only = [RetryTarget(job_id="j1"), RetryTarget(job_id="j4")]
    jobs, stats = server._analyze_queue([dict(r) for r in _QROWS], AnalyzeBatchReq(only=only))
    assert _ids(jobs) == ["j1", "j4"]
    # 白名单外的岗不计入任何闸门：计进「跳过 2 个已分析」等于把用户筛掉的东西当成被拒绝
    assert stats == {"skipped_other_city": 0, "skipped_analyzed": 0, "need_jd": 1}, stats
    jobs, _ = server._analyze_queue([dict(r) for r in _QROWS],
                                    AnalyzeBatchReq(only=only, limit=1))
    assert _ids(jobs) == ["j1"], "limit 要在收窄之后再截，否则截的是全库"


def test_analyze_queue_only_does_not_open_the_gates(monkeypatch):
    """白名单是「缩小候选集」，不是「放行」：集合里的已分析岗、异地岗照样被两道闸拦下。
    闸门一松就退化成「点一次把选中的岗全重跑一遍」，那正是 2026-10-09 撤回的老 bug。"""
    monkeypatch.setattr(server.db, "get_profile", lambda: {"data": {"city": "上海"}})
    only = [RetryTarget(job_id="j2"), RetryTarget(job_id="bj")]   # 已分析的 + 异地的
    jobs, stats = server._analyze_queue([dict(r) for r in _QROWS], AnalyzeBatchReq(only=only))
    assert jobs == []
    assert stats == {"skipped_other_city": 1, "skipped_analyzed": 1, "need_jd": 0}, stats


def test_batch_note_announces_filtered_scope(analyze_ready):
    """进度条上那句「按列表筛选 N 个」是唯一的痕迹：同一颗按钮，筛过和没筛过跑的
    根本不是一批岗，不写出来就只能靠用户记住自己十分钟前点过哪个 pill。"""
    analyze_ready(_QROWS)
    r = server.start_analyze_batch(AnalyzeBatchReq(only=[RetryTarget(job_id="j4"),
                                                         RetryTarget(job_id="j2")]))
    assert r["started"] is True, r
    assert server._batch["note"] == "按列表筛选 1 个 · 其中 1 个要先抓 JD", server._batch["note"]
    server._batch["running"] = False   # 空壳 worker 不收尾，不复位的话第二次压根没启动
    r = server.start_analyze_batch(AnalyzeBatchReq())
    assert r["started"] is True, r
    assert "按列表筛选" not in server._batch["note"], "没收窄候选集却播报了筛选，全库看起来像筛过的"


@pytest.fixture
def analyze_ready(monkeypatch):
    """预检全绿 + 悬浮窗与 worker 换成空壳：真 worker 会去抓 JD（接管键鼠、烧风控）。"""
    monkeypatch.setattr(server, "_get_llm_config",
                        lambda mid=None: {"base_url": "https://x/v1", "model": "m"})
    monkeypatch.setattr(server.llm, "chat", lambda *a, **k: "pong")
    monkeypatch.setattr(server, "_native_channel_status",
                        lambda: {"ok": True, "chrome_found": True, "chrome_ready": True})
    monkeypatch.setattr(server.db, "get_profile", lambda: {"data": {"city": "上海"}})
    monkeypatch.setattr(server, "_ensure_hud", lambda: None)
    monkeypatch.setattr(server, "_progress_start", lambda *a, **k: True)
    monkeypatch.setattr(server, "_progress_update", lambda **k: None)
    monkeypatch.setattr(server, "_batch_worker",
                        lambda jobs, cfg, cancel, mode="fetch_and_analyze": None)

    def set_rows(rows):
        monkeypatch.setattr(server.db, "list_jobs",
                            lambda status=None: [dict(r) for r in rows])
    yield set_rows
    server._batch.update({"running": False, "stop": False, "total": 0, "done": 0,
                          "ok": 0, "failed": 0, "current": "", "errors": [],
                          "failed_jobs": [], "finished_at": 0.0})


def test_analyze_batch_starts_after_triage_retired(analyze_ready):
    analyze_ready(_QROWS)
    r = server.start_analyze_batch(AnalyzeBatchReq())
    assert r["started"] is True and r["total"] == 3        # j1 + 历史 drop 的 j3 + 缺 JD 的 j4
    assert (r["skipped_other_city"], r["skipped_analyzed"], r["need_jd"]) == (1, 2, 1)


def test_analyze_batch_empty_queue_explains_why(analyze_ready):
    """队列空时必须说清「为什么没跑起来」，并把便宜的那一步（force 重跑）指出来。"""
    analyze_ready([{"platform": "boss", "job_id": "j2", "city": "上海",
                    "llm_analysis": {"score": 9}}])
    r = server.start_analyze_batch(AnalyzeBatchReq())
    assert r["ok"] is True and r["started"] is False
    assert "已分析过" in r["message"] and "粗筛" not in r["message"]


def test_analyze_batch_missing_jd_runs_fetch_and_analyze(analyze_ready, monkeypatch):
    """队列里有缺 JD 的岗 → 整批走 fetch_and_analyze：抓完立即分析，一次点击补齐。
    这是岗位市场「一键抓取并分析」的落点，砍掉它就只能重跑整条智能抓取。"""
    called = {}
    monkeypatch.setattr(server, "_batch_worker",
                        lambda jobs, cfg, cancel, mode="fetch_and_analyze": called.update(
                            mode=mode, ids=[j["job_id"] for j in jobs]))
    analyze_ready([{"platform": "boss", "job_id": "has_jd", "city": "上海", "jd_text": "正文"},
                   {"platform": "boss", "job_id": "no_jd", "city": "上海"}])
    r = server.start_analyze_batch(AnalyzeBatchReq())
    assert r["started"] is True and r["need_jd"] == 1, r
    assert called["mode"] == "fetch_and_analyze", called
    assert called["ids"] == ["has_jd", "no_jd"], "已有 JD 的岗不能被挤出队列"


def test_analyze_batch_all_cached_jd_is_analyze_only(analyze_ready, monkeypatch):
    """队列里 JD 都在库 → analyze_only：不接管键鼠，Chrome 没开也能跑。"""
    called = {}
    monkeypatch.setattr(server, "_batch_worker",
                        lambda jobs, cfg, cancel, mode="fetch_and_analyze": called.update(mode=mode))
    monkeypatch.setattr(server, "_native_channel_status",
                        lambda: {"ok": False, "chrome_found": False, "chrome_ready": False})
    analyze_ready([{"platform": "boss", "job_id": "j1", "city": "上海", "jd_text": "正文"}])
    r = server.start_analyze_batch(AnalyzeBatchReq())
    assert r["started"] is True and r["need_jd"] == 0, r
    assert called["mode"] == "analyze_only", called


def test_analyze_batch_retry_still_fetches_missing_jd(analyze_ready, monkeypatch):
    """补抓点名的岗里缺 JD 的必须真抓（fetch_and_analyze）；已拿到 JD 的则只重做分析，
    不再让「补抓」顺手把额度重花一遍。"""
    called = {}
    monkeypatch.setattr(server, "_batch_worker",
                        lambda jobs, cfg, cancel, mode="fetch_and_analyze": called.update(mode=mode))
    analyze_ready([{"platform": "boss", "job_id": "j1", "city": "上海"}])
    r = server.start_analyze_batch(AnalyzeBatchReq(retry=[{"platform": "boss", "job_id": "j1"}]))
    assert r["started"] is True and called["mode"] == "fetch_and_analyze", called
    server._batch["running"] = False   # worker 是空壳，不会自己收尾

    called.clear()
    analyze_ready([{"platform": "boss", "job_id": "j1", "city": "上海", "jd_text": "正文"}])
    r = server.start_analyze_batch(AnalyzeBatchReq(retry=[{"platform": "boss", "job_id": "j1"}]))
    assert r["started"] is True and called["mode"] == "analyze_only", called


def test_batch_run_publishes_mode(monkeypatch):
    """状态接口必须带出 mode：一批到底碰没碰 BOSS（接管键鼠、花额度）是用户最该看见的差别，
    chip 写死「批量分析」就等于把它藏起来（2026-10-09「点分析却在重抓全库」的观感来源）。"""
    gate = type("Gate", (), {"stopped": staticmethod(lambda: False)})
    monkeypatch.setattr(server, "fetch_gate", gate)
    for mode in ("fetch_and_analyze", "analyze_only"):
        server._batch["mode"] = ""
        server._batch_run([], {}, threading.Event(), mode=mode)   # 空队列：只验状态，不跑任何岗位
        assert server._batch["mode"] == mode


def test_batch_start_resets_finish_clock(analyze_ready):
    """_batch 是进程内字典：新批次若留着上一批的结束时间，刚跑完的结果一打开就被判成过期，
    「一键补抓」直接点不动——开始时必须把计时归零到本次。"""
    analyze_ready(_QROWS)
    server._batch["finished_at"] = time.time() - 6 * 3600      # 上一批是六小时前
    r = server.start_analyze_batch(AnalyzeBatchReq())
    assert r["started"] is True, r
    assert server._batch["finished_at"] > time.time() - 60, "新批次继承了旧批次的结束时间"


def test_batch_worker_stamps_finish_time(monkeypatch):
    """收尾必须盖章：前端判「这份失败清单还作不作数」只看这一个时间戳。
    从开始就计时、结束时刷新，worker 万一没走到收尾也仍有据可判（不会退回 0=无从判断）。

    不用 `analyze_ready`：那个 fixture 把 `_batch_worker` 换成空壳了，这里要验的正是真的收尾。
    """
    monkeypatch.setattr(server, "_batch_run", lambda *a, **k: None)
    monkeypatch.setattr(server, "_progress_finish", lambda **k: None)
    server._batch["running"] = True
    server._batch["finished_at"] = 0.0
    server._batch_worker([], {}, threading.Event())
    assert server._batch["finished_at"] > time.time() - 5, "收尾没时间戳，结果面板判不出新旧"
    assert server._batch["running"] is False
    server._batch["finished_at"] = 0.0


# ---------- run.bat restart：结束旧实例前的三重判定 ----------
_NETSTAT = """  Proto  Local Address          Foreign Address        State           PID\r
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1156\r
  TCP    127.0.0.1:8080         0.0.0.0:0              LISTENING       67644\r
  TCP    127.0.0.1:8080         127.0.0.1:5204         TIME_WAIT       0\r
  TCP    127.0.0.1:18080        0.0.0.0:0              LISTENING       99999\r
"""


def _fake_netstat(monkeypatch, out=_NETSTAT, raise_exc=False):
    def run(cmd, **kw):
        if raise_exc:
            raise OSError("netstat 不可用")
        return subprocess.CompletedProcess(cmd, 0, out, "")
    monkeypatch.setattr(server.subprocess, "run", run)


def test_port_owner_pid_reads_only_listening_row(monkeypatch):
    _fake_netstat(monkeypatch)
    assert server._port_owner_pid(8080) == 67644      # TIME_WAIT 那行不算


def test_port_owner_pid_no_prefix_false_hit(monkeypatch):
    """18080 不能被当成 8080（端口串味等于杀错进程）。"""
    _fake_netstat(monkeypatch)
    assert server._port_owner_pid(18080) == 99999
    assert server._port_owner_pid(8090) is None


def test_port_owner_pid_swallows_netstat_failure(monkeypatch):
    _fake_netstat(monkeypatch, raise_exc=True)
    assert server._port_owner_pid(8080) is None       # 查不到 → 后面拒绝杀


def test_kill_plan_refuses_unidentified_responder():
    """认不出是 JobForge 就绝不动手——端口上可能是别的程序。"""
    ok, why = server._kill_plan(None, 67644, [])
    assert ok is False and "认不出" in why
    ok, why = server._kill_plan({"app": "other"}, 67644, [])
    assert ok is False


def test_kill_plan_refuses_pid_mismatch():
    """whoami 自报的 PID 与端口占用者不一致 → 说明中间有人换过手，拒绝。"""
    ok, why = server._kill_plan({"app": "jobforge", "pid": 111}, 222, [])
    assert ok is False and "对不上" in why
    ok, why = server._kill_plan({"app": "jobforge", "pid": 111}, None, [])
    assert ok is False and "对不上" in why


def test_kill_plan_refuses_while_scraping():
    """正在抓 JD 时结束会留下半截子进程与键鼠接管，必须先让人停手。"""
    ok, why = server._kill_plan({"app": "jobforge", "pid": 111}, 111, ["批量分析"])
    assert ok is False and "批量分析" in why


def test_kill_plan_allows_clean_restart():
    ok, why = server._kill_plan({"app": "jobforge", "pid": 111}, 111, [])
    assert ok is True and "111" in why


def test_instance_identity_accepts_legacy_platforms(monkeypatch):
    """正在跑的旧实例往往还没装 /api/whoami（404）——退回认 /api/platforms 的结构，
    否则「一键重启」对最需要它的场景（重启旧代码）永远说认不出。"""
    def fake(port, path):
        return None if path == "/api/whoami" else {"platforms": [{"key": "boss"}]}
    monkeypatch.setattr(server, "_json_get", fake)
    assert server._instance_identity(8080) == {"app": "jobforge"}


def test_instance_identity_rejects_foreign_service(monkeypatch):
    def fake(port, path):
        return {"app": "someone-else"} if path == "/api/whoami" else {}
    monkeypatch.setattr(server, "_json_get", fake)
    assert server._instance_identity(8080) is None


def test_stop_existing_refuses_without_taskkill(monkeypatch):
    """三重判定不过 → 一条 taskkill 都不发（测试里也绝不允许真发）。"""
    calls = []
    monkeypatch.setattr(server, "_instance_identity", lambda p: {"app": "someone-else"})
    monkeypatch.setattr(server, "_port_owner_pid", lambda p: 67644)
    monkeypatch.setattr(server, "_busy_tasks", lambda p: [])
    monkeypatch.setattr(server.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    assert server._stop_existing(8080) is False
    assert calls == []


def test_stop_existing_kills_netstat_pid_and_waits_release(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_instance_identity", lambda p: {"app": "jobforge"})
    monkeypatch.setattr(server, "_port_owner_pid", lambda p: 67644)
    monkeypatch.setattr(server, "_busy_tasks", lambda p: [])
    monkeypatch.setattr(server.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(server, "_http_get", lambda port, path: (None, b""))   # 端口已释放
    assert server._stop_existing(8080) is True
    assert calls == [["taskkill", "/PID", "67644", "/T", "/F"]]


def test_parse_argv_port_forms_and_restart():
    """两种 --port 写法都要认（`--port=8090` 一度被改坏，静默回落到 8080 = 第二个实例
    抢正式端口）。"""
    assert server._parse_argv([]) == (8080, False)
    assert server._parse_argv(["--port", "8090"]) == (8090, False)
    assert server._parse_argv(["--port=8090"]) == (8090, False)
    assert server._parse_argv(["--restart"]) == (8080, True)
    assert server._parse_argv(["--port=8091", "--restart"]) == (8091, True)


# ---------- 抓取子进程输出契约：真超时 / 无输出 / 非 JSON 要分开报 ----------
def _child(code):
    return subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace")


@pytest.fixture
def gate_free(monkeypatch):
    """闸门置位状态由文件承载，单测里钉成「没暂停、没停止」，只验本函数的分支。"""
    monkeypatch.setattr(server.fetch_gate, "stopped", lambda: False)
    monkeypatch.setattr(server.fetch_gate, "paused", lambda: False)


def test_pausable_returns_json_and_no_note(gate_free):
    data, note = server._communicate_pausable(
        _child("print('noise'); import json; print(json.dumps({'ok': 1, 'jd': '中文'}))"),
        timeout=30)
    assert note == "" and data["jd"] == "中文"      # 只取最后一行（ensure_ascii=True，线上传 ASCII）


def test_pausable_distinguishes_no_output_from_timeout(gate_free):
    """修复前：这两种都返回 None，上层一律印「抓取超时（150s）」，
    把编码崩溃一类的真故障伪装成风控超时（2026-10-01 实测白烧两次键鼠）。"""
    p = _child("import sys; sys.stderr.write('UnicodeEncodeError: gbk codec, char \\\\ufffc'); sys.exit(1)")
    data, note = server._communicate_pausable(p, timeout=30)
    assert data is None
    assert "无输出" in note and "退出码 1" in note and "UnicodeEncodeError" in note

    data, note = server._communicate_pausable(_child("import time; time.sleep(5)"), timeout=0.3)
    assert data is None and "超时" in note and "无输出" not in note


def test_pausable_reports_non_json_output(gate_free):
    data, note = server._communicate_pausable(_child("print('Traceback: boom')"), timeout=30)
    assert data is None and "不是 JSON" in note and "Traceback" in note


def test_pausable_still_honours_stop(monkeypatch):
    """点「停止」时立即 kill 返回，不等满超时（停止秒级生效是既有约定）。"""
    monkeypatch.setattr(server.fetch_gate, "paused", lambda: False)
    monkeypatch.setattr(server.fetch_gate, "stopped", lambda: True)
    data, note = server._communicate_pausable(_child("import time; time.sleep(5)"), timeout=150)
    assert data is None and note == "已停止"




# ---------- A4：抓取词放开自由关键词（2026-10-03，后端 query 覆盖字段） ----------

def _stub_scrape(monkeypatch, city="上海"):
    """一条龙端点全 stub：crawl 不真抓、db 不碰真实库、进度作用域不拉悬浮窗。"""
    monkeypatch.setattr(server.db, "get_profile", lambda: {"data": {"city": city}})
    monkeypatch.setattr(server, "_progress_scope", lambda *a, **k: _NullScope())
    monkeypatch.setattr(server.db, "upsert_jobs", lambda jobs: [False] * len(jobs))
    monkeypatch.setattr(server.db, "add_scrape_log", lambda *a, **k: None)
    seen = {}
    monkeypatch.setattr(server.spider, "crawl",
                        lambda plat, q, c, page, mock: seen.update(query=q, city=c) or
                        {"jobs": [], "source": "real", "platform": "boss"})
    return seen


_RESUME = "李明远\n求职意向：高级前端工程师\n期望城市：上海\n技能专长：JavaScript React"


def test_scrape_query_override(monkeypatch):
    """显式自由关键词优先于简历推导（A4：UI 已有入口，同词反复抓的困局在这解开）。"""
    seen = _stub_scrape(monkeypatch)
    r = server.scrape_from_resume(
        ScrapeFromResumeReq(resume_text=_RESUME, query="Go 后端"))
    assert r["query_used"] == "Go 后端"
    assert seen["query"] == "Go 后端"


def test_scrape_query_blank_falls_back_to_resume(monkeypatch):
    """留空/纯空白 = 原行为：从简历解析意向岗位推导。"""
    seen = _stub_scrape(monkeypatch)
    r = server.scrape_from_resume(
        ScrapeFromResumeReq(resume_text=_RESUME, query="   "))
    assert r["query_used"] == "高级前端工程师"
    assert seen["query"] == "高级前端工程师"


# ---------- C/D 组：数据备份 + 消息截断诚实化（2026-10-03） ----------

def test_backup_roundtrip(tmp_path, monkeypatch):
    """备份 = jobs.db 热备 + messages.json 复制；清单能列出且带文件名。"""
    import sqlite3
    src = tmp_path / "src"; src.mkdir()
    c = sqlite3.connect(str(src / "jobs.db"))
    c.execute("CREATE TABLE t(x)"); c.commit(); c.close()
    (src / "messages.json").write_text('[]', encoding='utf-8')
    monkeypatch.setattr(server.paths, "data", lambda *p: str(src.joinpath(*p)))
    r = server.create_backup()
    assert r["ok"] is True
    ls = server.list_backups()
    assert ls["keep"] == server._BACKUP_KEEP
    assert ls["backups"] and ls["backups"][0]["name"] == r["name"]
    assert "jobs.db" in ls["backups"][0]["files"]
    assert "messages.json" in ls["backups"][0]["files"]


def test_backup_prunes_to_keep_limit(tmp_path, monkeypatch):
    """备份目录只保留最近 _BACKUP_KEEP 份（含新做的这份）。"""
    import pathlib
    import sqlite3
    src = tmp_path / "src"; src.mkdir()
    c = sqlite3.connect(str(src / "jobs.db"))
    c.execute("CREATE TABLE t(x)"); c.commit(); c.close()
    monkeypatch.setattr(server.paths, "data", lambda *p: str(src.joinpath(*p)))
    monkeypatch.setattr(server, "_BACKUP_KEEP", 2)
    bdir = pathlib.Path(src) / "backups"
    for n in ("backup-20260101-000001", "backup-20260102-000002", "backup-20260103-000003"):
        (bdir / n).mkdir(parents=True)
    assert server.create_backup()["ok"] is True
    names = sorted(d.name for d in bdir.iterdir() if d.is_dir())
    assert len(names) == 2 and "backup-20260101-000001" not in names


def test_messages_endpoint_reports_truncation(tmp_path, monkeypatch):
    """limit 静默截断改为如实返回 total/truncated（C 组体检条：后 100 条悄悄消失）。"""
    monkeypatch.setattr(server.db, "list_messages",
                        lambda limit=200: [{"msg_id": i} for i in range(min(limit, 3))])
    monkeypatch.setattr(server.db, "count_messages", lambda: 50)
    r = server.get_messages()
    assert r["total"] == 50 and len(r["messages"]) == 3 and r["truncated"] is True
    monkeypatch.setattr(server.db, "count_messages", lambda: 3)
    assert server.get_messages()["truncated"] is False


# ---------- 一键智能抓取（2026-10-08 收敛的主线）：抓列表 → 抓 JD → AI 分析，阶段边界停止 ----------

def _pipeline_cleanup():
    """流水线测试后的全局态复位（worker 在后台线程跑，monkeypatch 管不到 _batch/锁）。"""
    server._batch.update({"running": False, "stop": False, "total": 0, "done": 0,
                          "ok": 0, "failed": 0, "current": "", "errors": [],
                          "failed_jobs": [], "stage": "", "pipeline": False, "note": ""})
    if server._pipeline_lock.locked():
        server._pipeline_lock.release()


def _pipeline_env(monkeypatch, crawl_result=None, jd_fail_ids=None,
                  jd_timeout_ids=None, list_rows=None, new_jobs=None):
    """流水线全 stub：预检全绿、抓列表/抓 JD/AI 分析三段全部换成可断言的桩。
    返回 calls dict 供断言（crawl/jd/analyze 计数与被分析的 job_id）。"""
    monkeypatch.setattr(server, "_get_llm_config",
                        lambda mid=None: {"base_url": "https://x/v1", "model": "m"})
    monkeypatch.setattr(server.llm, "chat", lambda *a, **k: "pong")
    monkeypatch.setattr(server, "_native_channel_status",
                        lambda: {"ok": True, "chrome_found": True, "chrome_ready": True})
    monkeypatch.setattr(server.db, "get_profile",
                        lambda: {"data": {"city": "上海", "resume_text": _RESUME}})
    monkeypatch.setattr(server.db, "add_scrape_log", lambda *a, **k: None)
    monkeypatch.setattr(server, "_ensure_hud", lambda: None)
    monkeypatch.setattr(server, "_progress_start", lambda *a, **k: True)
    monkeypatch.setattr(server, "_progress_update", lambda **k: None)
    monkeypatch.setattr(server, "_progress_finish", lambda **k: None)
    monkeypatch.setattr(server, "_progress_scope", lambda *a, **k: _NullScope())
    calls = {"crawl": 0, "jd": 0, "analyze": 0, "analyzed_jobs": []}

    # 默认本次抓到 2 个新岗位（与真实 upsert 后的库状态一致：真实行一律带 status）
    new_jobs = new_jobs or [{"platform": "boss", "job_id": f"n{i}", "city": "上海",
                             "title": "AI工程师", "tags": ["Python"], "status": "discovered"}
                            for i in range(2)]
    rows = list_rows if list_rows is not None else []
    store = [dict(r) for r in rows]        # 模拟 seen_jobs 表：只装「已在库」的行
    for r in store:
        r.setdefault("status", "discovered")
    monkeypatch.setattr(server.db, "list_jobs",
                        lambda status=None: [dict(r) for r in store])

    def fake_upsert(jobs):
        """真实语义：库里没有的才算新岗并落库，已有的返回 False（不打新岗标）。
        （曾经这里一律返回 True，桩比真实输出乐观，「只抓新岗」类口径测不出来。）"""
        known = {r.get("job_id") for r in store}
        flags = [j.get("job_id") not in known for j in jobs]
        for j, is_new in zip(jobs, flags):
            if is_new:
                store.append(dict(j))
        return flags
    monkeypatch.setattr(server.db, "upsert_jobs", fake_upsert)

    def fake_get_job(platform, job_id):
        for r in store:
            if r.get("job_id") == job_id:
                return dict(r)
        return None
    monkeypatch.setattr(server.db, "get_job", fake_get_job)

    def fake_save_jd(platform, job_id, jd_text, reset_confirm=True):
        for r in store:
            if r.get("job_id") == job_id:
                r["jd_text"] = jd_text
        return True
    monkeypatch.setattr(server.db, "save_jd", fake_save_jd)

    def fake_save_analysis(platform, job_id, analysis, review_floor=None):
        calls["analyzed_jobs"].append(job_id)
        calls.setdefault("review_floor", []).append(review_floor)
        for r in store:
            if r.get("job_id") == job_id:
                r["llm_analysis"] = analysis
                if (review_floor is not None and isinstance(analysis.get("score"), (int, float))
                        and r.get("status", "discovered") == "discovered"
                        and analysis["score"] >= review_floor):
                    r["status"] = "reviewing"
        return True
    monkeypatch.setattr(server.db, "save_job_analysis", fake_save_analysis)

    def fake_crawl(plat, q, c, page, mock):
        calls["crawl"] += 1
        if crawl_result is not None:
            return crawl_result
        return {"jobs": new_jobs, "source": "real", "platform": "boss",
                "stats": {"total": len(new_jobs), "new": len(new_jobs), "seen": 0}}
    monkeypatch.setattr(server.spider, "crawl", fake_crawl)

    # JD 抓取：默认全部成功写回 JD 文本；jd_fail_ids 里的失败；jd_timeout_ids 里模拟 150s 软锁超时
    fail_ids = jd_fail_ids or set()
    timeout_ids = jd_timeout_ids or set()
    def fake_fetch_jd(job, refresh=False, cancel=None):
        calls["jd"] += 1
        if job["job_id"] in timeout_ids:
            return {"ok": False, "error": "抓取超时（150s）；直连亦未解析到 JD，可点下方链接在 BOSS 查看"}
        if job["job_id"] in fail_ids:
            return {"ok": False, "error": "风控"}
        fake_save_jd(job["platform"], job["job_id"], f"JD正文-{job['job_id']}")
        return {"ok": True}
    monkeypatch.setattr(server, "_fetch_jd_core", fake_fetch_jd)

    def fake_analyze(cfg2, pdata, job):
        calls["analyze"] += 1
        return {"verdict": "v", "score": 80, "strengths": [], "gaps": [], "advice": []}
    monkeypatch.setattr(server.llm, "analyze_match", fake_analyze)
    return calls


def _wait_pipeline_done(timeout_s=3.0, cleanup=True):
    """等流水线后台线程跑完（全 stub 毫秒级；跑不完即超时失败）。
    返回结束时 _batch 的快照（cleanup 会清 errors，断言用快照）。
    cleanup=False 时不释放流水线锁——用来检验「锁由生产代码自己归还」。"""
    import time as _t
    deadline = _t.time() + timeout_s
    while _t.time() < deadline:
        if not server._batch["running"]:
            snap = {"errors": list(server._batch["errors"]), "note": server._batch["note"],
                    "failed_jobs": [dict(r) for r in server._batch["failed_jobs"]]}
            if cleanup:
                _pipeline_cleanup()
            return snap
        _t.sleep(0.02)
    if cleanup:
        _pipeline_cleanup()
    raise AssertionError("流水线线程 3 秒内未结束")


def test_pipeline_happy_path(monkeypatch):
    """快乐路径：抓列表 → 新岗 JD 全抓 → 一次性分析，分析结果落库。"""
    env = _pipeline_env(monkeypatch)
    try:
        r = server.start_pipeline(PipelineReq())
        assert r["ok"] is True and r["started"] is True
        snap = _wait_pipeline_done()
        assert env["crawl"] == 1 and env["jd"] == 2 and env["analyze"] == 2
        assert len(env["analyzed_jobs"]) == 2
        assert server._batch["pipeline"] is False and server._batch["stage"] == ""
        assert not snap["errors"]
    finally:
        _pipeline_cleanup()


def test_pipeline_jd_scope_covers_new_and_jd_less_old_jobs(monkeypatch):
    """抓 JD 范围（2026-10-09 用户裁定）：缺 JD 的一律真抓——本次新岗 + 库里已有但缺
    JD 的老岗；库里已有 JD 的免费复用，不再花额度。实际数量如实写进 note。"""
    old_missing = {"platform": "boss", "job_id": "o1", "city": "上海", "title": "老岗缺JD"}
    old_cached = {"platform": "boss", "job_id": "o2", "city": "上海", "title": "老岗有JD",
                  "jd_text": "已有正文"}
    njobs = [{"platform": "boss", "job_id": f"n{i}", "city": "上海", "title": "新岗"}
             for i in range(2)]
    env = _pipeline_env(monkeypatch, list_rows=[old_missing, old_cached],
                        new_jobs=[old_missing, old_cached] + njobs)
    try:
        server.start_pipeline(PipelineReq())
        snap = _wait_pipeline_done()
        assert env["jd"] == 3, "2 个新岗 + 1 个缺 JD 的老岗；有缓存的 o2 不该再花额度"
        assert sorted(env["analyzed_jobs"]) == ["n0", "n1", "o1", "o2"]   # 有 JD 的都分析
        assert "抓 JD 3 个" in snap["note"] and "新岗 2" in snap["note"]
        assert "补库里缺 JD 的老岗 1" in snap["note"]
        assert "复用" in snap["note"]
    finally:
        _pipeline_cleanup()


def test_pipeline_skips_already_analyzed_jobs(monkeypatch):
    """有 JD 但已分析过的岗位不再分析：同一关键词反复抓列表时不重复烧 token。"""
    old_done = {"platform": "boss", "job_id": "o9", "city": "上海", "title": "已分析老岗",
                "jd_text": "已有正文", "llm_analysis": {"score": 80}}
    njobs = [{"platform": "boss", "job_id": f"p{i}", "city": "上海", "title": "新岗"}
             for i in range(2)]
    env = _pipeline_env(monkeypatch, list_rows=[old_done],
                        new_jobs=[old_done] + njobs)
    try:
        server.start_pipeline(PipelineReq())
        snap = _wait_pipeline_done()
        assert sorted(env["analyzed_jobs"]) == ["p0", "p1"], "o9 已有分析，不该再来一次"
        assert "跳过 1" in snap["note"]
    finally:
        _pipeline_cleanup()


def test_pipeline_scrape_error_aborts(monkeypatch):
    """抓取失败 → 不抓 JD 也不分析，错误信息落在 errors。"""
    env = _pipeline_env(monkeypatch, crawl_result={"jobs": [], "source": "error",
                                                   "platform": "boss", "error": "风控"})
    try:
        server.start_pipeline(PipelineReq())
        snap = _wait_pipeline_done()
        assert env["crawl"] == 1 and env["jd"] == 0 and env["analyze"] == 0
        assert any("抓取失败" in e for e in snap["errors"])
    finally:
        _pipeline_cleanup()


def test_pipeline_no_new_jobs_stops(monkeypatch):
    """抓取成功但无新岗位 → 直接结束并说明。"""
    env = _pipeline_env(monkeypatch,
                        crawl_result={"jobs": [], "source": "real", "platform": "boss",
                                      "stats": {"total": 0, "new": 0, "seen": 0}},
                        new_jobs=[])
    try:
        server.start_pipeline(PipelineReq())
        snap = _wait_pipeline_done()
        assert env["jd"] == 0 and env["analyze"] == 0
        assert any("没有新岗位" in e for e in snap["errors"])
    finally:
        _pipeline_cleanup()


def test_pipeline_jd_failures_skipped_in_analysis(monkeypatch):
    """部分 JD 抓取失败 → 失败岗跳过分析，成功岗照常分析，结尾有提示。"""
    env = _pipeline_env(monkeypatch, jd_fail_ids={"n0"})
    try:
        server.start_pipeline(PipelineReq())
        snap = _wait_pipeline_done()
        assert env["jd"] == 2 and env["analyze"] == 1          # 只有 n1 进分析
        assert env["analyzed_jobs"] == ["n1"]
        assert any("JD 抓取失败" in e for e in snap["errors"])
    finally:
        _pipeline_cleanup()


def test_pipeline_rejected_when_batch_running(monkeypatch):
    """已有批量任务在跑 → 流水线拒绝，不碰任何阶段。"""
    env = _pipeline_env(monkeypatch)
    try:
        server._batch["running"] = True
        r = server.start_pipeline(PipelineReq())
        assert r["ok"] is False and "在跑" in r["error"]
        assert env["crawl"] == 0
    finally:
        server._batch["running"] = False


def test_pipeline_rejected_without_resume(monkeypatch):
    """个人资料无简历文本 → 预检拒绝。"""
    _pipeline_env(monkeypatch)
    try:
        monkeypatch.setattr(server.db, "get_profile", lambda: {"data": {"city": "上海"}})
        r = server.start_pipeline(PipelineReq())
        assert r["ok"] is False and "简历" in r["error"]
    finally:
        _pipeline_cleanup()

def test_pipeline_timeout_circuit_breaks(monkeypatch):
    """BOSS 软锁（连续 150s 超时）→ 连 2 次即熔断，不再空烧剩余岗位；
    结尾提示冷却与剩余数量。"""
    tjobs = [{"platform": "boss", "job_id": f"t{i}", "city": "上海",
              "title": "AI工程师"} for i in range(5)]
    env = _pipeline_env(monkeypatch, jd_timeout_ids={"t0", "t1", "t2", "t3", "t4"},
                        list_rows=[], new_jobs=tjobs)
    try:
        server.start_pipeline(PipelineReq())
        snap = _wait_pipeline_done()
        assert env["jd"] == 2                      # 连 2 次超时即熔断，第 3 个没试
        assert env["analyze"] == 0                 # 没有成功 JD，不进分析
        assert any("已自动熔断" in e and "软性限流" in e for e in snap["errors"])
    finally:
        _pipeline_cleanup()


def test_pipeline_timeout_then_success_resets_counter(monkeypatch):
    """单次超时夹在成功之间不触发熔断（计数只在连续超时时累积）。"""
    mjobs = [{"platform": "boss", "job_id": f"m{i}", "city": "上海",
              "title": "AI工程师"} for i in range(3)]
    # m0 超时，m1/m2 成功 → 不熔断，分析 2 个
    env = _pipeline_env(monkeypatch, jd_timeout_ids={"m0"}, list_rows=[], new_jobs=mjobs)
    try:
        server.start_pipeline(PipelineReq())
        snap = _wait_pipeline_done()
        assert env["jd"] == 3 and env["analyze"] == 2
        assert not any("已自动熔断" in e for e in snap["errors"])
        assert any("JD 抓取失败" in e or "1 个岗位" in e for e in snap["errors"])
    finally:
        _pipeline_cleanup()


def test_pipeline_nontimeout_failures_circuit_break(monkeypatch):
    """连续「非超时」失败（Chrome 中途被关 / 风控 / 无 url）也要熔断，剩余岗位不再试。

    回归：2026-10-09 之前流水线抄了一份循环体，只把「抓取超时」计入熔断，其它失败
    一律清零重来 → 环境类故障时剩余二十几个岗位每岗走满三层降级，纯空烧。
    """
    fjobs = [{"platform": "boss", "job_id": f"f{i}", "city": "上海",
              "title": "AI工程师"} for i in range(5)]
    env = _pipeline_env(monkeypatch, jd_fail_ids={f"f{i}" for i in range(5)},
                        list_rows=[], new_jobs=fjobs)
    try:
        server.start_pipeline(PipelineReq())
        snap = _wait_pipeline_done()
        assert env["jd"] == 3, "连 3 次失败应熔断，第 4、5 个不该再试"
        assert env["analyze"] == 0
        assert any("已自动停止" in e and "Chrome" in e for e in snap["errors"])
    finally:
        _pipeline_cleanup()


def test_batch_run_timeout_breaker_is_shared(monkeypatch):
    """软锁熔断在共用循环里，手动批量（fetch_and_analyze）同样连 2 次超时即停。

    旧版只有通用「连 3 失败」熔断，超时不单独计——每岗 150s×3 层降级的代价下，
    第 3 次才停等于多烧一整个岗位。
    """
    monkeypatch.setattr(server.fetch_gate, "checkpoint", lambda: None)
    monkeypatch.setattr(server.fetch_gate, "stopped", lambda: False)
    monkeypatch.setattr(server, "_progress_update", lambda **k: None)
    calls = {"jd": 0}

    def fake_fetch(job, refresh=False, cancel=None):
        calls["jd"] += 1
        return {"ok": False, "error": "抓取超时（150s）；直连亦未解析到 JD"}
    monkeypatch.setattr(server, "_fetch_jd_core", fake_fetch)
    server._batch.update({"running": True, "stop": False, "total": 5, "done": 0,
                          "ok": 0, "failed": 0, "current": "", "errors": [],
                          "stage": "", "pipeline": False})
    server._batch_cancel.clear()
    try:
        jobs = [{"platform": "boss", "job_id": f"b{i}", "title": "AI工程师"} for i in range(5)]
        server._batch_run(jobs, {"base_url": "x", "model": "m"}, server._batch_cancel)
        assert calls["jd"] == 2, "第 2 次超时就该熔断，不该试第 3 个"
        assert any("已自动熔断" in e and "软性限流" in e for e in server._batch["errors"])
    finally:
        _pipeline_cleanup()


def test_match_analysis_endpoint_passes_review_floor(monkeypatch):
    """单岗分析端点也要带升档门槛：流水线之外（详情弹窗手动分析）是唯一另一条分析落库路径，
    漏传就等于「批量会升档、单点不会」的隐性双标。"""
    seen = {}
    monkeypatch.setattr(server.db, "get_job",
                        lambda p, j: {"platform": p, "job_id": j, "title": "AI工程师",
                                      "status": "discovered", "llm_analysis": None})
    monkeypatch.setattr(server.db, "get_profile", lambda: {"data": {}})
    monkeypatch.setattr(server, "_get_llm_config", lambda mid=None: {"base_url": "x", "model": "m"})
    monkeypatch.setattr(server.llm, "analyze_match", lambda *a, **k: {"score": 91})
    monkeypatch.setattr(server.db, "save_job_analysis",
                        lambda p, j, a, review_floor=None: seen.update(args=(p, j, a["score"], review_floor)))
    out = server.match_analysis(AnalyzeReq(job_id="m1"))
    assert out["ok"] is True
    assert seen["args"] == ("boss", "m1", 91, server._AUTO_REVIEW_SCORE)


def test_batch_run_analyze_passes_review_floor(monkeypatch):
    """批量分析（共用循环的 analyze_only）同样把门槛透传到 db 层。"""
    floors = []
    monkeypatch.setattr(server.fetch_gate, "checkpoint", lambda: None)
    monkeypatch.setattr(server.fetch_gate, "stopped", lambda: False)
    monkeypatch.setattr(server, "_progress_update", lambda **k: None)
    monkeypatch.setattr(server.db, "get_job",
                        lambda p, j: {"platform": p, "job_id": j, "title": "AI工程师"})
    monkeypatch.setattr(server.db, "get_profile", lambda: {"data": {}})
    monkeypatch.setattr(server.llm, "analyze_match", lambda *a, **k: {"score": 88})
    monkeypatch.setattr(server.db, "save_job_analysis",
                        lambda p, j, a, review_floor=None: floors.append(review_floor))
    server._batch.update({"running": True, "stop": False, "total": 1, "done": 0,
                          "ok": 0, "failed": 0, "current": "", "errors": [],
                          "stage": "", "pipeline": False})
    server._batch_cancel.clear()
    try:
        server._batch_run([{"platform": "boss", "job_id": "q1", "title": "AI工程师"}],
                          {"base_url": "x", "model": "m"}, server._batch_cancel,
                          mode="analyze_only")
        assert floors == [server._AUTO_REVIEW_SCORE]
    finally:
        _pipeline_cleanup()


def test_pipeline_broadcasts_auto_promoted_count(monkeypatch):
    """播报里必须有「自动升入评估列 N 个」：漏斗入口从粗筛换成 AI 分之后，
    条数是这条路径唯一可观测的证据，静默升档等于用户看不出它生效过。"""
    env = _pipeline_env(monkeypatch)          # 桩分析分 80 ≥ 门槛 70 → 2 个新岗都该升
    try:
        assert server.start_pipeline(PipelineReq()).get("started") is True
        snap = _wait_pipeline_done()
        assert "自动升入评估列 2 个" in snap["note"], snap["note"]
    finally:
        _pipeline_cleanup()
    # 反向：分不够就一个都不升，播报也必须写 0，不能省略成「看起来没这回事」
    _pipeline_env(monkeypatch)
    monkeypatch.setattr(server.llm, "analyze_match",
                        lambda *a, **k: {"verdict": "v", "score": 40})
    try:
        assert server.start_pipeline(PipelineReq()).get("started") is True
        snap = _wait_pipeline_done()
        assert "自动升入评估列 0 个" in snap["note"], snap["note"]
    finally:
        _pipeline_cleanup()


# ---------- P6「结果看不见」（2026-10-09）：失败岗结构化 + 熔断指引不截断 + 一键补抓 ----------


def test_batch_records_failed_jobs(monkeypatch):
    """失败岗必须留下「是哪个岗、死在哪一步」的结构化记录。

    P6 的根因：进度 chip 只有一句截断文本，用户看不出哪个岗失败、也看不出失败在
    JD 抓取（花 BOSS 额度、要冷却）还是 AI 分析（只花 token、可直接重试）——
    两者补救动作完全不同，混成一条错误列表等于没区分。
    """
    monkeypatch.setattr(server.fetch_gate, "checkpoint", lambda: None)
    monkeypatch.setattr(server.fetch_gate, "stopped", lambda: False)
    monkeypatch.setattr(server, "_progress_update", lambda **k: None)
    monkeypatch.setattr(server.db, "get_profile", lambda: {"data": {}})
    monkeypatch.setattr(server.db, "get_job",
                        lambda p, j: {"platform": p, "job_id": j, "title": "AI工程师"})

    def fake_fetch(job, refresh=False, cancel=None):
        if job["job_id"] == "x0":
            return {"ok": False, "error": "抓取超时（150s）"}
        return {"ok": True}
    monkeypatch.setattr(server, "_fetch_jd_core", fake_fetch)

    def boom(*a, **k):
        raise llm.LLMError("模型连接失败")
    monkeypatch.setattr(server.llm, "analyze_match", boom)

    server._batch.update({"running": True, "stop": False, "total": 2, "done": 0,
                          "ok": 0, "failed": 0, "current": "", "errors": [],
                          "failed_jobs": [], "stage": "", "pipeline": False})
    server._batch_cancel.clear()
    try:
        jobs = [{"platform": "boss", "job_id": "x0", "title": "岗零", "company": "甲"},
                {"platform": "boss", "job_id": "x1", "title": "岗一", "company": "乙"}]
        server._batch_run(jobs, {"base_url": "x", "model": "m"}, server._batch_cancel)
        rec = server._batch["failed_jobs"]
        assert [r["job_id"] for r in rec] == ["x0", "x1"]
        assert [r["stage"] for r in rec] == ["jd", "analyze"], "死在哪一步决定怎么补救"
        assert [r["platform"] for r in rec] == ["boss", "boss"]
        assert rec[0]["title"] == "岗零" and rec[1]["company"] == "乙"
        assert "抓取超时" in rec[0]["error"] and "模型连接失败" in rec[1]["error"]
    finally:
        _pipeline_cleanup()


def test_batch_failed_jobs_survive_pipeline_stages(monkeypatch):
    """流水线两段各自重置进度计数，但失败清单不能跟着清空：
    JD 抓取阶段失败的岗正是「冷却后一键补抓」的对象，分析阶段一跑就被抹掉等于看不见。"""
    env = _pipeline_env(monkeypatch, jd_fail_ids={"n0"})
    try:
        assert server.start_pipeline(PipelineReq()).get("started") is True
        snap = _wait_pipeline_done()
        assert [r["job_id"] for r in snap["failed_jobs"]] == ["n0"]
        assert snap["failed_jobs"][0]["stage"] == "jd"
    finally:
        _pipeline_cleanup()


def test_breaker_messages_are_not_truncated(monkeypatch):
    """熔断指引是结果面板里最该读全的一句话：句尾的补救动作不能被截掉，
    且要指向新的聚合入口，不再是「逐个开详情弹窗补抓」。"""
    tjobs = [{"platform": "boss", "job_id": f"t{i}", "city": "上海", "title": "AI工程师"}
             for i in range(5)]
    _pipeline_env(monkeypatch, jd_timeout_ids={f"t{i}" for i in range(5)},
                  list_rows=[], new_jobs=tjobs)
    try:
        assert server.start_pipeline(PipelineReq()).get("started") is True
        snap = _wait_pipeline_done()
        msg = [e for e in snap["errors"] if "已自动熔断" in e][0]
        assert "剩余 3 个岗位" in msg, msg
        assert msg.endswith("「重试这 N 个失败岗」"), f"句尾补救指引被截掉了：{msg!r}"
    finally:
        _pipeline_cleanup()

    fjobs = [{"platform": "boss", "job_id": f"f{i}", "city": "上海", "title": "AI工程师"}
             for i in range(5)]
    _pipeline_env(monkeypatch, jd_fail_ids={f"f{i}" for i in range(5)},
                  list_rows=[], new_jobs=fjobs)
    try:
        assert server.start_pipeline(PipelineReq()).get("started") is True
        snap = _wait_pipeline_done()
        msg = [e for e in snap["errors"] if "已自动停止" in e][0]
        assert "剩余 2 个未处理" in msg, msg
        assert msg.endswith("见结果面板）"), f"句尾指引被截掉了：{msg!r}"
    finally:
        _pipeline_cleanup()


def test_analyze_batch_retry_runs_only_named_jobs(analyze_ready, monkeypatch):
    """点名重试：队列＝点名的失败岗，跳过城市/已分析两道闸门（它们是人工从失败清单挑的，
    再拦一遍就成了「点了没反应」），但四道预检一个都不能省——重跑照样花 JD 抓取额度。"""
    captured = {}
    monkeypatch.setattr(server, "_batch_worker",
                        lambda jobs, cfg, cancel, mode="fetch_and_analyze":
                        captured.update(jobs=jobs))
    analyze_ready([{"platform": "boss", "job_id": "j1", "city": "上海"},
                   {"platform": "boss", "job_id": "j2", "city": "北京",
                    "llm_analysis": {"score": 9}},
                   {"platform": "boss", "job_id": "j3", "city": "上海",
                    "llm_analysis": {"score": 5}}])

    r = server.start_analyze_batch(AnalyzeBatchReq(retry=[{"platform": "boss", "job_id": "j2"},
                                                          {"platform": "boss", "job_id": "zz"}]))
    assert r["ok"] is True and r["started"] is True
    assert [j["job_id"] for j in captured["jobs"]] == ["j2"], "异地 + 已分析都不该拦点名补抓"
    assert r["total"] == 1 and r["retry_missing"] == 1, "库里没有的 id 要如实报数，不静默吞掉"

    server._batch["running"] = False
    monkeypatch.setattr(server, "_native_channel_status",
                        lambda: {"ok": False, "chrome_found": False, "chrome_ready": False})
    r = server.start_analyze_batch(AnalyzeBatchReq(retry=[{"platform": "boss", "job_id": "j1"}]))
    assert r["ok"] is False and "原生通道" in r["error"], "retry 不能绕过原生通道预检去空烧额度"


def test_pipeline_can_run_again_after_finish(monkeypatch):
    """回归：一次流水线跑完后，同一进程内必须能立刻再跑第二次。

    2026-10-08 的 `start_pipeline` 用 `_pipeline_lock.acquire()` 占锁、全链路无人
    release（worker 的 finally 只复位 `_batch`）→ 一次服务生命周期内只能成功发起一次，
    之后每次都是 `{'ok': False, 'error': '已有流水线在跑'}`，而此时 `_batch['running']`
    早已是 False，界面上看不出任何区别。其余流水线用例靠 `_pipeline_cleanup()` 替生产
    代码放了锁，所以全绿也照不出这条——本用例刻意不放。
    """
    _pipeline_env(monkeypatch)
    try:
        r1 = server.start_pipeline(PipelineReq())
        assert r1.get("started") is True, r1
        _wait_pipeline_done(cleanup=False)
        assert server._pipeline_lock.locked() is False, "流水线跑完锁没归还"

        r2 = server.start_pipeline(PipelineReq())
        assert r2.get("started") is True, f"第二次流水线被拒：{r2}"
        _wait_pipeline_done(cleanup=False)
        assert server._pipeline_lock.locked() is False, "流水线跑完锁没归还"
    finally:
        _pipeline_cleanup()
