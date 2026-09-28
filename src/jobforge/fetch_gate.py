"""跨进程抓取闸门：暂停 / 停止信号（悬浮窗 → server → 抓取代码）。

为什么用文件（2026-09-27 决策）：抓取有两种形态——JD 抓取跑在子进程
（fetch_jd.py → fetch_jd_native），岗位列表抓取跑在 server 进程内的线程
（spider.crawl_boss → crawl_boss_native）。两种都要能被「暂停/结束」按到，
线程内可用 Event，子进程只能用文件，故统一以文件为单一信号源——沿用
fetch_throttle.json 已验证的跨进程约定。文件极小、只在安全点读，开销可忽略。

语义：
  paused=true  → checkpoint 原地阻塞等待（恢复或停止才返回），期间不注入键鼠
  stopped=true → checkpoint raise Stopped，调用方转结构化错误返回

安全点约定：只在「两个注入动作之间」调用 checkpoint——不加在
Ctrl+L/粘贴/回车等连续按键序列中间，避免把一次导航撕成两半。

写侧：server 的暂停/恢复/结束端点；读侧：fetch_jd_native 的安全点。
路径可用环境变量 JOBFORGE_GATE_FILE 覆盖（测试指向临时文件，不污染真实状态）。
"""
import json
import os
import time

from jobforge import paths

DEFAULT_GATE_FILE = paths.data("fetch_gate.json")


class Stopped(Exception):
    """停止信号：抓取在安全点主动退出（区别于风控/网络等失败）。"""


def gate_path() -> str:
    return os.environ.get("JOBFORGE_GATE_FILE") or DEFAULT_GATE_FILE


def _read() -> dict:
    try:
        with open(gate_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write(state: dict):
    p = gate_path()
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, p)   # 原子替换：读侧不会读到半个文件
    except Exception:
        pass


def paused() -> bool:
    return bool(_read().get("paused"))


def stopped() -> bool:
    return bool(_read().get("stopped"))


def set_state(paused=None, stopped=None):
    """写信号（只改传入的字段）。读写都在同一文件，保持单一事实来源。"""
    st = _read()
    if paused is not None:
        st["paused"] = bool(paused)
    if stopped is not None:
        st["stopped"] = bool(stopped)
    st["updated_at"] = time.time()
    _write(st)


def clear():
    """任务开始时复位（上一次任务的 stopped 不复用到新任务）。"""
    _write({"paused": False, "stopped": False, "updated_at": time.time()})


def checkpoint(poll: float = 0.4):
    """安全点：暂停则阻塞；停止则 raise Stopped。返回后可继续注入键鼠。"""
    while True:
        st = _read()
        if st.get("stopped"):
            raise Stopped("已停止（用户手动结束）")
        if not st.get("paused"):
            return
        time.sleep(poll)


def wait_pausable(seconds: float, poll: float = 0.5):
    """可暂停的等待：暂停时长不计入剩余（节流窗口不被暂停吃掉），
    停止时最迟 poll 秒内 raise Stopped。"""
    remain = float(seconds)
    while remain > 0:
        checkpoint(poll)
        step = min(poll, remain)
        time.sleep(step)
        remain -= step
