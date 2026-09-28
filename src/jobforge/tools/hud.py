"""JobForge 悬浮进度窗（独立进程）：抓取时的置顶进度显示 + 暂停/继续/结束。

为什么要独立进程 + 三条窗口约束（2026-09-27 决策，三条均经注入点击探针实测定案——
探针脚本未入库、已随 .trash 清理，结论见记忆库 Lessons/Windows悬浮窗穿透与不抢焦点）：
原生通道靠注入真实键鼠操作 Chrome，任何抢焦点或吃点击的窗口都会打断抓取。
  1 永不抢焦点：WS_EX_NOACTIVATE + WS_EX_TOOLWINDOW（且不进 Alt+Tab）
  2 面板区域点击穿透：WS_EX_TRANSPARENT（跨进程唯一可行做法——WM_NCHITTEST
    返回 HTTRANSPARENT 只在同线程内传递，探针实测跨进程会让点击凭空消失）
  3 按钮可点但不激活窗口：按钮条独立小窗，SetWindowRgn 裁成按钮矩形；矩形
    之外（含按钮间空隙）不属于窗口 → 不参与命中测试 → 穿透

与 server 交互：轮询 GET /api/scrape-progress（600ms），按钮 POST
/api/scrape-control。任务结束后停留数秒展示结果再自动关闭；✕ 立即关闭
（不影响任务）。拖 ⠿ 手柄移动窗口，位置记在 data/hud_pos.json。
调试/自测：环境变量 JOBFORGE_API 换服务地址；JOBFORGE_HUD_DEBUG=1 启动时
把窗口与按钮矩形打到 stdout，供自动化脚本定位点击。
"""
import ctypes
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import urllib.request

from jobforge import paths

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # 与 fetch_jd_native 一致：物理像素坐标
except Exception:
    pass

POS_FILE = paths.data("hud_pos.json")
POLL_MS = 600
LINGER_MS = 9000        # 任务结束后展示结果的停留时长（手动打开且无任务时不自动关，留给用户点 ✕）

USER32 = ctypes.windll.user32
GDI32 = ctypes.windll.gdi32

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x1, 0x2, 0x10, 0x20
HWND_TOPMOST = -1
RGN_OR = 2
SPI_GETWORKAREA = 0x0030

USER32.SetWindowLongPtrW.restype = ctypes.c_void_p
USER32.SetWindowLongPtrW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p)
USER32.GetWindowLongPtrW.restype = ctypes.c_void_p
USER32.GetWindowLongPtrW.argtypes = (ctypes.c_void_p, ctypes.c_int)
USER32.SetWindowPos.restype = ctypes.c_int
USER32.SetWindowPos.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint)
GDI32.CreateRectRgn.restype = ctypes.c_void_p
GDI32.CreateRectRgn.argtypes = (ctypes.c_int,) * 4
GDI32.CombineRgn.restype = ctypes.c_int
GDI32.CombineRgn.argtypes = (ctypes.c_void_p,) * 3 + (ctypes.c_int,)
GDI32.DeleteObject.argtypes = (ctypes.c_void_p,)
USER32.SetWindowRgn.restype = ctypes.c_int
USER32.SetWindowRgn.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int)


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


# ---------- 尺寸（逻辑像素，最终乘 DPI 缩放 S） ----------
W = 420
PANEL_H = 138
BAR_H = 46
BTN = {                        # 按钮条内的矩形：名称 → (x, y, w, h)
    "drag":  (10, 8, 34, 30),
    "pause": (140, 8, 110, 30),
    "stop":  (258, 8, 110, 30),
    "close": (376, 8, 30, 30),
}


def _dpi() -> int:
    try:
        return int(USER32.GetDpiForSystem())
    except Exception:
        try:
            hdc = USER32.GetDC(0)
            return int(GDI32.GetDeviceCaps(hdc, 88))   # LOGPIXELSX
        except Exception:
            return 96


def _workarea():
    r = RECT()
    try:
        if USER32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0):
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return 0, 0, USER32.GetSystemMetrics(0), USER32.GetSystemMetrics(1)


def _top_level(hwnd):
    for _ in range(8):
        p = USER32.GetParent(hwnd)
        if not p:
            return hwnd
        hwnd = p
    return hwnd


def _add_ex(hwnd, bits):
    ex = USER32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) or 0
    USER32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ctypes.c_void_p(ex | bits))
    USER32.SetWindowPos(hwnd, ctypes.c_void_p(HWND_TOPMOST), 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED)


def _api(path: str, payload=None, timeout=2.5):
    base = (os.environ.get("JOBFORGE_API") or "http://127.0.0.1:8080").rstrip("/")
    if payload is None:
        req = urllib.request.Request(base + path)
    else:
        req = urllib.request.Request(
            base + path, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _trunc(s, n):
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _mmss(sec):
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}" if sec >= 60 else f"{sec}s"


def _pause_action(paused) -> str:
    """同一按钮两态：未暂停时点它＝暂停，已暂停时点它＝继续（纯函数便于测试）。"""
    return "resume" if paused else "pause"


def _pause_label(paused) -> str:
    return "▶ 继续" if paused else "⏸ 暂停"


def _bar_fraction(total: int, done: int, active: bool, finished: bool,
                  stopped: bool) -> float:
    """进度条填充比例（0~1）。

    多项任务（批量分析）按真实完成比例 done/total。
    单项任务（抓单个岗位 JD／单页列表）没有可观测的中间刻度，**只有三档**：
    开始前 0% → 获取中 50% → 完成后 100%（被结束时停在 50%：开始过但没跑完）。
    早期版本对单项任务放「不确定进度」来回滚动的动画，观感像横跳且看不出进度
    （2026-09-27 用户要求改为固定三档）。
    """
    if total > 1:
        return max(0.0, min(1.0, done / total))
    if active:
        return 0.5
    if finished:
        return 0.5 if stopped else 1.0
    return 0.0


# 按钮配色：可用态 / 禁用态（tk 的 disabled 不改底色，需显式设置，否则「结束」
# 禁用时仍是亮红，看起来还能点——2026-09-27 截图实测）
BTN_COLORS = {
    "pause_on":  ("#1f6feb", "#388bfd"),
    "pause_res": ("#238636", "#2ea043"),
    "stop_on":   ("#b62324", "#da3633"),
    "off_bg":    "#222a36",
    "off_fg":    "#5a6675",
}


def _style_button(btn, enabled: bool, text: str, on_bg: str, on_active: str):
    """统一按可用/禁用切换按钮外观与文字（禁用态用暗底灰字）。"""
    if enabled:
        btn.configure(state="normal", text=text, bg=on_bg, fg="white",
                      activebackground=on_active, activeforeground="white")
    else:
        btn.configure(state="disabled", text=text, bg=BTN_COLORS["off_bg"],
                      fg=BTN_COLORS["off_fg"], disabledforeground=BTN_COLORS["off_fg"])


class Hud:
    def __init__(self):
        self.S = max(1.0, _dpi() / 96.0)
        self.q = queue.Queue()
        self.seen_active = False
        self.close_job = None
        self.fails = 0
        self.drag_origin = None
        self.last_state = {}

        s = self.S
        self.w_px, self.ph_px, self.bh_px = int(W * s), int(PANEL_H * s), int(BAR_H * s)
        self.x, self.y = self._init_pos()

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.geometry(f"{self.w_px}x{self.ph_px}+{self.x}+{self.y}")
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.96)
        self.root.configure(bg="#1b2230")             # 1px 视觉描边由外层 bg 透出
        try:
            self.root.tk.call("tk", "scaling", _dpi() / 72.0)
        except Exception:
            pass

        inner = tk.Frame(self.root, bg="#0f1319")
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        def lbl(x, y, text, size, color, bold=False, anchor="nw", width=0,
                place_anchor="nw"):
            f = tk.Label(inner, text=text, bg="#0f1319", fg=color,
                         font=("Microsoft YaHei UI", size, "bold" if bold else "normal"),
                         anchor=anchor, justify="left")
            # 注意：anchor 是文字在控件内的对齐，place_anchor 才是控件相对 (x,y) 的
            # 定位方式——右对齐的状态芯片必须用 place_anchor="ne"，否则控件从 (x,y)
            # 向右铺开、超出窗口被裁（2026-09-27 截图实测「待命」只剩「待」）
            opts = dict(x=int(x * s), y=int(y * s), anchor=place_anchor)
            if width:
                opts["width"] = int(width * s)
            f.place(**opts)
            return f

        self.l_title = lbl(16, 13, "JobForge · 抓取进度", 11, "#e6edf3", bold=True)
        self.l_chip = lbl(W - 16, 15, "", 9, "#8b949e", anchor="e", place_anchor="ne")
        self.bar = tk.Canvas(inner, bg="#0f1319", highlightthickness=0, bd=0)
        self.bar.place(x=int(16 * s), y=int(45 * s), width=int((W - 32) * s), height=int(10 * s))
        self.bar_bg = self.bar.create_rectangle(0, 0, int((W - 32) * s), int(10 * s),
                                                fill="#1f2733", outline="")
        self.bar_fill = self.bar.create_rectangle(0, 0, 0, int(10 * s), fill="#ff7a18", outline="")
        self.l_stats = lbl(16, 64, "等待任务…", 9, "#c9d1d9")
        self.l_cur = lbl(16, 88, "", 9, "#8b949e")
        self.l_detail = lbl(16, 110, "", 9, "#6e7681")

        # ---------- 按钮条（独立小窗，裁形后可点区域＝按钮矩形） ----------
        self.bar_win = tk.Toplevel(self.root)
        self.bar_win.overrideredirect(True)
        self.bar_win.geometry(f"{self.w_px}x{self.bh_px}+{self.x}+{self.y + self.ph_px}")
        self.bar_win.attributes("-topmost", True)
        self.bar_win.configure(bg="#171d27")

        def mkbtn(name, text, bg, active, cmd, size=9, fg="#e6edf3"):
            x, y, w, h = BTN[name]
            b = tk.Button(self.bar_win, text=text, bg=bg, fg=fg, relief="flat", bd=0,
                          activebackground=active, activeforeground="#e6edf3",
                          disabledforeground=BTN_COLORS["off_fg"],
                          font=("Microsoft YaHei UI", size), command=cmd,
                          highlightthickness=0, overrelief="flat")
            b.place(x=int(x * s), y=int(y * s), width=int(w * s), height=int(h * s))
            if os.environ.get("JOBFORGE_HUD_DEBUG"):
                # 自测用：把控件收到的原始鼠标事件与 command 触发都打到 stdout
                b.bind("<ButtonPress-1>", lambda e, n=name: print(f"DBG press {n}", flush=True), add="+")
                b.bind("<ButtonRelease-1>", lambda e, n=name: print(f"DBG release {n}", flush=True), add="+")
                b.bind("<Enter>", lambda e, n=name: print(f"DBG enter {n}", flush=True), add="+")
            return b

        # 拖动/关闭是多功能键：用暗底浅字，与主操作按钮（亮色）区分
        self.b_drag = mkbtn("drag", "⠿", BTN_COLORS["off_bg"], "#2d3846", lambda: None, size=11)
        self.b_pause = mkbtn("pause", "⏸ 暂停", *BTN_COLORS["pause_on"], self.on_pause, size=9)
        self.b_stop = mkbtn("stop", "⏹ 结束", *BTN_COLORS["stop_on"], self.on_stop, size=9)
        self.b_close = mkbtn("close", "✕", BTN_COLORS["off_bg"], "#3d2b2b", self.on_close, size=10)
        # 初始没有任务：两个主按钮先按禁用态呈现（render 每轮会重算）
        _style_button(self.b_pause, False, "⏸ 暂停", *BTN_COLORS["pause_on"])
        _style_button(self.b_stop, False, "⏹ 结束", *BTN_COLORS["stop_on"])

        self.b_drag.bind("<ButtonPress-1>", self.drag_start)
        self.b_drag.bind("<B1-Motion>", self.drag_move)
        self.b_drag.bind("<ButtonRelease-1>", self.drag_end)

        self.root.update()
        self.bar_win.update()
        self.panel_hwnd = _top_level(self.root.winfo_id())
        self.bar_hwnd = _top_level(self.bar_win.winfo_id())
        self._apply_styles()
        self._shape_buttons()

        if os.environ.get("JOBFORGE_HUD_DEBUG"):
            btns = {k: [int(v[0] * s) + self.x, int(v[1] * s) + self.y + self.ph_px,
                        int((v[0] + v[2]) * s) + self.x, int((v[1] + v[3]) * s) + self.y + self.ph_px]
                    for k, v in BTN.items()}
            print(json.dumps({"panel": [self.x, self.y, self.w_px, self.ph_px],
                              "bar": [self.x, self.y + self.ph_px, self.w_px, self.bh_px],
                              "buttons": btns, "pid": os.getpid(),
                              "panel_hwnd": self.panel_hwnd, "bar_hwnd": self.bar_hwnd},
                             ensure_ascii=False), flush=True)
            # 窗口移动后重新输出矩形，供自测脚本在拖动后定位按钮
            self._debug_dump = lambda: print(json.dumps(
                {"moved": True, "panel": [self.x, self.y, self.w_px, self.ph_px],
                 "buttons": {k: [int(v[0] * s) + self.x, int(v[1] * s) + self.y + self.ph_px,
                                 int((v[0] + v[2]) * s) + self.x,
                                 int((v[1] + v[3]) * s) + self.y + self.ph_px]
                             for k, v in BTN.items()}}, ensure_ascii=False), flush=True)

        self.root.after(100, self.drain)
        threading.Thread(target=self._poll_loop, daemon=True).start()

    # ---------- 位置 ----------
    def _init_pos(self):
        try:
            with open(POS_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            x, y = int(d["x"]), int(d["y"])
            l, t, r, b = _workarea()
            if l - 200 <= x <= r and t - 200 <= y <= b:      # 副屏拔掉等情形兜底
                return x, y
        except Exception:
            pass
        l, t, r, b = _workarea()
        return r - self.w_px - int(20 * self.S), b - (self.ph_px + self.bh_px) - int(20 * self.S)

    def _save_pos(self):
        try:
            with open(POS_FILE, "w", encoding="utf-8") as f:
                json.dump({"x": self.x, "y": self.y}, f)
        except Exception:
            pass

    def drag_start(self, e):
        self.drag_origin = (e.x_root, e.y_root, self.x, self.y)

    def drag_move(self, e):
        if not self.drag_origin:
            return
        ox, oy, px, py = self.drag_origin
        self.move_to(px + e.x_root - ox, py + e.y_root - oy)

    def drag_end(self, _e):
        self.drag_origin = None
        self._save_pos()
        d = getattr(self, "_debug_dump", None)
        if d:
            d()

    def move_to(self, x, y):
        self.x, self.y = int(x), int(y)
        self.root.geometry(f"+{self.x}+{self.y}")
        self.bar_win.geometry(f"+{self.x}+{self.y + self.ph_px}")

    # ---------- 三条窗口约束 ----------
    def _apply_styles(self):
        # 面板：置顶 + 不激活 + 不进 Alt+Tab + 整窗不吃鼠标（点击直接落到下层 Chrome）
        _add_ex(self.panel_hwnd, WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
                | WS_EX_TRANSPARENT | WS_EX_LAYERED)
        # 按钮条：置顶 + 不激活（区域靠 SetWindowRgn 裁，不能用 TRANSPARENT）
        _add_ex(self.bar_hwnd, WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)

    def _shape_buttons(self):
        """把按钮条的可点区域裁成各按钮矩形的并集；其余（含按钮间空隙）穿透。"""
        rects = []
        for x, y, w, h in BTN.values():
            rects.append((int(x * self.S), int(y * self.S),
                          int((x + w) * self.S), int((y + h) * self.S)))
        full = None
        for (l, t, r, b) in rects:
            rgn = GDI32.CreateRectRgn(l, t, r, b)
            if full is None:
                full = rgn
            else:
                GDI32.CombineRgn(full, full, rgn, RGN_OR)
                GDI32.DeleteObject(rgn)
        USER32.SetWindowRgn(self.bar_hwnd, full, 1)

    # ---------- 轮询 ----------
    def _poll_loop(self):
        while True:
            try:
                self.q.put(("state", _api("/api/scrape-progress")))
            except Exception as e:
                self.q.put(("fail", f"{type(e).__name__}"))
            time.sleep(POLL_MS / 1000.0)

    def _post_action(self, action):
        """按钮动作：POST 后只把「结果」记下（不塞进状态队列——控制响应的结构
        与进度快照完全不同，混在一起会让界面被当成「无任务」渲染并误关窗）。"""
        try:
            _api("/api/scrape-control", {"action": action})
            self.q.put(("action_ok", action))
        except Exception as e:
            self.q.put(("action_fail", f"{action}: {type(e).__name__}"))

    def on_pause(self):
        act = _pause_action(self.last_state.get("paused"))
        self.l_detail.configure(text="已发送暂停指令…" if act == "pause" else "正在恢复…")
        self.b_pause.configure(state="disabled")
        threading.Thread(target=self._post_action, args=(act,), daemon=True).start()

    def on_stop(self):
        self.l_detail.configure(text="正在结束…")
        self.b_stop.configure(state="disabled", text="⏹ 结束中")
        threading.Thread(target=self._post_action, args=("stop",), daemon=True).start()

    def on_close(self):
        self.root.destroy()
        os._exit(0)          # 轮询线程是 daemon，但 _exit 保证不留残留

    # ---------- 渲染 ----------
    def drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "state":
                    self.fails = 0
                    self.render(payload)
                elif kind == "action_ok":
                    # 动作已被服务端接受，界面等下一轮轮询刷新（失败也靠轮询纠偏）
                    self.fails = 0
                elif kind == "action_fail":
                    self.l_detail.configure(text=f"操作未送达（{payload}）")
                    self.fails += 1
                else:
                    self.fails += 1
                    if self.fails >= 8:
                        self.l_chip.configure(text="⚠ 服务已断开", fg="#f85149")
                        self.l_detail.configure(text="无法连接 JobForge 工作台，可能服务已退出")
        except queue.Empty:
            pass
        self.root.after(120, self.drain)

    def _set_fill(self, frac, color):
        total = int((W - 32) * self.S)
        self.bar.itemconfigure(self.bar_fill, fill=color)
        self.bar.coords(self.bar_fill, 0, 0, int(total * max(0.0, min(1.0, frac))), int(10 * self.S))

    def _schedule_close(self, ms):
        if self.close_job is not None:
            return
        self.close_job = self.root.after(ms, self.on_close)

    def render(self, s):
        # 注意：进度快照里的 "ok" 是「成功条数」（空闲时为 0），不是响应成功标志
        # ——早期版本在这里判 falsy 直接 return，导致空闲窗口永远不自动关闭
        # （自测脚本的桩数据恰好 ok=3 掩盖了它）。
        if not isinstance(s, dict):
            return
        self.last_state = s

        active = bool(s.get("active"))
        paused = bool(s.get("paused"))
        stopping = bool(s.get("stopping"))
        title = s.get("title") or "抓取任务"
        total, done = int(s.get("total") or 0), int(s.get("done") or 0)
        ok, failed = int(s.get("ok") or 0), int(s.get("failed") or 0)

        if active:
            self.seen_active = True
            if self.close_job is not None:
                self.root.after_cancel(self.close_job)
                self.close_job = None
            self.l_title.configure(text=f"JobForge · {_trunc(title, 26)}")
            # 按钮可用性每轮按真实状态重算：动作发出后的临时 disabled 靠这里恢复
            _style_button(self.b_stop, not stopping, "⏹ 结束中" if stopping else "⏹ 结束",
                          *BTN_COLORS["stop_on"])
            if stopping:
                self.l_chip.configure(text="⏹ 停止中", fg="#f85149")
            elif paused:
                self.l_chip.configure(text="⏸ 已暂停", fg="#d29922")
            else:
                self.l_chip.configure(text="● 运行中", fg="#3fb950")
            # 停止中不给暂停（已经要结束了，暂停没有意义，按了只会让人困惑）
            _style_button(self.b_pause, not stopping, _pause_label(paused),
                          *(BTN_COLORS["pause_res"] if paused else BTN_COLORS["pause_on"]))
            self.l_stats.configure(text=(
                f"{done}/{total} 已完成 · 成功 {ok}" + (f" · 失败 {failed}" if failed else "")))
            eta = s.get("eta_sec")
            if eta:
                self.l_stats.configure(text=self.l_stats.cget("text") + f" · 剩余约 {_mmss(eta)}")
            self.l_cur.configure(text="▸ " + _trunc(s.get("current") or "", 34) if s.get("current") else "")
            detail = s.get("detail") or ""
            until = s.get("detail_until_ts")
            if until:
                left = int(until - time.time())
                if left > 0:
                    detail = f"{s.get('phase') or '等待中'}（还剩 {left}s）"
            elif s.get("phase"):
                detail = s.get("phase") or detail
            # 暂停/停止中要说清「现在为什么不动了」，否则这一行会空着让人以为卡死
            if paused:
                detail = "已暂停 · 点「继续」接着跑（节流等待不计时）"
            elif stopping:
                detail = "正在结束当前动作，稍候 1~2 秒…"
            self.l_detail.configure(text=_trunc(detail, 44))
            # 进度条比例统一由 _bar_fraction 决定：单项任务固定三档（0/50/100%），
            # 多项任务按真实比例——不再有来回滚动的不确定动画
            frac = _bar_fraction(total, done, active=True, finished=False, stopped=stopping)
            self._set_fill(frac, "#d29922" if paused else "#ff7a18")
            return

        # ---------- 非运行态 ----------
        fin = int(s.get("finished_at") or 0)
        _style_button(self.b_pause, False, "⏸ 暂停", *BTN_COLORS["pause_on"])
        _style_button(self.b_stop, False, "⏹ 结束", *BTN_COLORS["stop_on"])
        if fin:
            stopped, err = bool(s.get("stopped")), s.get("last_error") or ""
            self.l_title.configure(text=f"JobForge · {_trunc(title, 26)}")
            if stopped:
                self.l_chip.configure(text="⏹ 已结束", fg="#8b949e")
                self.l_stats.configure(text=f"结束前完成 {done}/{total} · 成功 {ok}"
                                            + (f" · 失败 {failed}" if failed else ""))
                self.l_detail.configure(text=_trunc(err, 44) or "已按你的要求停止")
            else:
                self.l_chip.configure(text="✅ 已完成", fg="#3fb950")
                self.l_stats.configure(text=f"共 {total} 个 · 成功 {ok}"
                                            + (f" · 失败 {failed}" if failed else ""))
                self.l_detail.configure(text="窗口稍后自动关闭")
            self.l_cur.configure(text="")
            # 完成后 100%；被结束时停在「开始过但没跑完」那一档（单项 50%／多项按比例）
            self._set_fill(_bar_fraction(total, done, active=False, finished=True, stopped=stopped),
                           "#3fb950" if not stopped else "#8b949e")
            self._schedule_close(LINGER_MS)
            return

        # 待命（还没开始跑）：进度条归零，对应「开始前 0%」
        self._set_fill(0.0, "#8b949e")
        self.l_chip.configure(text="待命", fg="#8b949e")
        if not self.seen_active:
            # 手动打开、还没等到任务：保持开着（用户是主动点开的，自动消失会莫名其妙），
            # 它不抢焦点也不挡鼠标，留着无害
            self.l_stats.configure(text="当前没有进行中的抓取任务")
            self.l_detail.configure(text="开始抓取时会自动显示进度 · 点 ✕ 关闭")
        else:
            self._schedule_close(LINGER_MS)


def main():
    try:
        hud = Hud()
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        return 1
    hud.root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
