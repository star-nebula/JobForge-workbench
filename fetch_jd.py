"""抓 BOSS 岗位详情页 JD 正文。双通道调度（2026-09-25 用户决策：隐蔽优先）：

  1. 原生键鼠通道（fetch_jd_native，默认首选）：OS 层注入键鼠操控用户日常
     Chrome，浏览器进程内零 CDP 痕迹；含跨进程节流（18~35s 随机间隔）
  2. CDP 通道（本文件，兜底）：连接 127.0.0.1:9222 复用 zhipin tab 页内 fetch

FETCH_JD_MODE 环境变量：auto（默认，native→cdp）/ native / cdp。
CDP 通道实测路径（保留）：页内 fetch 搜索 wapi 拿 securityId → 站内跳转详情页；
注意 BOSS 反爬较激进，同一 profile 短时间大量开 tab 会被临时风控——频率是主因。

用法：python fetch_jd.py <platform> <job_id> <job_url> <title> <company>
输出：stdout 一行 JSON {ok, jd?, error?, platform, job_id}
"""
import json
import os
import re
import sys
import time
import unicodedata

from playwright.sync_api import sync_playwright

CDP_URL = "http://localhost:9222"
HOME = "https://www.zhipin.com/"

# 详情页 URL 直跳会被反爬弹成 about:blank；securityId 是站内跳转的通行证
SEARCH_API = ("/wapi/zpgeek/search/joblist.json?query={query}"
              "&city=100010000&page=1&pageSize=30")

# JD 正文锚点：职责/要求小节标题行（全半角冒号均可；容忍「你的/我们的」前缀
# ——BOSS 招聘者常自写「你的工作内容：」，实测漏锚点会让标签区漏丢）
_ANCHOR_RE = re.compile(
    r"^(?:你的|我们的)?(岗位职责|工作职责|职位描述|职位详情|岗位描述|工作内容"
    r"|任职要求|任职资格|岗位要求|职位要求|工作要求|任职条件)")
# 冗余板块哨兵。板块标题（公司介绍/工商信息/竞争力分析/查看完整个人竞争力）
# 允许「行首词 + 空白或内容」匹配——UIA 文本里标题常与板块内容同行，整行精确
# 匹配会漏截（2026-09-26：JD 只留正文，标签/公司介绍/工商信息一律不留）
_STOP_SECTION_RE = re.compile(
    r"^(竞争力分析|查看完整个人竞争力|公司介绍|工商信息)(\s|$)")
_STOP_EXACT_RE = re.compile(
    r"^(招聘者|HR|招聘经理)$")
_STOP_OTHER_RE = re.compile(
    r"安全提示|^(.{0,6}(先生|女士))$"
    r"|^(刚刚活跃|今日活跃|本周活跃|本月活跃|长期在线)$")
# 有序编号（1、/ 2. / 3，）或无序符号（•·●等）开头 → 是正文条目，不是标签
# （行首允许引号/空白：招聘者粘贴时可能整段带引号）
_TAG_NUM_RE = re.compile(r'^["“”\']*(?:\d+[、.．,，:：)）]|[•·●■◆▪‣◦–—-])')
_TAG_LINE_MAX = 40  # 标签区整行长度上限（多标签拼接行）

# Kangxi 部首（U+2F00–U+2FD5）→ 正字映射；不用整体 NFKC，避免误伤全角标点
# str.translate 按 ord 序号查表，键必须是 int
_KANGXI_MAP = {ord(c): unicodedata.normalize("NFKC", c)
               for c in map(chr, range(0x2F00, 0x2FD6))}


def clean_jd(text) -> str:
    """清洗详情页全文 → 只留「岗位职责/任职要求」正文。

    UIA 可访问性树拿到的是主文档全文，尾部带招聘者卡/竞争力分析/BOSS 安全
    提示/公司介绍/工商信息等冗余块；本函数按正文锚点定位起点、哨兵行截断
    终点。锚点之前的短行区是技能标签——列表抓取时 tags 字段已有（岗位卡
    chips 展示），JD 内不再保留（2026-09-26 用户决策）。Kangxi 部首映射还原
    BOSS 字体混淆的形近字（⽤→用、⼯→工），否则中文关键词匹配会被污染。
    幂等：已清洗文本再过一遍不变。锚点找不到时仍做哨兵截断与历史「标签：」
    前缀剥离（板块边界强，不误杀正文）；无任何哨兵则原样返回（保守）。"""
    if not text:
        return text
    text = text.translate(_KANGXI_MAP)
    lines = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln or (lines and lines[-1]):
            lines.append(ln)
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()

    def _hit_stop(ln: str) -> bool:
        return bool(ln and (_STOP_SECTION_RE.match(ln)
                            or _STOP_EXACT_RE.match(ln)
                            or _STOP_OTHER_RE.search(ln)))

    start = None
    for i, ln in enumerate(lines):
        if ln and _ANCHOR_RE.match(ln):
            start = i
            break
    if start is None:
        # 二次定位：招聘者自写小节标题（不在锚点表内）的强特征——短行、行尾
        # 冒号、下一行是编号条目（如「工作职责描述：」+「1. ……」）；命中则
        # 该行即内容起点，其前的短行标签区走同一个丢弃逻辑
        for i in range(len(lines) - 1):
            ln = lines[i]
            if (ln and len(ln) <= 20 and ln.endswith(("：", ":"))
                    and _TAG_NUM_RE.match(lines[i + 1] or "")):
                start = i
                break
    if start is None:
        # 无锚点（JD 自由文本或缺小节标题）：哨兵是强边界，截掉其后板块不误杀；
        # 连哨兵都没有才原样返回。历史坏缓存的行首「标签：」前缀顺手剥离
        stop = len(lines)
        for i, ln in enumerate(lines):
            if _hit_stop(ln):
                stop = i
                break
        if stop == len(lines):
            return "\n".join(lines)
        body = [re.sub(r'^(?:标签[:：]\s*)?["“”\']*', "", ln, count=1)
                for ln in lines[:stop] if ln]
        return "\n".join(body).strip()
    # 锚点前区域：仅当整体像标签区（无编号/无序符号、行不太长）才认定为技能
    # 标签区——丢弃（列表抓取时 tags 已有）；否则视为无标题正文（如 JD 缺
    # 「岗位职责」标题、职责条目带编号直接开始），降级并入输出开头——内容不丢
    pre = [ln for ln in lines[:start] if ln]
    is_tag_block = bool(pre) and all(
        not _TAG_NUM_RE.match(ln) and len(ln) <= _TAG_LINE_MAX for ln in pre)
    if is_tag_block:
        body = []
    else:
        # 历史坏缓存兼容：旧版误生成的「标签：」前缀与行首引号在降级时剥离
        # （两段独立可选：缓存可能已被上一轮自愈剥掉前缀、只剩引号）
        body = [re.sub(r'^(?:标签[:：]\s*)?["“”\']*', "", ln, count=1) for ln in pre]
    for ln in lines[start:]:
        if _hit_stop(ln):
            break
        body.append(ln)
    while body and not body[-1]:
        body.pop()
    return "\n".join(body).strip()


def _settle(page, timeout_s=15):
    """等页面跳转稳定（首页会按 IP 跳区域站，如 /foshan/）。"""
    last = None
    for _ in range(timeout_s):
        time.sleep(1)
        if page.url == last:
            break
        last = page.url
    time.sleep(2)


def _find_job(page, job_id, title, company):
    """页面上下文内调搜索接口，按 job_id 前缀（或标题+公司）匹配目标岗位，返回 securityId。"""
    query = (title or "").strip()[:20] or "招聘"
    expr = """async (q) => {
        const r = await fetch('/wapi/zpgeek/search/joblist.json?query=' + encodeURIComponent(q)
            + '&city=100010000&page=1&pageSize=30', {credentials: 'include'});
        return await r.json();
    }"""
    d = None
    for _ in range(3):
        try:
            d = page.evaluate(expr, query)
            break
        except Exception:
            time.sleep(3)
    if not d or d.get("code") != 0:
        return None, "搜索接口被风控（code=%s），稍后再试或手动打开链接" % (d or {}).get("code")
    items = (d.get("zpData") or {}).get("jobList") or []
    prefix = job_id[:16]
    for it in items:
        eid = str(it.get("encryptJobId") or "")
        if eid.startswith(prefix) or eid.split("_")[0].startswith(prefix):
            return it, None
    # 前缀没命中：标题+公司模糊匹配兜底
    for it in items:
        if (title and it.get("jobName") == title
                and (not company or it.get("brandName") == company)):
            return it, None
    return None, "搜索结果里没找到该岗位（可能已下线或换页）"


def _extract_jd(page):
    return page.evaluate("""() => {
        for (const h of document.querySelectorAll('h3')) {
            const t = (h.textContent || '').trim();
            if (['职位描述', '职位详情', '岗位职责'].includes(t)) {
                const box = h.parentElement ? h.parentElement.querySelector('.job-sec-text') : null;
                if (box && box.textContent.trim()) return box.textContent.trim();
            }
        }
        let best = '';
        document.querySelectorAll('.job-sec-text').forEach(s => {
            const t = s.textContent.trim();
            if (t.length > best.length) best = t;
        });
        return best;
    }""")


def _pick_page(ctx, want_url="zhipin.com"):
    """优先复用已加载完成的 zhipin tab（安全层已算好 stoken，页内 fetch 可用）。
    遍历现有 pages，跳过 about:blank / 非 zhipin 页；找不到返回 None。
    注意：主动 new_page() 开的新 tab 会被 Boss 安全层软锁（XHR 全 pending）。"""
    for pg in ctx.pages:
        try:
            u = pg.url or ""
        except Exception:
            continue
        if want_url in u and not u.startswith("about:"):
            return pg
    return None


def _fetch_jd_cdp(job_id: str, title: str, company: str) -> dict:
    result = {"ok": False, "jd": None, "error": None}
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(CDP_URL)
            ctx = browser.contexts[0]
            # 1) 优先复用用户已打开的 zhipin tab（无调试环境下加载完成，stoken 已就绪）
            page = _pick_page(ctx)
            owned = False
            if page is None:
                # 2) 没有现成 tab：开新 tab 走老流程（首页 settle 后再发请求，成功率较低）
                page = ctx.new_page()
                owned = True
                page.goto(HOME, wait_until="load", timeout=25000)
                _settle(page)
            try:
                it, err = _find_job(page, job_id, title, company)
                if not it:
                    result["error"] = err or "未找到岗位"
                    return result
                jid = it["encryptJobId"]
                url = f"https://www.zhipin.com/job_detail/{jid}.html?securityId={it['securityId']}&lid={jid}"
                page.evaluate("u => location.href = u", url)
                deadline = time.time() + 15
                while time.time() < deadline:
                    time.sleep(1)
                    if "/job_detail/" in (page.url or ""):
                        break
                if "/job_detail/" not in (page.url or ""):
                    result["error"] = "详情页被反爬拦截（跳转未生效），稍后再试或手动打开链接"
                    return result
                time.sleep(2.5)   # 等正文渲染
                jd = _extract_jd(page)
                if jd and len(jd.strip()) > 20:
                    result["ok"] = True
                    result["jd"] = jd.strip()[:20000]
                else:
                    result["error"] = "详情页已打开但未解析到职位描述（可能岗位已下线或页面改版）"
            finally:
                if owned:
                    try:
                        page.close()
                    except Exception:
                        pass
            browser.close()
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        result["error"] = ("Chrome 9222 不可达：" + msg) if "ECONNREFUSED" in msg else msg
    return result


def fetch_jd(job_id: str, job_url: str, title: str, company: str) -> dict:
    """调度：native 优先（auto/native），CDP 兜底（auto/cdp）。native 失败原因并入最终 error。"""
    mode = (os.environ.get("FETCH_JD_MODE") or "auto").strip().lower()
    native_err = None
    if mode in ("auto", "native"):
        try:
            from fetch_jd_native import fetch_jd_native
            r = fetch_jd_native(job_id, job_url, title, company)
            if r.get("ok"):
                r["jd"] = clean_jd(r.get("jd"))
                return r
            native_err = r.get("error") or "未知原因"
        except Exception as e:
            native_err = f"原生通道不可用：{type(e).__name__}: {e}"
        if mode == "native":
            return {"ok": False, "jd": None, "error": native_err}
    r = _fetch_jd_cdp(job_id, title, company)
    if r.get("ok"):
        r["jd"] = clean_jd(r.get("jd"))
    if native_err and not r.get("ok"):
        r["error"] = f"原生通道失败（{native_err}）；CDP 兜底也失败：{r.get('error')}"
    return r


def main():
    if len(sys.argv) < 4:
        print(json.dumps({"ok": False, "error": "参数不足：fetch_jd.py <platform> <job_id> <url> <title> <company>"}, ensure_ascii=False))
        return
    platform, job_id, url = sys.argv[1], sys.argv[2], sys.argv[3]
    title = sys.argv[4] if len(sys.argv) > 4 else ""
    company = sys.argv[5] if len(sys.argv) > 5 else ""
    r = fetch_jd(job_id, url, title, company)
    r.update({"platform": platform, "job_id": job_id})
    print(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()
