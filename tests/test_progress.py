"""抓取进度闸门与进度状态的测试（2026-09-27 悬浮进度窗）。

覆盖三块：
  fetch_gate   跨进程暂停/停止文件信号（暂停不计时、停止 raise）
  server       进度作用域（正常/异常/嵌套收尾）、控制端点、stopped 分支
  hud          纯函数（暂停按钮两态、时间与截断格式化）
窗口行为（置顶/穿透/不抢焦点）靠注入点击探针实测（探针脚本未入库、已失效），不进单测；
结论见记忆库 Lessons/Windows悬浮窗穿透与不抢焦点。
"""
import os
import tempfile
import threading
import time

import pytest

from jobforge import fetch_gate
from jobforge.tools import hud


@pytest.fixture(autouse=True)
def gate_tmp(monkeypatch, tmp_path):
    """所有用例的闸门文件指向临时目录，绝不碰真实 fetch_gate.json。"""
    p = tmp_path / "gate.json"
    monkeypatch.setenv("JOBFORGE_GATE_FILE", str(p))
    fetch_gate.clear()
    yield p
    fetch_gate.clear()


# ---------------- fetch_gate ----------------

def test_gate_default_state_is_running():
    assert fetch_gate.paused() is False
    assert fetch_gate.stopped() is False
    fetch_gate.checkpoint()          # 不阻塞、不抛


def test_gate_checkpoint_blocks_while_paused_then_returns():
    fetch_gate.set_state(paused=True)
    threading.Timer(0.6, lambda: fetch_gate.set_state(paused=False)).start()
    t0 = time.time()
    fetch_gate.checkpoint(poll=0.05)
    assert 0.4 < time.time() - t0 < 3.0    # 真的等了，且恢复后返回


def test_gate_checkpoint_raises_on_stop():
    fetch_gate.set_state(stopped=True)
    with pytest.raises(fetch_gate.Stopped):
        fetch_gate.checkpoint()
    # 停止优先于暂停：两个都置位时也要立刻抛
    fetch_gate.set_state(paused=True)
    with pytest.raises(fetch_gate.Stopped):
        fetch_gate.checkpoint()


def test_gate_clear_resets_both():
    fetch_gate.set_state(paused=True, stopped=True)
    fetch_gate.clear()
    assert fetch_gate.paused() is False and fetch_gate.stopped() is False


def test_wait_pausable_does_not_count_pause_time():
    """暂停时长不计入等待：暂停 1s 期间目标 1s 的等待不应提前结束。"""
    fetch_gate.set_state(paused=True)
    threading.Timer(1.0, lambda: fetch_gate.set_state(paused=False)).start()
    t0 = time.time()
    fetch_gate.wait_pausable(0.5, poll=0.05)
    dt = time.time() - t0
    assert dt >= 1.4, dt          # 至少是 暂停1.0 + 等待0.5
    assert dt < 4.0, dt


def test_wait_pausable_interrupted_by_stop():
    fetch_gate.set_state(stopped=True)
    t0 = time.time()
    with pytest.raises(fetch_gate.Stopped):
        fetch_gate.wait_pausable(10.0, poll=0.05)
    assert time.time() - t0 < 2.0     # 不必等满 10s


def test_gate_reads_corrupt_file_as_running(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{ 不是 json", encoding="utf-8")
    monkeypatch.setenv("JOBFORGE_GATE_FILE", str(bad))
    assert fetch_gate.paused() is False
    assert fetch_gate.stopped() is False


# ---------------- server 进度状态 ----------------

@pytest.fixture
def srv(monkeypatch):
    from jobforge import server
    monkeypatch.setattr(server, "_ensure_hud", lambda: None)   # 测试不真的起窗口
    server._progress_depth = 0
    server._progress.update({"active": False, "title": "", "phase": "", "current": "",
                             "total": 0, "done": 0, "ok": 0, "failed": 0,
                             "started_at": 0.0, "detail_until_ts": 0.0,
                             "finished_at": 0.0, "stopped": False,
                             "last_error": "", "avg_sec": 0.0})
    return server


def test_progress_scope_normal(srv):
    with srv._progress_scope("智能抓取", "打开搜索页", total=3) as sc:
        assert sc.outer is True
        sc.set(done=1, ok=1)
        assert srv._progress_get()["active"] is True
    p = srv._progress_get()
    assert p["active"] is False and p["done"] == 1 and p["total"] == 3
    assert srv._progress_depth == 0


def test_progress_scope_finishes_on_exception(srv):
    """异常路径必须收尾：否则进度窗永远卡在「运行中」，嵌套计数也会泄漏。"""
    with pytest.raises(ValueError):
        with srv._progress_scope("X", "p"):
            raise ValueError("boom")
    p = srv._progress_get()
    assert p["active"] is False
    assert "ValueError" in p["last_error"]
    assert srv._progress_depth == 0


def test_progress_scope_nested_inner_does_not_take_over(srv):
    """批量分析中打开单个 JD 弹窗：内层不夺显示、不复位闸门，外层结束才收尾。"""
    with srv._progress_scope("外层批量", "抓 JD", total=5) as outer:
        assert outer.outer is True
        with srv._progress_scope("内层单岗", "详情页", total=1) as inner:
            assert inner.outer is False
            inner.set(title="不该生效")
            assert srv._progress_get()["title"] == "外层批量"
        assert srv._progress_get()["active"] is True      # 内层结束不影响外层
        assert srv._progress_depth == 1
    assert srv._progress_get()["active"] is False
    assert srv._progress_depth == 0


def test_scrape_control_pause_resume_stop(srv):
    r = srv.scrape_control(srv.ScrapeControlReq(action="pause"))
    assert r["ok"] and fetch_gate.paused() is True
    assert srv.scrape_progress()["paused"] is True

    r = srv.scrape_control(srv.ScrapeControlReq(action="resume"))
    assert r["paused"] is False and fetch_gate.paused() is False

    r = srv.scrape_control(srv.ScrapeControlReq(action="stop"))
    assert r["stopped"] is True and fetch_gate.stopped() is True


def test_scrape_control_rejects_unknown_action(srv):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        srv.scrape_control(srv.ScrapeControlReq(action="explode"))


def test_scrape_control_stop_kills_running_procs(srv):
    killed = []

    class FakeProc:
        def kill(self):
            killed.append(1)

    with srv._batch_proc_lock:
        srv._batch_procs.add(FakeProc())
    try:
        r = srv.scrape_control(srv.ScrapeControlReq(action="stop"))
        assert r["killed_procs"] == 1 and killed == [1]
    finally:
        with srv._batch_proc_lock:
            srv._batch_procs.clear()


def test_progress_stopping_flag_only_when_active(srv):
    """stopping 只在任务运行中有意义：非运行态没有「正在停」的东西。"""
    srv.scrape_control(srv.ScrapeControlReq(action="stop"))
    assert srv.scrape_progress()["stopping"] is False

    fetch_gate.clear()
    srv._progress_start("X", "p", total=2)
    srv.scrape_control(srv.ScrapeControlReq(action="stop"))
    assert srv.scrape_progress()["stopping"] is True
    srv._progress_finish(stopped=True)


def test_scrape_from_resume_returns_stopped_structure(srv, monkeypatch):
    def fake_crawl(*a, **k):
        fetch_gate.set_state(stopped=True)
        raise fetch_gate.Stopped("已停止（用户手动结束）")

    monkeypatch.setattr(srv.spider, "crawl", fake_crawl)
    res = srv.scrape_from_resume(srv.ScrapeFromResumeReq(resume_text="求职意向：前端\n技能：React"))
    assert res["source"] == "stopped"
    assert res["jobs"] == []
    assert res["stats"]["total"] == 0
    assert srv._progress_get()["active"] is False
    assert srv._progress_depth == 0


def test_crawl_returns_stopped_structure(srv, monkeypatch):
    def fake_crawl(*a, **k):
        fetch_gate.set_state(stopped=True)
        raise fetch_gate.Stopped("已停止（用户手动结束）")

    monkeypatch.setattr(srv.spider, "crawl", fake_crawl)
    res = srv.crawl_jobs(srv.CrawlReq(query="前端"))
    assert res["source"] == "stopped"
    assert srv._progress_depth == 0


def test_batch_worker_stops_at_safety_point(srv, monkeypatch):
    """批量 worker 在岗位边界检查闸门：暂停时不继续抓下一个。"""
    jobs = [{"platform": "boss", "job_id": "1", "title": "A", "company": "C"}]
    monkeypatch.setattr(srv.db, "list_jobs", lambda status=None: jobs)
    fetched = []
    monkeypatch.setattr(srv, "_fetch_jd_core",
                        lambda j, refresh=False, cancel=None: fetched.append(j["job_id"]) or
                        {"ok": True, "jd": "x"})
    monkeypatch.setattr(srv.db, "get_job", lambda p, i: jobs[0])
    monkeypatch.setattr(srv.db, "get_profile", lambda: {"data": {}})
    monkeypatch.setattr(srv.llm, "analyze_match", lambda *a, **k: "分析")

    def fake_save(*a, **k):
        fetch_gate.set_state(stopped=True)      # 分析后立刻收到停止

    monkeypatch.setattr(srv.db, "save_job_analysis", fake_save)
    srv._batch.update({"running": True, "stop": False, "total": 1, "done": 0,
                       "ok": 0, "failed": 0, "current": "", "errors": []})
    srv._batch_cancel.clear()
    srv._progress_start("批量 AI 分析", "抓取 JD 并分析", total=1)
    srv._batch_worker(jobs, {"base_url": "x", "model": "m"}, srv._batch_cancel)
    assert srv._batch["running"] is False
    assert srv._progress_get()["active"] is False
    assert srv._progress_get()["stopped"] is True


def test_batch_worker_skips_work_while_paused_then_runs(srv, monkeypatch):
    """暂停在岗位边界生效：暂停期间不抓 JD，恢复后继续。"""
    jobs = [{"platform": "boss", "job_id": "1", "title": "A", "company": "C"}]
    fetched = []
    monkeypatch.setattr(srv, "_fetch_jd_core",
                        lambda j, refresh=False, cancel=None: fetched.append(j["job_id"]) or
                        {"ok": True, "jd": "x"})
    monkeypatch.setattr(srv.db, "get_job", lambda p, i: jobs[0])
    monkeypatch.setattr(srv.db, "get_profile", lambda: {"data": {}})
    monkeypatch.setattr(srv.llm, "analyze_match", lambda *a, **k: "分析")
    monkeypatch.setattr(srv.db, "save_job_analysis", lambda *a, **k: None)

    srv._batch.update({"running": True, "stop": False, "total": 1, "done": 0,
                       "ok": 0, "failed": 0, "current": "", "errors": []})
    srv._batch_cancel.clear()
    srv._progress_start("批量 AI 分析", "抓取 JD 并分析", total=1)
    # 任务开始后再暂停（_progress_start 会复位闸门，清掉上一次任务的残留状态）
    fetch_gate.set_state(paused=True)
    threading.Timer(0.8, lambda: fetch_gate.set_state(paused=False)).start()
    t0 = time.time()
    srv._batch_worker(jobs, {"base_url": "x", "model": "m"}, srv._batch_cancel)
    dt = time.time() - t0
    assert fetched == ["1"], fetched        # 恢复后照常抓
    assert dt >= 0.7, dt                    # 确实是等过暂停
    assert srv._batch["done"] == 1
    assert srv._progress_get()["active"] is False


def test_progress_start_clears_stale_gate(srv):
    """新任务开始时必须复位闸门：上一次任务留下的 stopped 不能立刻掐死新任务。"""
    fetch_gate.set_state(paused=True, stopped=True)
    srv._progress_start("新任务", "阶段", total=2)
    assert fetch_gate.paused() is False and fetch_gate.stopped() is False
    srv._progress_finish(stopped=False)


# ---------------- hud 纯函数 ----------------

def test_pause_action_two_states():
    assert hud._pause_action(False) == "pause"
    assert hud._pause_action(True) == "resume"


def test_pause_label_two_states():
    assert hud._pause_label(False) == "⏸ 暂停"
    assert hud._pause_label(True) == "▶ 继续"


def test_mmss():
    assert hud._mmss(45) == "45s"
    assert hud._mmss(61) == "1:01"
    assert hud._mmss(-3) == "0s"


def test_trunc():
    assert hud._trunc("abc", 5) == "abc"
    assert hud._trunc("abcdef", 5) == "abcd…"
    assert hud._trunc("a\nb", 9) == "a b"
    assert hud._trunc("", 4) == ""


# ---------------- hud.render：真实进度快照必须被渲染 ----------------
# 回归（2026-09-27 实测发现）：进度快照的 "ok" 是「成功条数」，空闲/未开始时为 0。
# 早期 render 写成 s.get("ok", True) 判 falsy 就 return，导致完全空闲的窗口不渲染
# ——窗口既不显示「待命」也不自动关，只能靠 ✕。桩数据 ok=3 时掩盖了它。

class _W:
    """最小 tkinter 控件替身：只记录 configure 结果。"""

    def __init__(self):
        self.cfg = {}

    def configure(self, **kw):
        self.cfg.update(kw)

    def cget(self, k):
        return self.cfg.get(k, "")

    def itemconfigure(self, *a, **k):
        pass

    def coords(self, *a):
        pass

    def after(self, *a):
        return "job"

    def after_cancel(self, j):
        pass


def _fake_hud():
    """伪造 render 需要的最小 Hud 状态（不建真窗口）。"""
    class Fake:
        pass

    f = Fake()
    f.last_state = {}
    f.seen_active = False
    f.close_job = None
    f.fails = 0
    for name in ("l_title", "l_chip", "l_stats", "l_cur", "l_detail", "bar", "bar_fill",
                 "b_pause", "b_stop"):
        setattr(f, name, _W())

    def _set_fill(frac, color=None):
        fills.append(frac)

    f._set_fill = _set_fill
    f._schedule_close = lambda ms: scheduled.append(ms)
    return f


scheduled = []
fills = []


@pytest.mark.parametrize("snap", [
    # 空闲：ok=0 且 done=0 —— 正是被 falsy 判断误杀的形态
    {"active": False, "title": "", "phase": "", "current": "", "total": 0, "done": 0,
     "ok": 0, "failed": 0, "started_at": 0.0, "finished_at": 0.0, "stopped": False,
     "last_error": "", "paused": False, "stopping": False, "eta_sec": None},
    # 运行中：ok=0（还没成功过任何一个）
    {"active": True, "title": "智能抓取", "phase": "打开搜索页", "current": "关键词",
     "total": 12, "done": 0, "ok": 0, "failed": 0, "paused": False, "stopping": False},
    # 运行中且暂停
    {"active": True, "title": "x", "total": 12, "done": 3, "ok": 2, "failed": 1,
     "paused": True, "stopping": False},
    # 运行中且停止中
    {"active": True, "title": "x", "total": 12, "done": 3, "ok": 2, "paused": False,
     "stopping": True},
    # 正常完成
    {"active": False, "title": "x", "total": 12, "done": 12, "ok": 11, "failed": 1,
     "finished_at": 1, "stopped": False},
    # 用户结束
    {"active": False, "title": "x", "total": 12, "done": 5, "ok": 5, "finished_at": 1,
     "stopped": True, "last_error": "已停止（用户手动结束）"},
])
def test_render_accepts_real_snapshots(snap):
    """六种真实快照都要被渲染：render 不能因为 ok/done 为 0 而早退。"""
    del scheduled[:]
    f = _fake_hud()
    hud.Hud.render(f, snap)
    assert f.l_chip.cfg.get("text"), "状态芯片必须被写入"


def test_render_idle_shows_standby_and_does_not_breach_ok_zero():
    """空闲快照（ok=0）必须落到「待命」分支：手动打开时保持可见、不自动关。"""
    del scheduled[:]
    f = _fake_hud()
    hud.Hud.render(f, {"active": False, "ok": 0, "done": 0, "total": 0, "finished_at": 0,
                       "title": "", "paused": False, "stopping": False})
    assert f.l_chip.cfg["text"] == "待命"
    assert "没有进行中的抓取任务" in f.l_stats.cfg["text"]
    assert scheduled == []            # 未启动过任务：不自动关（留给用户点 ✕）


def test_render_finished_schedules_auto_close():
    """任务结束后要调度自动关闭（停留展示 LINGER_MS）。"""
    del scheduled[:]
    f = _fake_hud()
    f.seen_active = True
    hud.Hud.render(f, {"active": False, "ok": 3, "done": 3, "total": 3, "finished_at": 1,
                       "stopped": False, "title": "x"})
    assert scheduled == [hud.LINGER_MS]
    assert f.l_chip.cfg["text"].startswith("✅")


def test_render_stopped_shows_stopped_chip():
    del scheduled[:]
    f = _fake_hud()
    f.seen_active = True
    hud.Hud.render(f, {"active": False, "ok": 1, "done": 2, "total": 9, "finished_at": 1,
                       "stopped": True, "last_error": "已停止（用户手动结束）", "title": "x"})
    assert f.l_chip.cfg["text"].startswith("⏹ 已结束")
    assert "已停止" in f.l_detail.cfg["text"]
    assert scheduled == [hud.LINGER_MS]


def test_render_non_dict_ignored():
    f = _fake_hud()
    hud.Hud.render(f, "not a dict")     # 不抛异常
    hud.Hud.render(f, None)


# ---------------- 单项任务进度条：固定三档（用户 2026-09-27 要求） ----------------
# 抓单个岗位 JD 时进度条此前放「不确定进度」来回滚动的动画（tick 每轮推进），
# 观感像横跳且看不出进度。现要求只有三档：开始前 0% → 获取中 50% → 完成后 100%。

def test_bar_fraction_single_item_three_states():
    assert hud._bar_fraction(1, 0, active=False, finished=False, stopped=False) == 0.0    # 开始前
    assert hud._bar_fraction(1, 0, active=True, finished=False, stopped=False) == 0.5     # 获取中
    assert hud._bar_fraction(1, 1, active=False, finished=True, stopped=False) == 1.0      # 完成后
    # total=0（连岗位数都还没拿到）也按单项处理，不能除零
    assert hud._bar_fraction(0, 0, active=True, finished=False, stopped=False) == 0.5
    assert hud._bar_fraction(0, 0, active=False, finished=False, stopped=False) == 0.0


def test_bar_fraction_single_item_stopped_stays_mid():
    """被结束的单项任务停在 50%（开始过但没跑完），而不是跳回 0 或假装 100%。"""
    assert hud._bar_fraction(1, 0, active=False, finished=True, stopped=True) == 0.5
    assert hud._bar_fraction(1, 1, active=False, finished=True, stopped=True) == 0.5


def test_bar_fraction_multi_item_uses_real_ratio():
    """多项任务仍按真实完成比例（批量分析不适用三档规则）。"""
    assert hud._bar_fraction(12, 0, active=True, finished=False, stopped=False) == 0.0
    assert hud._bar_fraction(12, 3, active=True, finished=False, stopped=False) == 0.25
    assert hud._bar_fraction(12, 6, active=True, finished=False, stopped=False) == 0.5
    assert hud._bar_fraction(12, 12, active=False, finished=True, stopped=False) == 1.0
    # 被结束：显示结束前跑到的比例（比旧版统一归零更贴合「结束前完成 41/87」的文案）
    assert hud._bar_fraction(87, 41, active=False, finished=True, stopped=True) == pytest.approx(41 / 87)


def test_bar_fraction_clamped():
    assert hud._bar_fraction(10, 99, active=True, finished=False, stopped=False) == 1.0
    assert hud._bar_fraction(10, -3, active=True, finished=False, stopped=False) == 0.0


@pytest.mark.parametrize("snap,expected", [
    # 开始前（空闲/待命）
    ({"active": False, "total": 0, "done": 0, "ok": 0, "finished_at": 0,
      "title": "", "paused": False, "stopping": False, "stopped": False}, 0.0),
    # 获取中
    ({"active": True, "total": 1, "done": 0, "ok": 0, "title": "抓取 JD 正文",
      "paused": False, "stopping": False, "finished_at": 0, "stopped": False}, 0.5),
    # 获取中且暂停：仍是 50%（暂停不改进度，只改颜色）
    ({"active": True, "total": 1, "done": 0, "ok": 0, "title": "x", "paused": True,
      "stopping": False, "finished_at": 0, "stopped": False}, 0.5),
    # 完成
    ({"active": False, "total": 1, "done": 1, "ok": 1, "finished_at": 1, "title": "x",
      "paused": False, "stopping": False, "stopped": False}, 1.0),
    # 失败也算「跑完了」→ 100%（失败信息在文字行里体现）
    ({"active": False, "total": 1, "done": 1, "ok": 0, "failed": 1, "finished_at": 1,
      "title": "x", "paused": False, "stopping": False, "stopped": False}, 1.0),
    # 被结束：停在 50%
    ({"active": False, "total": 1, "done": 0, "ok": 0, "finished_at": 1, "title": "x",
      "paused": False, "stopping": False, "stopped": True}, 0.5),
])
def test_render_single_job_bar_three_states(snap, expected):
    """渲染层验证：单项任务在任意两个时间点取到的值都属于 {0, 0.5, 1}。"""
    del fills[:]
    f = _fake_hud()
    hud.Hud.render(f, snap)
    assert fills and fills[-1] == expected, (snap, fills)


def test_render_single_job_bar_does_not_jitter():
    """回归：同一快照重复渲染必须给出同一个比例（旧版靠 tick 推进 → 每轮都变）。"""
    snap = {"active": True, "total": 1, "done": 0, "ok": 0, "title": "抓取 JD 正文",
            "paused": False, "stopping": False, "finished_at": 0, "stopped": False}
    seen = set()
    for _ in range(8):
        del fills[:]
        f = _fake_hud()
        hud.Hud.render(f, snap)
        seen.add(fills[-1])
    assert seen == {0.5}, f"单项任务进度条不应随时间变化，实际 {seen}"


def test_render_single_job_values_are_always_one_of_three():
    """穷举单项任务的状态组合：取值只可能是 0 / 0.5 / 1（用户要求「只有三种进度」）。"""
    got = set()
    for active in (True, False):
        for finished in (True, False):
            for stopped in (True, False):
                for done in (0, 1):
                    snap = {"active": active, "total": 1, "done": done, "ok": done,
                            "finished_at": 1 if finished else 0, "title": "x",
                            "paused": False, "stopping": False, "stopped": stopped}
                    del fills[:]
                    f = _fake_hud()
                    hud.Hud.render(f, snap)
                    got |= set(fills)
    assert got <= {0.0, 0.5, 1.0}, got
