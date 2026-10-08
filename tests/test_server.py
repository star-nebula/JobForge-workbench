"""server 层纯函数测试：密钥脱敏 + 模型配置选型 + 批量分析预检 + JD 确认端点 + L0 城市门槛
+ L1 粗筛端点 + L2 精配额度门禁 + restart 三重判定 + 抓取子进程输出分支
（db/llm mock，不碰真实库、绝不触发真实 JD 抓取）。"""
import subprocess
import sys

import pytest
from fastapi import HTTPException

from jobforge import llm, server
from jobforge.server import (AnalyzeBatchReq, CrawlReq, JdConfirmReq, PipelineReq,
                             ScrapeFromResumeReq, TriageReq, _get_llm_config, _mask_key)


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
    """批量启动前必须原生通道就绪（Chrome 开着 zhipin），否则非缓存岗位全失败（2026-09-27 事故回归）。"""
    monkeypatch.setattr(server, "_get_llm_config",
                        lambda mid=None: {"base_url": "https://x/v1", "model": "m"})
    monkeypatch.setattr(server.llm, "chat", lambda *a, **k: "pong")
    monkeypatch.setattr(server, "_native_channel_status",
                        lambda: {"ok": False, "chrome_found": False, "chrome_ready": False, "error": "未找到 Chrome"})
    r = server.start_analyze_batch(AnalyzeBatchReq())
    assert r["ok"] is False and "原生通道未就绪" in r["error"]


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


# ---------- L1 打包粗筛端点 ----------
_ROWS = [{"platform": "boss", "job_id": "a", "triage_keep": None},
         {"platform": "boss", "job_id": "b", "triage_keep": 1},
         {"platform": "boss", "job_id": "c", "triage_keep": None}]


def _stub_triage(monkeypatch, rows=_ROWS, keep=lambda jid: jid != "c"):
    monkeypatch.setattr(server, "_get_llm_config",
                        lambda mid=None: {"base_url": "https://x/v1", "model": "m"})
    monkeypatch.setattr(server.llm, "chat", lambda *a, **k: "pong")
    monkeypatch.setattr(server.db, "list_jobs", lambda status=None: list(rows))
    monkeypatch.setattr(server.db, "get_profile", lambda: {"data": {"city": "上海"}})
    saved = []
    monkeypatch.setattr(server.db, "save_job_triage",
                        lambda p, j, k, r: saved.append((j, k, r)))
    monkeypatch.setattr(server.llm, "triage_jobs",
                        lambda cfg, pdata, jobs, chunk_size, progress=None: [
                            {"platform": "boss", "job_id": j["job_id"],
                             "keep": keep(j["job_id"]), "reason": "r", "answered": True}
                            for j in jobs])
    return saved


def test_triage_skips_already_triaged(monkeypatch):
    """缺省只挑 triage_keep 为 NULL 的岗位——「筛过且判不匹配」不能被下一轮又筛一遍。"""
    saved = _stub_triage(monkeypatch)
    r = server.start_triage(TriageReq(chunk_size=2))
    assert [s[0] for s in saved] == ["a", "c"]
    assert (r["total"], r["kept"], r["dropped"], r["chunks"]) == (2, 1, 1, 1)


def test_triage_job_ids_overrides_filter(monkeypatch):
    """显式 job_ids 用于小样本验收：无视已筛状态，指定谁就筛谁。"""
    saved = _stub_triage(monkeypatch, keep=lambda jid: True)
    r = server.start_triage(TriageReq(job_ids=["b"], chunk_size=20))
    assert [s[0] for s in saved] == ["b"]
    assert r["ok"] is True and r["total"] == 1


def test_triage_limit_and_force(monkeypatch):
    saved = _stub_triage(monkeypatch)
    assert server.start_triage(TriageReq(limit=1))["total"] == 1
    saved.clear()
    r = server.start_triage(TriageReq(force=True, chunk_size=40))
    assert sorted(s[0] for s in saved) == ["a", "b", "c"]     # force 连筛过的重筛
    assert r["chunks"] == 1


def test_triage_nothing_to_do(monkeypatch):
    saved = _stub_triage(monkeypatch, rows=[{"platform": "boss", "job_id": "b", "triage_keep": 0}])
    r = server.start_triage(TriageReq())
    assert r["ok"] is True and r["total"] == 0 and saved == []


def test_triage_requires_config(monkeypatch):
    monkeypatch.setattr(server, "_get_llm_config", lambda mid=None: None)
    r = server.start_triage(TriageReq())
    assert r["ok"] is False and "尚未配置" in r["error"]


def test_triage_ping_failure_blocks_before_scan(monkeypatch):
    """批量粗筛会打若干次模型调用，配置不可用要当场拦下（同批量分析的教训）。"""
    monkeypatch.setattr(server, "_get_llm_config",
                        lambda mid=None: {"base_url": "https://x/v1", "model": "m"})
    monkeypatch.setattr(server.llm, "chat",
                        lambda *a, **k: (_ for _ in ()).throw(llm.LLMError("HTTP 401")))
    listed = []
    monkeypatch.setattr(server.db, "list_jobs", lambda status=None: listed.append(1) or [])
    r = server.start_triage(TriageReq())
    assert r["ok"] is False and "配置不可用" in r["error"]
    assert listed == []          # ping 失败就不该去枚举岗位


def test_triage_llm_error_does_not_write(monkeypatch):
    """整块解析失败时抛 LLMError → 端点报错回用户，不得把岗位静默写成 drop。"""
    saved = _stub_triage(monkeypatch)

    def boom(cfg, pdata, jobs, chunk_size, progress=None):
        raise llm.LLMError("LLM 未返回 JSON")
    monkeypatch.setattr(server.llm, "triage_jobs", boom)
    r = server.start_triage(TriageReq())
    assert r["ok"] is False and "未返回 JSON" in r["error"]
    assert saved == []


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


def test_triage_skips_other_cities(monkeypatch):
    rows = [{"platform": "boss", "job_id": "sh", "triage_keep": None, "city": "上海"},
            {"platform": "boss", "job_id": "bj", "triage_keep": None, "city": "北京"}]
    saved = _stub_triage(monkeypatch, rows=rows)
    r = server.start_triage(TriageReq())
    assert [s[0] for s in saved] == ["sh"]
    assert r["skipped_other_city"] == 1


def test_triage_job_ids_bypass_city_gate(monkeypatch):
    """显式 job_ids 圈定优先于城市门槛（小样本验收要能指定任意岗）。"""
    rows = [{"platform": "boss", "job_id": "bj", "triage_keep": None, "city": "北京"}]
    saved = _stub_triage(monkeypatch, rows=rows)
    r = server.start_triage(TriageReq(job_ids=["bj"]))
    assert [s[0] for s in saved] == ["bj"] and r["skipped_other_city"] == 0


def test_analyze_batch_skips_other_cities(monkeypatch):
    """异地岗不进精配队列：全部被门槛滤掉 → 直接不开跑（不开线程、不拉悬浮窗）。"""
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


# ---------- L2 精配额度门禁（阶段2：JD 抓取额度只花在 triage_keep=1 的岗上） ----------
_QROWS = [{"platform": "boss", "job_id": "k1", "city": "上海", "triage_keep": 1},
          {"platform": "boss", "job_id": "k2", "city": "上海", "triage_keep": 1,
           "llm_analysis": {"match_score": 9}},
          {"platform": "boss", "job_id": "d1", "city": "上海", "triage_keep": 0},
          {"platform": "boss", "job_id": "u1", "city": "上海", "triage_keep": None},
          {"platform": "boss", "job_id": "bj", "city": "北京", "triage_keep": 1}]


def _ids(rows):
    return [r["job_id"] for r in rows]


def test_analyze_queue_gates_by_triage(monkeypatch):
    """没筛过的岗同样拦下：否则新抓的一批整批绕过门禁，门等于没装。"""
    monkeypatch.setattr(server.db, "get_profile", lambda: {"data": {"city": "上海"}})
    jobs, stats = server._analyze_queue([dict(r) for r in _QROWS], AnalyzeBatchReq())
    assert _ids(jobs) == ["k1"]
    assert stats == {"skipped_other_city": 1, "skipped_dropped": 1,
                     "skipped_untriaged": 1, "skipped_analyzed": 1}


def test_analyze_queue_include_dropped_bypasses_triage(monkeypatch):
    """include_dropped 用于人工复核粗筛：drop 与未筛都放行，但城市门与已分析照旧。"""
    monkeypatch.setattr(server.db, "get_profile", lambda: {"data": {"city": "上海"}})
    jobs, stats = server._analyze_queue([dict(r) for r in _QROWS],
                                        AnalyzeBatchReq(include_dropped=True))
    assert _ids(jobs) == ["k1", "d1", "u1"]
    assert stats["skipped_dropped"] == 0 and stats["skipped_untriaged"] == 0
    assert stats["skipped_analyzed"] == 1 and stats["skipped_other_city"] == 1


def test_analyze_queue_force_and_limit(monkeypatch):
    """force 只放开「已分析」这一轴，不放开粗筛门（两个轴语义不同，不能合一个开关）。"""
    monkeypatch.setattr(server.db, "get_profile", lambda: {"data": {"city": "上海"}})
    jobs, _ = server._analyze_queue([dict(r) for r in _QROWS],
                                    AnalyzeBatchReq(force=True))
    assert _ids(jobs) == ["k1", "k2"]
    jobs, _ = server._analyze_queue([dict(r) for r in _QROWS],
                                    AnalyzeBatchReq(force=True, include_dropped=True))
    assert _ids(jobs) == ["k1", "k2", "d1", "u1"]
    jobs, _ = server._analyze_queue([dict(r) for r in _QROWS],
                                    AnalyzeBatchReq(force=True, include_dropped=True, limit=2))
    assert _ids(jobs) == ["k1", "k2"]


def test_analyze_queue_no_city_gate_without_profile_city(monkeypatch):
    """profile 没填城市 → 不限城市，北京岗照样进队列（但粗筛门照旧生效）。"""
    monkeypatch.setattr(server.db, "get_profile", lambda: {"data": {}})
    jobs, stats = server._analyze_queue([dict(r) for r in _QROWS], AnalyzeBatchReq())
    assert _ids(jobs) == ["k1", "bj"] and stats["skipped_other_city"] == 0
    assert stats["skipped_dropped"] == 1 and stats["skipped_untriaged"] == 1


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
    monkeypatch.setattr(server, "_batch_worker", lambda jobs, cfg, cancel: None)

    def set_rows(rows):
        monkeypatch.setattr(server.db, "list_jobs",
                            lambda status=None: [dict(r) for r in rows])
    yield set_rows
    server._batch.update({"running": False, "stop": False, "total": 0, "done": 0,
                          "ok": 0, "failed": 0, "current": "", "errors": []})


def test_analyze_batch_starts_only_kept(analyze_ready):
    analyze_ready(_QROWS)
    r = server.start_analyze_batch(AnalyzeBatchReq())
    assert r["started"] is True and r["total"] == 1
    assert (r["skipped_dropped"], r["skipped_untriaged"],
            r["skipped_other_city"], r["skipped_analyzed"]) == (1, 1, 1, 1)


def test_analyze_batch_all_dropped_explains_triage(analyze_ready):
    """队列空时必须说清「为什么没跑起来」，且指向便宜的那一步（先粗筛，不是重抓）。"""
    analyze_ready([{"platform": "boss", "job_id": "d1", "city": "上海", "triage_keep": 0},
                   {"platform": "boss", "job_id": "u1", "city": "上海", "triage_keep": None}])
    r = server.start_analyze_batch(AnalyzeBatchReq())
    assert r["ok"] is True and r["started"] is False
    assert "粗筛" in r["message"] and "不匹配" in r["message"]


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


# ---------- B1：粗筛并发锁 + 预览 + 进度（2026-10-03，全同步接口防连点/防刷新重复起跑） ----------

def _triage_env(monkeypatch, rows=None):
    """粗筛端点环境 stub：模型可达、db 走内存行、triage_jobs 换成不碰 LLM 的桩。"""
    monkeypatch.setattr(server, "_get_llm_config",
                        lambda mid=None: {"base_url": "https://x/v1", "model": "m"})
    monkeypatch.setattr(server.llm, "chat", lambda *a, **k: "pong")
    monkeypatch.setattr(server.db, "save_job_triage", lambda *a, **k: None)
    monkeypatch.setattr(server.db, "get_profile", lambda: {"data": {"city": "上海"}})
    monkeypatch.setattr(server.db, "list_jobs", lambda status=None: rows if rows is not None else [])
    calls = {}
    def fake_triage(cfg, pdata, jobs, chunk_size=llm.TRIAGE_CHUNK, progress=None):
        calls["chunk_size"] = chunk_size
        calls["progress"] = progress
        if progress:
            progress(len(jobs), -(-len(jobs) // chunk_size))
        return [{"platform": "boss", "job_id": j["job_id"], "keep": True,
                 "reason": "r", "answered": True} for j in jobs]
    monkeypatch.setattr(server.llm, "triage_jobs", fake_triage)
    return calls


_TRIAGE_ROWS = [
    {"platform": "boss", "job_id": "a1", "city": "上海", "triage_keep": None},
    {"platform": "boss", "job_id": "a2", "city": "上海", "triage_keep": None},
    {"platform": "boss", "job_id": "kept", "city": "上海", "triage_keep": 1},
    {"platform": "boss", "job_id": "far", "city": "北京", "triage_keep": None},
]


def test_triage_preview_queue口径(monkeypatch):
    """预览 = 队列口径只读版：未筛 2 个进队列、已粗筛 1 个排除、异地 1 个 L0 拦下。"""
    _triage_env(monkeypatch, _TRIAGE_ROWS)
    pv = server.triage_preview()
    assert pv["pending"] == 2
    assert pv["chunks"] == -(-2 // llm.TRIAGE_CHUNK)
    assert pv["skipped_other_city"] == 1


def test_triage_busy_lock_rejects_second_run(monkeypatch):
    """上一轮没跑完时再点必须被服务端拒绝——前端按钮禁用态会随页面刷新丢失。"""
    _triage_env(monkeypatch, _TRIAGE_ROWS)
    assert server._triage_lock.acquire(blocking=False)
    server._triage_state.update(running=True, done=0, total=0)
    try:
        r = server.start_triage(TriageReq())
        assert r["busy"] is True and r["ok"] is False and "粗筛在跑" in r["error"]
    finally:
        server._triage_state["running"] = False
        server._triage_lock.release()
    # 锁释放后能正常跑完，且跑完锁必然回到释放态（finally 兜底，异常路径同样）
    r = server.start_triage(TriageReq())
    assert r["ok"] is True and r["total"] == 2
    assert not server._triage_state["running"]


def test_triage_status_reports_progress(monkeypatch):
    """进度经 progress 回调写进 _triage_state，状态端点原样吐出。"""
    calls = _triage_env(monkeypatch, _TRIAGE_ROWS[:2])
    r = server.start_triage(TriageReq())
    assert r["ok"] is True
    assert calls["progress"] is not None          # 回调真的传给了 llm 层
    s = server.triage_status()
    assert s["running"] is False
    assert s["done"] == 2 and s["total"] == -(-2 // llm.TRIAGE_CHUNK)


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


# ---------- 一键流水线（2026-10-08）：抓取 → 粗筛 → 精配（限额），阶段边界停止 ----------

def _pipeline_cleanup():
    """流水线测试后的全局态复位（worker 在后台线程跑，monkeypatch 管不到 _batch/锁）。"""
    server._batch.update({"running": False, "stop": False, "total": 0, "done": 0,
                          "ok": 0, "failed": 0, "current": "", "errors": [],
                          "stage": "", "pipeline": False})
    if server._pipeline_lock.locked():
        server._pipeline_lock.release()


def _pipeline_env(monkeypatch, crawl_result=None, jd_fail_ids=None,
                  jd_timeout_ids=None, list_rows=None, new_jobs=None):
    """流水线全 stub：预检全绿、抓取/粗筛/精配三段全部换成可断言的桩。
    返回 calls dict 供断言（crawl/triage/batch 计数与 batch_jobs 队列）。"""
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

    # 默认本次抓到 2 个新岗位（与真实 upsert 后的库状态一致）
    new_jobs = new_jobs or [{"platform": "boss", "job_id": f"n{i}", "city": "上海",
                             "title": "AI工程师", "tags": ["Python"]} for i in range(2)]
    rows = list_rows if list_rows is not None else []
    store = list(rows) + new_jobs          # 模拟 seen_jobs 表：save_jd/save_analysis 原地更新它
    monkeypatch.setattr(server.db, "list_jobs",
                        lambda status=None: [dict(r) for r in store])
    monkeypatch.setattr(server.db, "upsert_jobs", lambda jobs: [True] * len(jobs))

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

    def fake_save_analysis(platform, job_id, analysis):
        calls["analyzed_jobs"].append(job_id)
        for r in store:
            if r.get("job_id") == job_id:
                r["llm_analysis"] = analysis
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


def _wait_pipeline_done(timeout_s=3.0):
    """等流水线后台线程跑完（全 stub 毫秒级；跑不完即超时失败）。
    返回结束时 _batch 的快照（cleanup 会清 errors，断言用快照）。"""
    import time as _t
    deadline = _t.time() + timeout_s
    while _t.time() < deadline:
        if not server._batch["running"]:
            snap = {"errors": list(server._batch["errors"])}
            _pipeline_cleanup()
            return snap
        _t.sleep(0.02)
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
