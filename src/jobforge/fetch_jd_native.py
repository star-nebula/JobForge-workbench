"""Computer Use 通道：OS 原生键鼠操控用户已登录的 Chrome 抓 BOSS JD 正文。

设计目标（2026-09-25 用户决策）：全程不碰 CDP/Playwright，浏览器进程内零自动化
痕迹；输入事件由 Windows 层注入（isTrusted=true），页面侧与真人操作不可区分。
定位用 UIA 无障碍树，取数用 Ctrl+A/Ctrl+C 剪贴板，不做视觉识别、不引外部服务。

行为契约：fetch_jd_native(job_id, job_url, title, company) 返回 {ok, jd?, error?}，
与 fetch_jd.py 一致；窗口缺失/风控/解析失败时返回结构化 error，由调用方兜底。

节流是主防护：被风控的主因是频率（见 fetch_jd.py 头注释「大量开 tab 被临时风控」），
跨进程共享 fetch_throttle.json，两次抓取至少间隔随机 18~35 秒，每次调用先认领时段。

流程：
  1. 节流等待 → 2. 找到打开了 zhipin.com 的 Chrome 窗口并聚焦（UIA 读标签栏，
  可命中非活动 tab）→ 3. 浏览器已开着目标岗位详情 tab（上次遗留/用户手开）时
  直接切过去复用，免导航请求 → 4. 路径A：地址栏直跳 job_detail URL（真人贴
  链接行为，无 securityId 也可能放行）→ 5. 被弹 about:blank 或解析不到正文
  时，路径B：打开搜索列表页（query 参数与真人搜索产生的 URL 一致），UIA 按
  标题+公司定位卡片，滚到可见后中键点职位名链接——中键走浏览器原生行为后台
  开新 tab，绕过 BOSS 对左键导航的 JS 静默拦截（v8~v10 对照实验定案）；新
  tab 标题过校验，被服务端 302 弹到 IP 归属地分站则关掉后对同链接重试一次
  → 6. 详情页 Ctrl+A/Ctrl+C，正则截取「职位描述」段，抓完 Ctrl+W 关 tab
  恢复现场（复用的已有 tab 不关）。

前提：Chrome 已打开并登录 zhipin.com（优先用日常浏览器的窗口，设备指纹最真实）。
注意：抓取期间会临时接管键鼠约 20~60 秒，请勿操作电脑；剪贴板会被临时占用并在
结束后恢复。自检：python -m jobforge.fetch_jd_native --check（只查窗口/节流，不导航）。
"""
import ctypes
import json
import random
import re
import sys
import time
from urllib.parse import quote

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

import pyautogui
import pygetwindow as gw
import pyperclip
from pywinauto import Application

from jobforge import fetch_gate, paths

THROTTLE_FILE = paths.data("fetch_throttle.json")
THROTTLE_MIN, THROTTLE_MAX = 18, 35
SEARCH_URL = "https://www.zhipin.com/web/geek/jobs?query={q}&city=100010000"
JD_HEADS = ("职位描述", "职位详情", "岗位职责")
JD_TAILS = ("看了该职位的还看了", "看了该职位的人还看了", "同类职位", "相似职位",
            "该公司的其他职位", "面试评价", "技能解析", "温馨提示", "简历处理",
            "工作地址", "查看更多信息", "求职工具", "升级VIP", "热门职位",
            "与BOSS随时沟通", "去App", "前往App",
            "公司介绍", "福利待遇", "工商信息", "竞争力分析")  # JD 正文后的天然板块边界（与 fetch_jd.clean_jd 哨兵对齐：JD 只留正文）
MAX_JD_LEN = 20000
SID_RE = re.compile(r'"securityId"\s*:\s*"([^"]+)"')
LID_RE = re.compile(r'"lid"\s*:\s*"([^"]+)"')

_user32 = ctypes.windll.user32


class NativeError(Exception):
    pass


def throttle_due_in() -> float:
    try:
        with open(THROTTLE_FILE, "r", encoding="utf-8") as f:
            return max(0.0, json.load(f).get("next_ok_ts", 0) - time.time())
    except Exception:
        return 0.0


def throttle_wait(max_wait: float = 60):
    """等节流窗口到期再认领下一个时段。等待本身可暂停（暂停时长不算进节流，
    否则恢复后还要再等一整轮）、可被「结束」打断（最迟 0.5s raise Stopped）。"""
    wait = min(throttle_due_in(), max_wait)
    if wait > 0:
        fetch_gate.wait_pausable(wait + random.uniform(0.5, 2.0))
    fetch_gate.checkpoint()      # 无节流等待时也要过闸：暂停/结束必须立刻可感
    try:
        with open(THROTTLE_FILE, "w", encoding="utf-8") as f:
            json.dump({"next_ok_ts": time.time() + random.uniform(THROTTLE_MIN, THROTTLE_MAX)}, f)
    except Exception:
        pass


def _chrome_windows():
    out = []
    for w in gw.getAllWindows():
        t = w.title or ""
        if t.endswith("- Google Chrome") and w.visible:
            out.append(w)
    return out


def _window_title(w) -> str:
    hwnd = w._hWnd
    n = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    _user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value or ""


def find_zhipin_tab():
    """返回已打开 zhipin 的 Chrome 窗口（pygetwindow Window，已聚焦）。
    先按窗口标题（活动 tab 含「直聘」）找；找不到再用 UIA 遍历标签栏，
    命中非活动 tab 时用 Select 原生切换过去。"""
    cands = [w for w in _chrome_windows() if "直聘" in w.title]
    if not cands:
        for w in _chrome_windows():
            try:
                # 最小化窗口的 UIA 树枚举不可靠（TabItem 拿不到/挂起）——先恢复再遍历。
                # restore 是抓取流程必然动作（尾部还要 activate），提前不做也无济于事。
                if w.isMinimized:
                    w.restore()
                    time.sleep(random.uniform(0.4, 0.9))
                app = Application(backend="uia").connect(handle=w._hWnd, timeout=3)
                for tab in app.window(handle=w._hWnd).descendants(control_type="TabItem"):
                    if "直聘" in (tab.window_text() or ""):
                        tab.select()
                        time.sleep(random.uniform(0.8, 1.5))
                        cands.append(w)
                        break
            except Exception:
                continue
            if cands:
                break
    if not cands:
        raise NativeError("未找到打开 zhipin.com 的 Chrome 窗口，请先打开并登录 BOSS 直聘")
    w = cands[0]
    try:
        if w.isMinimized:
            w.restore()
            time.sleep(random.uniform(0.5, 1.0))
        w.activate()
    except Exception:
        pass
    time.sleep(random.uniform(0.4, 0.9))
    return w


def _goto(url):
    """地址栏导航：Ctrl+L → 剪贴板粘贴 → 回车，节奏加随机抖动。"""
    pyperclip.copy(url)
    time.sleep(random.uniform(0.3, 0.8))
    pyautogui.hotkey("ctrl", "l")
    time.sleep(random.uniform(0.3, 0.6))
    pyautogui.hotkey("ctrl", "v")
    time.sleep(random.uniform(0.2, 0.5))
    pyautogui.press("enter")


def _wait_title(w, old: str, timeout_s: float = 18) -> str:
    """等活动 tab 标题离开 old 并连续两次读值稳定，返回新标题；超时返回最后读值。"""
    deadline = time.time() + timeout_s
    last, stable = old, 0
    while time.time() < deadline:
        time.sleep(1)
        t = _window_title(w)
        if t != old:
            stable = stable + 1 if t == last else 0
            if stable >= 2:
                return t
        last = t
    return last


def _copy_page_text(w=None) -> str:
    """Esc 收起弹层 → Ctrl+A 全选 → Ctrl+C 复制 → 读剪贴板。要求焦点在页面。
    UIA Select 切 tab 后键盘焦点可能停在标签条上（Ctrl+A/Ctrl+C 落空，剪贴板
    原样不动）——此时按剪贴板是否变化判定，落空就对页面 Document 矩形中心
    补一次物理点击把焦点挪进页面，再重试一轮复制。"""
    pyautogui.press("escape")
    time.sleep(random.uniform(0.4, 0.9))
    try:
        pre = pyperclip.paste()
    except Exception:
        pre = None

    def _grab():
        pyautogui.hotkey("ctrl", "a")
        time.sleep(random.uniform(0.5, 1.2))
        pyautogui.hotkey("ctrl", "c")
        time.sleep(random.uniform(0.8, 1.6))
        try:
            return pyperclip.paste() or ""
        except Exception:
            return ""

    txt = _grab()
    if w is not None and txt == (pre or ""):
        try:
            app = Application(backend="uia").connect(handle=w._hWnd, timeout=5)
            r = app.window(handle=w._hWnd).descendants(
                control_type="Document")[0].rectangle()
            _native_click((r.left + r.right) // 2, (r.top + r.bottom) // 2)
            time.sleep(random.uniform(0.4, 0.8))
            txt = _grab()
        except Exception:
            pass
    return txt


def _page_text_uia(w) -> str:
    """UIA TextPattern 直接读窗口内各 Document 全文——零输入事件、不碰剪贴板。
    BOSS 详情页主内容区（职位描述等）Ctrl+A 选不中（疑似 content-visibility
    懒渲染），但可访问性树里有完整渲染文本，TextPattern 能整段拿到。取最长
    Document 的全文（隐藏 iframe 等小文档不会干扰）。"""
    best = ""
    try:
        app = Application(backend="uia").connect(handle=w._hWnd, timeout=5)
        for d in app.window(handle=w._hWnd).descendants(control_type="Document"):
            try:
                txt = d.iface_text.documentRange.GetText(-1) or ""
            except Exception:
                continue
            if len(txt) > len(best):
                best = txt
    except Exception:
        pass
    return best


def _jd_body_plausible(txt: str) -> bool:
    """TextPattern 文本里 JD 标题之后是否有实质正文。
    BOSS 详情页正文是 content-visibility 懒渲染：渲染未就绪时「职位描述」标题后
    直接是页尾「公司介绍」段——此时标题存在但正文为空，不能采纳，应回退剪贴板
    通道或等待后重试（实测坏样本：488 字纯公司介绍被整段当 JD 缓存）。"""
    t = (txt or "").replace("\r", "")
    pos = -1
    for h in JD_HEADS:
        i = t.find(h)
        if i != -1 and (pos == -1 or i < pos):
            pos = i
    if pos < 0:
        return False
    tail = t.find("公司介绍", pos)
    body = t[pos:tail if tail != -1 else len(t)]
    return len(body.replace("\n", "").replace(" ", "").strip()) >= 80


def _grab_page_text(w) -> str:
    """详情页正文抓取：UIA TextPattern 优先（更隐蔽且能拿到 Ctrl+A 选不中的
    区域），但要求标题后确有实质正文（懒渲染未就绪的空壳不可采纳）；无节标题
    或正文不可信时回退 Ctrl+A/C 剪贴板通道。"""
    txt = _page_text_uia(w)
    if any(h in txt for h in JD_HEADS) and _jd_body_plausible(txt):
        return txt
    return _copy_page_text(w)


def extract_jd(page_text: str) -> str:
    """从整页文本里截「职位描述」段：起点取最早的节标题，终点取其后最早的下节标记。"""
    t = (page_text or "").replace("\r", "")
    pos = -1
    for h in JD_HEADS:
        i = t.find(h)
        if i != -1 and (pos == -1 or i < pos):
            pos = i
    if pos == -1:
        return ""
    body = t[pos + 4:]
    cut = len(body)
    for tail in JD_TAILS:
        i = body.find(tail)
        if i != -1:
            cut = min(cut, i)
    lines = [ln.strip() for ln in body[:cut].split("\n")]
    return "\n".join(ln for ln in lines if ln).strip()


SEO_LINK_WORDS = ("招聘", "工资", "待遇", "全国", "信息", "公司")

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


_user32.WindowFromPoint.argtypes = [_POINT]
_user32.WindowFromPoint.restype = ctypes.c_void_p
_user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
_user32.GetAncestor.restype = ctypes.c_void_p


def _native_click(x: int, y: int):
    """跨屏安全的物理点击。pyautogui.click 按「主屏尺寸」归一化绝对坐标，
    窗口在副屏（负坐标）时点击会落到主屏；移动仍用 moveTo（SetCursorPos
    支持虚拟屏幕全坐标），按键事件不带 MOVE 标志即作用于当前光标位置。"""
    pyautogui.moveTo(x, y, duration=random.uniform(0.15, 0.4))
    time.sleep(random.uniform(0.05, 0.2))
    _user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(random.uniform(0.04, 0.09))
    _user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def _root_window_at(x: int, y: int):
    """(x,y) 处光标下最顶层根窗口句柄（WindowFromPoint 取到的是子窗口，
    须 GetAncestor(GA_ROOT) 归到顶层才能与 pygetwindow 的句柄比较）。"""
    hwnd = _user32.WindowFromPoint(_POINT(int(x), int(y)))
    if not hwnd:
        return 0
    return _user32.GetAncestor(hwnd, 2) or hwnd  # 2 = GA_ROOT


def _middle_click(x: int, y: int):
    """中键点击走浏览器原生行为：后台新 tab 打开链接，不经过页面自己的
    点击路由，从而绕过 BOSS 对左键导航的 JS 静默拦截（v8~v10 对照实验
    定案：同坐标左键零响应、中键稳定开新 tab）。"""
    pyautogui.moveTo(x, y, duration=random.uniform(0.15, 0.4))
    time.sleep(random.uniform(0.05, 0.2))
    _user32.mouse_event(MOUSEEVENTF_MIDDLEDOWN, 0, 0, 0, 0)
    time.sleep(random.uniform(0.04, 0.09))
    _user32.mouse_event(MOUSEEVENTF_MIDDLEUP, 0, 0, 0, 0)


def _tab_titles(win) -> list:
    try:
        return [t.window_text() or "" for t in win.descendants(control_type="TabItem")]
    except Exception:
        return []


def _select_tab(win, want: list) -> bool:
    """把 want（新 tab 标题）对应的 TabItem 切到前台；Chrome 标题可能附带
    「内存用量」尾巴，用互相包含做模糊配对。"""
    for _ in range(3):
        for t in win.descendants(control_type="TabItem"):
            try:
                tn = t.window_text() or ""
            except Exception:
                continue
            if tn and any(a and (a == tn or a in tn or tn in a) for a in want):
                try:
                    t.select()
                    time.sleep(random.uniform(1.2, 2.0))
                    return True
                except Exception:
                    break
        time.sleep(random.uniform(0.5, 1.0))
    return False


def _wait_new_detail_tab(w, win, before: set, title: str, company: str):
    """中键后轮询等新 tab 并切过去。新 tab 标题过 _page_ok：是详情页就返回
    其窗口标题；被服务端 302 到 IP 归属地分站/聚合页（job_detail 请求的典型
    风控表现，2026-09-25 实测被弹「佛山招聘网」）则 Ctrl+W 关掉返回 None，
    由调用方重试；10 秒内没出现新 tab 同样返回 None。"""
    deadline = time.time() + 10
    while time.time() < deadline:
        time.sleep(random.uniform(1.0, 1.8))
        added = [t for t in _tab_titles(win) if t and t not in before]
        if not added:
            continue
        if not _select_tab(win, added):
            raise NativeError("已打开详情 tab 但切换失败")
        t_new = _window_title(w)
        if _page_ok(t_new, title, company):
            return t_new
        _close_active_tab(w)
        time.sleep(random.uniform(2.5, 5.0))
        return None
    return None


def _click_search_result(w, title: str, company: str) -> str:
    """搜索列表页定位目标卡片，中键点其职位名链接（后台新 tab，绕过 BOSS
    对左键导航的 JS 拦截），UIA Select 切到新 tab，返回切过去后的窗口标题。
    卡片定位：搜索页每张职位卡是 ListItem，无障碍名含整卡文本（标题+薪资+
    公司），按标题+公司双条件配对，避免点中别家同名岗位；职位名链接必须在
    目标卡矩形之内（v9 教训：只按窗口范围过滤会点进浏览器 UI 区），中心点
    过 WindowFromPoint 哨兵确认仍在本窗口——滚出视口的 UIA 元素会报陈旧
    矩形，盲信坐标会把点击物理打到别的应用上。不在可视区则滚轮下翻（列表
    无限加载）。切 tab 用标题差集只认本次新开的，已有同内容旧 tab 不干扰；
    新 tab 标题过 _page_ok，被 302 弹分站则关掉后对同一链接重试一次（重试前
    把焦点切回搜索 tab——关 tab 后 Chrome 会落到相邻 tab）。"""
    key = re.sub(r"\s+", "", (title or ""))[:10]
    if not key:
        raise NativeError("岗位标题为空，无法在搜索结果中定位")
    comp = re.sub(r"\s+", "", (company or ""))[:6]
    app = Application(backend="uia").connect(handle=w._hWnd, timeout=5)
    win = app.window(handle=w._hWnd)
    box = w.box
    before = set(_tab_titles(win))
    search_title = _window_title(w)

    def _hittable(r):
        cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
        if not (box.left <= cx <= box.left + box.width
                and box.top <= cy <= box.top + box.height):
            return None
        if _root_window_at(cx, cy) != w._hWnd:
            return None
        return cx, cy

    for _ in range(8):
        anchor = None
        for item in win.descendants(control_type="ListItem"):
            try:
                name = re.sub(r"\s+", "", item.window_text() or "")
            except Exception:
                continue
            if key not in name or (comp and comp not in name):
                continue
            try:
                cr = item.rectangle()
            except Exception:
                continue
            ccx, ccy = (cr.left + cr.right) // 2, (cr.top + cr.bottom) // 2
            if not (box.left <= ccx <= box.left + box.width
                    and box.top <= ccy <= box.top + box.height):
                continue
            for link in item.descendants(control_type="Hyperlink"):
                try:
                    nm = re.sub(r"\s+", "", link.window_text() or "")
                    lr = link.rectangle()
                except Exception:
                    continue
                if key not in nm or len(nm) < 4 or any(b in nm for b in SEO_LINK_WORDS):
                    continue
                if not (lr.left >= cr.left and lr.right <= cr.right
                        and lr.top >= cr.top and lr.bottom <= cr.bottom):
                    continue
                pos = _hittable(lr)
                if pos is None:
                    continue
                for _ in range(2):
                    if _window_title(w) != search_title:
                        _select_tab(win, [search_title])
                    _middle_click(pos[0] + random.randint(-3, 3),
                                  pos[1] + random.randint(-2, 2))
                    t_new = _wait_new_detail_tab(w, win, before, title, company)
                    if t_new:
                        return t_new
                raise NativeError("中键点击未产出可用的详情页（新 tab 被弹分站或未出现），稍后再试")
            anchor = cr
            break
        if anchor is not None:
            sx = max(box.left + 40, min(anchor.left + 40, box.left + box.width - 40))
        else:
            sx = box.left + int(box.width * 0.45)
        pyautogui.moveTo(sx, box.top + int(box.height * 0.6))
        pyautogui.scroll(-900)
        time.sleep(random.uniform(1.2, 2.2))
    raise NativeError("搜索结果里没找到该岗位（可能已下线或排名靠后），可手动打开链接确认")


def _close_active_tab(w):
    """Ctrl+W 关掉当前（详情）tab，Chrome 自动回到相邻 tab 恢复现场；
    失败静默——残留 tab 不影响正确性，下次 fetch 的差集法会视为旧 tab。"""
    try:
        w.activate()
        time.sleep(random.uniform(0.3, 0.8))
        pyautogui.hotkey("ctrl", "w")
        time.sleep(random.uniform(1.0, 2.0))
    except Exception:
        pass


def _page_ok(win_title: str, title: str, company: str) -> bool:
    """落地页校验：只看窗口标题（活动 tab）。详情页标题必含职位名或公司名；
    无 securityId 直跳被弹回的首页/推荐页标题是「BOSS直聘-找工作…」，且正文里
    的推荐卡片可能恰好含目标岗位，故不能用正文做判据。"""
    t = win_title or ""
    if company and company in t:
        return True
    key = re.sub(r"\s+", "", (title or ""))[:8]
    return bool(key) and key in re.sub(r"\s+", "", t)


def _reuse_existing_detail_tab(w, title: str, company: str):
    """浏览器里已开着目标岗位的详情 tab（上次抓取遗留或用户手动打开）时直接
    切过去复用，省掉 job_detail/搜索两次导航——请求足迹最小。配对优先公司名：
    搜索页 tab 标题含查询词但不含公司名，按公司配对不会误复用搜索页；未提供
    公司名才退回职位名前缀。命中并切换成功返回窗口标题，否则 None。复用的
    tab 不负责关闭（可能是用户手开的），留在原处供下次复用。"""
    comp = (company or "").strip()
    key = re.sub(r"\s+", "", (title or ""))[:8]
    if not comp and not key:
        return None
    try:
        app = Application(backend="uia").connect(handle=w._hWnd, timeout=5)
        win = app.window(handle=w._hWnd)
        if comp:
            want = [t for t in _tab_titles(win) if t and comp in t]
        else:
            want = [t for t in _tab_titles(win)
                    if t and key and key in re.sub(r"\s+", "", t)]
    except Exception:
        return None
    if not want or not _select_tab(win, want):
        return None
    t_new = _window_title(w)
    return t_new if _page_ok(t_new, title, company) else None


def _nearest_before(pattern, txt: str, idx: int):
    best = None
    for m in pattern.finditer(txt, 0, idx):
        best = m
    return best


def _detail_url_with_security(w, job_id: str, title: str):
    """view-source 同源读取列表接口，提取目标岗位 securityId 拼完整详情 URL。
    详情页必须带 securityId 否则被 302 弹到 IP 归属地分站；DOM 拿不到（href
    是裸的，真人左键是前端 JS 导航时动态拼参），但列表接口 joblist.json 每个
    岗位对象都带 securityId。浏览器访问 view-source: 时带真实 cookies 同源
    请求该接口，Ctrl+A/C 全选复制即得源码——纯键盘零页面输入事件。securityId
    是岗位对象第一个字段、encryptJobId 靠后，故从 job_id 位置向前找最近的
    "securityId" 即同对象的那个。拿不到（岗位不在第一页/风控）返回 None，
    调用方降级裸 URL。"""
    if not job_id:
        return None
    try:
        q = re.sub(r"\s+", "", (title or ""))[:20] or "招聘"
        api = ("https://www.zhipin.com/wapi/zpgeek/search/joblist.json"
               f"?scene=1&query={quote(q)}&city=100010000&page=1&pageSize=30")
        old = _window_title(w)
        _goto("view-source:" + api)
        _wait_title(w, old, timeout_s=15)
        txt = _copy_page_text(w)
        idx = txt.find(job_id)
        if idx < 0:
            return None
        s = _nearest_before(SID_RE, txt, idx)
        if not s or len(s.group(1)) < 10 or job_id[:6] in s.group(1):
            return None
        tail = f"securityId={s.group(1)}"
        l = _nearest_before(LID_RE, txt, idx)
        if l and l.group(1) and job_id[:6] not in l.group(1):
            tail += f"&lid={l.group(1)}"
        return f"https://www.zhipin.com/job_detail/{job_id}.html?{tail}"
    except Exception:
        return None


def _extract_json_from_source(txt: str) -> dict:
    """从 view-source 页面可见文本还原 joblist.json 的 JSON 对象。
    view-source 渲染文本常带行号前缀（如「1{」「12  {」），TextPattern 与剪贴板
    通道拿到的形态也不同；先按首个 {"code" 起 json.loads，失败再逐行剥
    「行号+空白」前缀拼接重试。两路都失败说明拿到的不是 JSON（风控挑战页/未
    登录跳转），raise NativeError。"""
    txt = (txt or "").replace("\r", "")
    start = txt.find('{"code"')
    if start < 0:
        start = txt.find("{")
    if start < 0:
        raise NativeError("view-source 页未读到 JSON 内容（可能未登录或被风控）")
    candidates = [txt[start:]]
    stripped = "\n".join(re.sub(r"^\s*\d+\s?", "", ln) for ln in txt[start:].split("\n"))
    candidates.append(stripped)
    last_err = None
    for cand in candidates:
        try:
            return json.loads(cand)
        except Exception as e:
            last_err = e
    raise NativeError(f"列表 JSON 解析失败：{type(last_err).__name__}: {last_err}")


def crawl_boss_native(query: str, city_code: str, page: int = 1) -> dict:
    """原生通道抓岗位列表：view-source 同源读 joblist.json，浏览器自带真实
    登录态（含动态 __zp_stoken__），替代已废弃的 requests+静态 cookie 直调
    （2026-09-26 决策：静态 stoken 几分钟失效、直调接口有风控风险）。
    返回 joblist.json 解析后的完整 dict（zpData.jobList 为岗位数组，字段与旧
    通道完全一致，供 spider._normalize_boss 消费）；未登录/风控/解析失败
    raise NativeError。单页 30 条；接管键鼠约 8~15 秒，与 JD 抓取共享 18~35s
    节流。"""
    q = re.sub(r"\s+", "", (query or ""))[:20] or "招聘"
    page = max(1, int(page or 1))
    api = ("https://www.zhipin.com/wapi/zpgeek/search/joblist.json"
           f"?scene=1&query={quote(q)}&city={city_code}&page={page}&pageSize=30")
    throttle_wait()
    fetch_gate.checkpoint()      # 安全点：暂停/结束在导航前生效，不打断键鼠序列
    w = find_zhipin_tab()
    backup = None
    try:
        backup = pyperclip.paste()
    except Exception:
        pass
    try:
        old = _window_title(w)
        _goto("view-source:" + api)
        t = _wait_title(w, old, timeout_s=15)
        if "about:blank" in t:
            raise NativeError("view-source 页被弹成 about:blank（反爬软锁），过段时间再试")
        # TextPattern 优先（零输入事件、不占剪贴板），拿到的不像 JSON 再走剪贴板通道
        txt = _page_text_uia(w)
        if '{"code"' not in txt:
            txt = _copy_page_text(w)
        data = _extract_json_from_source(txt)
        if data.get("code") != 0:
            raise NativeError(f"boss code={data.get('code')} msg={data.get('message', '')}（未登录或被风控）")
        # 把被 view-source 占用的 tab 导航回 BOSS 求职页：tab 标题恢复「直聘」，
        # 下次 find_zhipin_tab 才能找到；也不把用户的 tab 留在源码页。
        # 失败不影响本次结果（数据已到手），只影响下次定位。
        try:
            old2 = _window_title(w)
            _goto("https://www.zhipin.com/web/geek/job")
            _wait_title(w, old2, timeout_s=12)
        except Exception:
            pass
        return data
    finally:
        if backup is not None:
            try:
                pyperclip.copy(backup)
            except Exception:
                pass


def fetch_jd_native(job_id: str, job_url: str, title: str, company: str) -> dict:
    result = {"ok": False, "jd": None, "error": None}
    backup = None
    try:
        backup = pyperclip.paste()
    except Exception:
        pass
    try:
        throttle_wait()
        w = find_zhipin_tab()
        jd = ""
        detail_url = (job_url or "").strip() or f"https://www.zhipin.com/job_detail/{job_id}.html"
        jid = job_id or ""
        if not jid:
            mj = re.search(r"job_detail/([A-Za-z0-9~_\-]+)\.html", detail_url)
            jid = mj.group(1) if mj else ""
        reused = _reuse_existing_detail_tab(w, title, company)
        if reused:
            page_text = _grab_page_text(w)
            if _page_ok(reused, title, company):
                jd = extract_jd(page_text)
        if len(jd.strip()) < 20:
            # 安全点：路径 A 的整段按键序列（Ctrl+L→粘贴→回车）开始前，先把暂停/结束认下来
            fetch_gate.checkpoint()
            old = _window_title(w)
            sec_url = _detail_url_with_security(w, jid, title)
            if sec_url:
                detail_url = sec_url
            _goto(detail_url)
            t1 = _wait_title(w, old, timeout_s=20)
            if "直聘" in t1 and "about:blank" not in t1:
                time.sleep(random.uniform(1.2, 2.2))  # 正文懒渲染需要触发时间，取太早会只剩页尾「公司介绍」
                page_text = _grab_page_text(w)
                if _page_ok(t1, title, company):
                    jd = extract_jd(page_text)
        if len(jd.strip()) < 20:
            # 安全点：路径 B（搜索页定位 + 中键点击）开始前
            fetch_gate.checkpoint()
            q = re.sub(r"\s+", "", (title or ""))[:20] or "招聘"
            old = _window_title(w)
            _goto(SEARCH_URL.format(q=quote(q)))
            t2 = _wait_title(w, old, timeout_s=20)
            if "about:blank" in t2:
                raise NativeError("搜索页被弹成 about:blank（反爬软锁），过段时间再试")
            if "直聘" not in t2:
                raise NativeError("搜索页打开异常（可能被风控或未登录），稍后再试")
            switched = None
            try:
                switched = _click_search_result(w, title, company)
                time.sleep(random.uniform(1.2, 2.2))  # 新开详情 tab 的正文懒渲染同样需要时间
                page_text = _grab_page_text(w)
                if not _page_ok(switched, title, company):
                    raise NativeError("点开的不是目标岗位详情页（标题：%s），可能已下线或被风控" % (switched or "")[:40])
                jd = extract_jd(page_text)
            finally:
                if switched is not None:
                    _close_active_tab(w)
        if len(jd.strip()) > 20:
            result["ok"] = True
            result["jd"] = jd.strip()[:MAX_JD_LEN]
        else:
            result["error"] = "原生通道已打开详情页但未解析到职位描述（页面改版或被风控）"
    except fetch_gate.Stopped as e:
        result["error"] = str(e)
        result["stopped"] = True        # 让上层跳过 CDP/直连兜底，停止秒级生效
    except NativeError as e:
        result["error"] = str(e)
    except pyautogui.FailSafeException:
        result["error"] = "原生通道中止：鼠标被移到屏幕角落（用户接管）"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        if backup is not None:
            try:
                pyperclip.copy(backup)
            except Exception:
                pass
    return result


def check_ready() -> dict:
    """轻量自检（不导航、不抢焦点、不切 tab、不 restore）：Chrome 是否开着
    zhipin 页 + 节流剩余。三态：chrome_ready=true（确认可抓）/ chrome_found=true
    但未确认（Chrome 在，可能最小化——最小化窗口的 UIA 树枚举不可靠，抓取时
    find_zhipin_tab 会 restore 后再定位）/ chrome_found=false（无 Chrome，硬拦）。"""
    info = {"ok": True, "chrome_found": False, "chrome_ready": False,
            "throttle_sec": round(throttle_due_in(), 1), "zhipin_window": None, "error": None}
    try:
        wins = _chrome_windows()
        info["chrome_found"] = bool(wins)
        cands = [w for w in wins if "直聘" in w.title]
        if cands:
            info["zhipin_window"] = cands[0].title
            info["chrome_ready"] = True
        else:
            found = None
            for w in wins:
                try:
                    app = Application(backend="uia").connect(handle=w._hWnd, timeout=3)
                    for tab in app.window(handle=w._hWnd).descendants(control_type="TabItem"):
                        if "直聘" in (tab.window_text() or ""):
                            found = tab.window_text()
                            break
                except Exception:
                    continue
                if found:
                    break
            if found:
                info["zhipin_window"] = found
                info["chrome_ready"] = True
            elif wins:
                info["error"] = "Chrome 已打开，但未能确认 BOSS 直聘页（窗口可能已最小化）"
            else:
                info["ok"] = False
                info["error"] = "未找到打开 zhipin.com 的 Chrome 窗口，请先打开并登录 BOSS 直聘"
    except Exception as e:
        info["ok"] = False
        info["error"] = f"{type(e).__name__}: {e}"
    return info


def main():
    # stdout 是 JSON 契约：被管道接管时按系统区域设置选码（中文机 = cp936），
    # 窗口标题里的非 GBK 字符会直接崩掉 print（同 fetch_jd.emit 的钉法）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if len(sys.argv) == 2 and sys.argv[1] in ("--check", "--check-json"):
        print(json.dumps(check_ready(), ensure_ascii=False))
        return
    if len(sys.argv) < 4:
        print(json.dumps({"ok": False, "error": "参数不足：fetch_jd_native.py <job_id> <url> <title> <company>"},
                         ensure_ascii=False))
        return
    job_id, url = sys.argv[1], sys.argv[2]
    title = sys.argv[3] if len(sys.argv) > 3 else ""
    company = sys.argv[4] if len(sys.argv) > 4 else ""
    print(json.dumps(fetch_jd_native(job_id, url, title, company), ensure_ascii=False))


if __name__ == "__main__":
    main()
