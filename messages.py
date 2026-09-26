"""通过 CDP 连接到用户已启动的 Chrome（127.0.0.1:9222），在 BOSS 直聘聊天页
监听页面自己发出的会话列表 XHR 响应，归一化出最近会话（最后一条消息 + 未读数）。

设计（Tier ①，只读、不做接口逆向）：
  - 找浏览器里已打开的 zhipin.com 聊天页 tab；没有则新开一个（绝不劫持其他 tab）
  - 求职者端聊天页是 /web/geek/chat（/web/chat/index 是招聘者端，会 302 回职位页）
  - page.on("response") 拦截会话列表 JSON 响应（实测接口：getGeekFriendList / geekFilterByLabel）
  - 解析是防御式的：字段名按候选链匹配 + 深度搜索，BOSS 改字段名也能兜底
  - 只读不写：不点击会话、不发消息，避免产生「已读」等副作用

输出：写 messages.json（{ok, fetched_at, messages, source, error, debug_urls, debug_sample}）
server.py 的 POST /api/messages/refresh 用 subprocess 跑本脚本后读文件入库。

用法前置（与 grab_cookies.py 相同）：Chrome --remote-debugging-port=9222 且已登录 BOSS。
"""
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

CDP_URL = "http://localhost:9222"
OUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "messages.json")
CHAT_URL = "https://www.zhipin.com/web/geek/chat"
WAIT_SECONDS = 12               # 页面加载后监听窗口

# 会话列表所在响应的 URL 特征（实测：求职者端为 getGeekFriendList / geekFilterByLabel）
SESSION_URL_KEYS = ("getGeekFriendList", "geekFilterByLabel", "getFriendList", "filterByLabel", "friend/list")

# 会话项的候选字段链（按优先级，防御式解析；实测字段：encryptUid/name/title/brandName/lastMsg/lastTS/unreadMsgCount）
ID_KEYS = ("encryptUid", "encryptBossId", "encryptFriendId", "friendId", "geekFriendId", "encryptId", "uid", "bossId")
NAME_KEYS = ("friendName", "name", "nickName", "nickname", "userName")
JOB_KEYS = ("jobName", "friendJobName", "positionName", "title")
COMPANY_KEYS = ("companyName", "friendCompanyName", "brandName", "brandComName")
TIME_KEYS = ("lastTS", "msgTime", "lastMsgTime", "lastContactTime", "lastShowTime", "updateTime", "time", "lastTime")
UNREAD_KEYS = ("unreadMsgCount", "unreadCount", "unreadNum", "unread", "noReadCount", "noreadCount")
MSG_KEYS = ("lastMsgContent", "lastContent", "lastMsgText", "showText", "content", "text")
NESTED_MSG_KEYS = ("lastMsg", "lastMessage", "lastMessageInfo", "msg")


def _walk_dicts(obj):
    """深度优先遍历 JSON，产出所有 dict。"""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_dicts(v)


def _pick(d, keys):
    for k in keys:
        v = d.get(k)
        if v is None or v == "":
            continue
        return v
    return None


def _flatten_text(v, depth=0):
    """把任意形态的消息体（str / dict / 嵌套 list）拍平成纯文本。"""
    if depth > 4 or v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, dict):
        for k in ("text", "content", "msgContent", "contentText", "title"):
            if k in v:
                s = _flatten_text(v[k], depth + 1)
                if s:
                    return s
        return ""
    if isinstance(v, list):
        parts = [_flatten_text(x, depth + 1) for x in v]
        return " ".join(p for p in parts if p)
    return ""


def _parse_time_ms(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        if v > 1e12:
            return int(v)            # 毫秒时间戳
        if v > 1e9:
            return int(v * 1000)     # 秒时间戳
    return None


def _parse_session(d):
    """把一个疑似会话的 dict 归一化；字段不足返回 None。"""
    fid = _pick(d, ID_KEYS)
    name = _flatten_text(_pick(d, NAME_KEYS))
    content = ""
    nested_dicts = []
    for nk in NESTED_MSG_KEYS:
        nv = d.get(nk)
        if nv is None or nv == "":
            continue
        if isinstance(nv, dict):
            nested_dicts.append(nv)
        if not content:
            content = _flatten_text(nv)
    if not content:
        content = _flatten_text(_pick(d, MSG_KEYS))
    if not name or (not content and _pick(d, TIME_KEYS) is None):
        return None
    time_ms = _parse_time_ms(_pick(d, TIME_KEYS))
    if time_ms is None:
        # 时间也可能藏在嵌套的 lastMessageInfo / lastMsg 里
        for nv in nested_dicts:
            time_ms = _parse_time_ms(_pick(nv, TIME_KEYS))
            if time_ms is not None:
                break
    unread = 0
    for k in UNREAD_KEYS:
        v = d.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            unread = max(0, int(v))
            break
    return {
        "friend_id": str(fid) if fid else "",
        "sender": name,
        "job_name": _flatten_text(_pick(d, JOB_KEYS)),
        "company_name": _flatten_text(_pick(d, COMPANY_KEYS)),
        "content": content,
        "time_ms": time_ms,
        "unread": unread,
    }


def _richness(s):
    """会话项的信息丰富度，用于同 ID 去重时保留更完整的版本。"""
    return (sum(1 for v in (s["content"], s["time_ms"], s["job_name"], s["company_name"]) if v)
            + min(s["unread"], 1))


def _parse_captured(captured):
    """从捕获的响应里解析会话列表，去重后按时间倒序。
    优先解析会话列表接口的响应（URL 命中 SESSION_URL_KEYS），未命中再全量兜底；
    同一会话多次出现时保留信息更丰富的版本（如 getGeekFriendList 比 geekFilterByLabel 全）。"""
    sessions, seen = [], set()
    fallback_used = False
    sample = None

    def _collect(items):
        nonlocal sample, fallback_used
        for item in items:
            try:
                data = json.loads(item["body"])
            except Exception:
                continue
            for d in _walk_dicts(data):
                fid = _pick(d, ID_KEYS)
                name = _flatten_text(_pick(d, NAME_KEYS))
                if fid:
                    s = _parse_session(d)
                else:
                    # 兜底：无 friend 类 id 但有名字 + 消息/时间的 dict（结构变化时）
                    if not name or _pick(d, NESTED_MSG_KEYS) is None and _pick(d, MSG_KEYS) is None:
                        continue
                    s = _parse_session(d)
                    fallback_used = True
                if not s:
                    continue
                key = s["friend_id"] or f"{s['sender']}|{s['content'][:30]}"
                if key in seen:
                    for i, old in enumerate(sessions):
                        if (old["friend_id"] or f"{old['sender']}|{old['content'][:30]}") == key:
                            if _richness(s) > _richness(old):
                                sessions[i] = s
                            break
                    continue
                seen.add(key)
                sessions.append(s)
                if sample is None:
                    sample = json.dumps(d, ensure_ascii=False)[:500]

    hits = [c for c in captured if c.get("body") and any(k in c["url"] for k in SESSION_URL_KEYS)]
    _collect(hits)
    if not sessions:
        fallback_used = False
        _collect([c for c in captured if c.get("body") and "zhipin.com" in c["url"]])
    sessions.sort(key=lambda x: x["time_ms"] or 0, reverse=True)
    return sessions, fallback_used, sample


def _find_or_open_chat_page(ctx):
    """找已打开的求职者聊天 tab，没有则新开（不劫持其他 tab）。"""
    for page in ctx.pages:
        if "zhipin.com" in page.url and "/web/geek/chat" in page.url:
            return page, True
    page = ctx.new_page()
    return page, False


def fetch_messages():
    result = {
        "ok": False,
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "messages": [],
        "source": "error",
        "error": None,
        "debug_page_url": None,
        "debug_urls": [],
        "debug_sample": None,
    }
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(CDP_URL)
            ctx = browser.contexts[0]
            page, existed = _find_or_open_chat_page(ctx)
            captured = []

            def on_response(resp):
                try:
                    url = resp.url
                    if "zhipin.com" not in url:
                        return
                    ct = resp.headers.get("content-type") or ""
                    if "json" in ct:
                        captured.append({"url": url, "body": resp.text()})
                except Exception:
                    pass

            def on_websocket(ws):
                # 聊天实时数据走 WS，这里只收帧（只读），JSON 帧能解析就解析
                def _frame(payload):
                    try:
                        if isinstance(payload, bytes):
                            captured.append({"url": f"ws[bin:{len(payload)}B]", "body": ""})
                        else:
                            captured.append({"url": "ws[frame]", "body": payload[:200000]})
                    except Exception:
                        pass
                try:
                    ws.on("framereceived", _frame)
                except Exception:
                    pass

            page.on("response", on_response)
            page.on("websocket", on_websocket)
            # 已在聊天页则 reload 触发请求；新开的 tab goto 本身就会触发
            try:
                if existed:
                    page.reload(wait_until="domcontentloaded", timeout=20000)
                else:
                    page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=20000)
            except Exception:
                # SPA reload 常见 ERR_ABORTED（页面自行跳转）；若不在聊天页则换新 tab 再试
                try:
                    if "/web/geek/chat" not in (page.url or ""):
                        page = ctx.new_page()
                        page.on("response", on_response)
                        page.on("websocket", on_websocket)
                        page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=20000)
                except Exception:
                    pass
            page.wait_for_timeout(WAIT_SECONDS * 1000)

            result["debug_page_url"] = page.url[:200]
            hit_urls = [c["url"][:160] for c in captured
                        if c.get("body") and any(k in c["url"] for k in SESSION_URL_KEYS)]
            other_urls = [c["url"][:160] for c in captured
                          if c.get("body") and not any(k in c["url"] for k in SESSION_URL_KEYS)]
            result["debug_urls"] = (hit_urls + other_urls)[:20]
            sessions, fallback_used, sample = _parse_captured(captured)
            result["debug_sample"] = sample
            result["messages"] = sessions
            if sessions:
                result["ok"] = True
                result["source"] = "real"
            else:
                err = "未捕获到会话数据：可能未登录、Chrome 未开聊天页，或 BOSS 接口结构变化"
                if any('"code":24' in (c.get("body") or "") for c in captured):
                    err = "BOSS 返回 code=24（身份校验失败）：请确认浏览器登录的是求职者身份，并手动打开一次消息页"
                result["error"] = err + ("（已走兜底解析仍未命中）" if fallback_used else "")
            browser.close()
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    return result


def main():
    result = fetch_messages()
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"WROTE {OUT_FILE} ok={result['ok']} count={len(result['messages'])}")
    if result.get("error"):
        print(f"ERROR: {result['error']}")
    if result.get("debug_urls"):
        print(f"CAPTURED_URLS: {len(result['debug_urls'])}")


if __name__ == "__main__":
    main()
