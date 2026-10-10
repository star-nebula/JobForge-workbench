"""原生列表抓取的导航失败分支（2026-10-01 上海复测：6 次里 4 次失败，全部 24 秒
= 15 秒标题超时；独立进程 7/7 成功 → 失败是地址栏注入没落到 Chrome，不是 BOSS 风控）。

固化两点：导航未生效时重试一次；错误文案按可观测事实分叉，不再统一甩锅「未登录或被风控」。
"""
import json

import pytest

from jobforge import fetch_gate, fetch_jd_native as F

DATA = json.dumps({"code": 0, "message": "Success",
                   "zpData": {"jobList": [{"encryptJobId": "x1", "jobName": "前端"}]}})


class Fake:
    """模拟浏览器：nav_seq 决定每次地址栏注入是否真的生效；page_seq 是每次生效后的页面文本。"""

    def __init__(self, nav_seq, page_seq):
        self.nav_seq = list(nav_seq)
        self.page_seq = list(page_seq)
        self.gotos = []
        self.title = "「佛山招聘」- BOSS直聘 - Google Chrome"
        self._landed = 0

    def goto(self, url):
        self.gotos.append(url)
        if self.nav_seq.pop(0):
            self._landed += 1
            self.title = "view-source:joblist.json - %d - Chrome" % self._landed
        # 注入落空：标题原样不动

    def window_title(self, w):
        return self.title

    def wait_title(self, w, old, timeout_s=18):
        return self.title

    def page_text(self, w):
        return self.page_seq[min(self._landed, len(self.page_seq) - 1)]


@pytest.fixture
def fake(monkeypatch):
    def _install(nav_seq, page_seq):
        br = Fake(nav_seq, page_seq)
        monkeypatch.setattr(F, "throttle_wait", lambda *a, **k: None)
        monkeypatch.setattr(F.fetch_gate, "checkpoint", lambda *a, **k: None)
        monkeypatch.setattr(F, "find_zhipin_tab", lambda: object())
        monkeypatch.setattr(F, "_goto", br.goto)
        monkeypatch.setattr(F, "_window_title", br.window_title)
        monkeypatch.setattr(F, "_wait_title", br.wait_title)
        monkeypatch.setattr(F, "_page_text_uia", br.page_text)
        monkeypatch.setattr(F, "_copy_page_text", br.page_text)

        class Clip:
            @staticmethod
            def paste():
                return ""

            @staticmethod
            def copy(_s):
                pass

        monkeypatch.setattr(F, "pyperclip", Clip)
        return br
    return _install


def test_nav_miss_then_retry_succeeds(fake):
    # 第一次注入落空（读回旧页面），重试生效
    br = fake([False, True], ["「佛山招聘」搜索结果页正文，没有 JSON", DATA])
    data = F.crawl_boss_native("高级前端工程师", "101020100", 1)
    src = [g for g in br.gotos if g.startswith("view-source:")]
    assert len(src) == 2 and src[0] == src[1]      # 重试的是同一个 API，不换目标
    assert data["code"] == 0
    assert br.gotos[-1] == "https://www.zhipin.com/web/geek/job"   # 读完把 tab 导航回求职页


def test_nav_still_missing_says_nav_failed_not_risk_control(fake):
    br = fake([False, False], ["旧页面正文"])
    with pytest.raises(F.NativeError) as e:
        F.crawl_boss_native("高级前端工程师", "101020100", 1)
    msg = str(e.value)
    assert "导航未生效" in msg
    assert "风控" not in msg and "未登录" not in msg
    assert len(br.gotos) == 2       # 确实只重试一次，不无限重放


def test_nav_ok_but_no_json_reports_page_content(fake):
    # 导航生效（标题已变）却读不到 JSON → 不再盲重试，文案说明是页面内容问题
    br = fake([True], ["<html>安全验证</html> 没有 JSON"])
    with pytest.raises(F.NativeError) as e:
        F.crawl_boss_native("高级前端工程师", "101020100", 1)
    msg = str(e.value)
    assert "没有 JSON 正文" in msg
    assert len(br.gotos) == 1


# ============================================================
# JD 正文抓取（2026-10-10「AI前端工程师」连补三轮全失败）
#
# 现场：库里 130 个期望城市岗位只有它缺 JD，其余 129 个都是「抓完列表的同一轮、
# 岗位还挂在搜索结果里」时抓到的。原生层报「已打开详情页但未解析到职位描述」——
# 这句话同时容纳两种完全不同的事：详情页正文 content-visibility 懒渲染没等到，
# 和被弹到一个标题含职位名的搜索/聚合页。旧代码在标题到位后只固定 sleep
# 1.2~2.2 秒取一次正文，然后就走剪贴板回退（BOSS 详情页主内容区 Ctrl+A 本来就选
# 不中），等于放弃。这批用例固化：值得等的页面要等（轮询），不值得等的页面别白等
# （落地页判据不过就不轮询），以及等不到时把「落在哪、页面上有什么」如实塞进报错。
# ============================================================
JD_FULL = ("职位详情\n" + "负责 React/Vue 前端开发，参与 AI 应用从 0 到 1 的落地。" * 5
           + "\n公司介绍\n深圳市易京科技有限公司")
# 坏样本形态（`_jd_body_plausible` 的 docstring 明写要挡掉的「488 字纯公司介绍」）：
# 「职位描述」标题在，标题后直接是页尾公司介绍段
JD_SHELL = "职位描述\n公司介绍\n深圳市易京科技有限公司成立于2015年，主营电商 SaaS。"
JD_LONG_TAIL = "职位描述\n公司介绍\n" + ("深圳市易京科技有限公司主营电商 SaaS 业务。" * 20)
NO_JD_TEXT = "BOSS直聘\n登录\n注册\n搜索职位\n推荐职位"     # 剪贴板通道拿到的壳子
DETAIL_TITLE = "「AI前端工程师」招聘-深圳市易京科技有限公司-BOSS直聘"
HOME_TITLE = "BOSS直聘-找工作0｜最新招聘 手机版-BOSS直聘"


@pytest.fixture
def fake_jd(monkeypatch):
    """装一个假浏览器给 fetch_jd_native()：uia_seq 是每次读页拿到的正文（读完后
    重复最后一条），land_title 是详情页导航落地后的窗口标题，click 决定路径 B
    返回什么标题或直接抛错。"""
    def _install(uia_seq, land_title=DETAIL_TITLE, click=DETAIL_TITLE):
        state = {"uia": list(uia_seq), "uia_calls": 0, "click_calls": 0, "title": land_title}

        def _uia(_w):
            state["uia_calls"] += 1
            i = state["uia_calls"] - 1
            return uia_seq[i] if i < len(uia_seq) else uia_seq[-1]

        def _click(_w, _t, _c):
            state["click_calls"] += 1
            if isinstance(click, Exception):
                raise click
            return click

        class _T:
            sleep_calls = 0

            @staticmethod
            def sleep(_s):
                _T.sleep_calls += 1

            @staticmethod
            def time():
                import time as _t
                return _t.time()

        monkeypatch.setattr(F, "throttle_wait", lambda *a, **k: None)
        monkeypatch.setattr(F.fetch_gate, "checkpoint", lambda *a, **k: None)
        monkeypatch.setattr(F, "find_zhipin_tab", lambda: object())
        monkeypatch.setattr(F, "_goto", lambda url: None)
        monkeypatch.setattr(F, "_window_title", lambda w: state["title"])
        monkeypatch.setattr(F, "_wait_title", lambda w, old, timeout_s=18: state["title"])
        monkeypatch.setattr(F, "_reuse_existing_detail_tab", lambda w, t, c: None)
        monkeypatch.setattr(F, "_detail_url_with_security", lambda w, jid, t: None)
        monkeypatch.setattr(F, "_click_search_result", _click)
        monkeypatch.setattr(F, "_close_active_tab", lambda w: None)
        monkeypatch.setattr(F, "_page_text_uia", _uia)
        monkeypatch.setattr(F, "_copy_page_text", lambda w: NO_JD_TEXT)
        monkeypatch.setattr(F, "time", _T)

        class Clip:
            @staticmethod
            def paste():
                return ""

            @staticmethod
            def copy(_s):
                pass

        monkeypatch.setattr(F, "pyperclip", Clip)
        return state
    return _install


def test_lazy_render_is_polled_before_giving_up(fake_jd):
    # 前两次读到的都是懒渲染空壳，第三次正文才填出来：应当在同一个页面上等到它
    st = fake_jd([JD_SHELL, JD_SHELL, JD_FULL])
    res = F.fetch_jd_native("d13eca496b1034fe0nN439W-EVRT",
                            "https://www.zhipin.com/job_detail/x.html", "AI前端工程师", "深圳市易京科技")
    assert res["ok"] is True, res["error"]
    assert "负责 React/Vue" in res["jd"]
    assert st["uia_calls"] == 3
    assert st["click_calls"] == 0, "第一页已经补救成功，不该再多走一次搜索页点击"


def test_first_shot_success_reads_the_page_once(fake_jd):
    # 回归护栏：本来就能抓到的岗，一次读页就返回——不多花一次输入事件、不多等一秒
    st = fake_jd([JD_FULL])
    res = F.fetch_jd_native("x1", "https://www.zhipin.com/job_detail/x1.html",
                            "AI前端工程师", "深圳市易京科技")
    assert res["ok"] is True and st["uia_calls"] == 1 and st["click_calls"] == 0


def test_wrong_landing_page_does_not_waste_the_poll(fake_jd):
    # 落地页标题既不含职位名也不含公司名 = 压根不是详情页，在它身上轮询毫无意义，
    # 应当一次读页就转路径 B（路径 B 落地正确，于是补救成功）
    st = fake_jd([JD_SHELL, JD_FULL], land_title=HOME_TITLE)
    res = F.fetch_jd_native("x1", "https://www.zhipin.com/job_detail/x1.html",
                            "AI前端工程师", "深圳市易京科技")
    assert res["ok"] is True, res["error"]
    assert st["click_calls"] == 1
    assert st["uia_calls"] == 2, f"错页面上不该轮询：{st['uia_calls']}"


def test_failure_reports_landed_page_and_body_sample(fake_jd):
    # 两条路都走到底仍抓不到：报错必须说清「落在哪、页面上有什么」，否则「未解析到
    # 职位描述」永远分不清懒渲染与被弹到搜索/聚合页。同时这里用的正文是「标题后只有
    # 公司介绍」的长坏样本——补救不许顺手把它当 JD 采纳（那是 _jd_body_plausible 挡的）。
    st = fake_jd([JD_LONG_TAIL])
    res = F.fetch_jd_native("x1", "https://www.zhipin.com/job_detail/x1.html",
                            "AI前端工程师", "深圳市易京科技")
    assert res["ok"] is False
    assert not res["jd"], "「标题后只有公司介绍」的坏样本绝不能被当成 JD 返回"
    assert "落地页标题" in res["error"] and "正文样本" in res["error"], res["error"]
    assert DETAIL_TITLE[:12] in res["error"], "报错里要看得见落到了哪个页面"
    assert st["uia_calls"] > 1, "失败前应当等过渲染"


# ============================================================
# 「AI前端工程师」真实现场（2026-10-10 从用户浏览器 UIA 树 dump，零额度）
#
# 打脸上一条假设：正文不是没渲染，是渲染得好好的（3046 字），但 BOSS 把这个岗位的
# JD 写成了「职位描述 → 技能标签 → 公司介绍（段落）→ 岗位职责 → 任职要求 → 加分项」，
# 「公司介绍」作为 JD 内部小节出现在标题后 39 字处。`_jd_body_plausible` 把它当成
# 懒渲染空壳拒掉、`extract_jd` 也只截到 33 字技能标签——两条路都在同一处边界上判死，
# 于是这一个岗永远失败，其余 169 个正常。页尾真正的边界是右栏的「竞争力分析」。
# ============================================================
REAL_PAGE_HEAD = (
    "首页\n职位\n公司\n校园\n搜索\n消息\n简历\n刘昊晴\n招聘中\n"
    "AI前端工程师\n15-25K·13薪\n深圳  3-5年  本科\n感兴趣立即沟通\n"
    "完善在线简历\n上传附件简历\n员工旅游带薪年假绩效奖金五险一金\n"
    "公司基本信息\n深圳市易京科技\n不需要融资\n20-99人\n计算机软件\n查看全部职位\n举报\n")
REAL_PAGE_JD = (
    "职位描述\nTypeScript\nVue\nReact\n计算机/软件工程相关专业\n"
    "公司介绍\n我们是一家深耕海外营销平台的 SaaS 公司，核心产品已稳定运营 3 年，日活过万。\n"
    "岗位职责\n- 负责公司 Web 端核心产品：机器人管理后台、数据看板、充值支付页、Agent 配置台\n"
    "- 把后端 API（Java+Python + MySQL，REST）数据变成好看好用的界面\n"
    "任职要求\n- 3 年以上前端经验，Vue3 或 React 至少精通一个，TypeScript 熟练\n"
    "加分项（没有也行）\n- 做过中后台管理系统/数据可视化（ECharts、表格大数据量渲染）\n")
REAL_PAGE_RAIL = (
    "李先生\n在线\n深圳市易京科技\n·\n人事\n竞争力分析\n查看完整个人竞争力\n"
    "BOSS 安全提示\nBOSS直聘严禁用人单位做出任何损害求职者合法权益的行为\n")
REAL_PAGE_FOOTER = (
    "公司介绍\n深圳市易京科技有限公司，作为全球信用保障科技领域的革新者，致力于构建智能信用保障生态平台。\n"
    "查看全部\n工商信息\n公司名称\n深圳市易京科技有限公司\n工作地址\n深圳福田区英龙商务中心1309\n"
    "更多职位\n看过该职位的人还看了\n前端开发工程师\n15-18K·13薪\n深圳沃达云Ai\n")
REAL_PAGE = REAL_PAGE_HEAD + REAL_PAGE_JD + REAL_PAGE_RAIL + REAL_PAGE_FOOTER


def test_company_intro_inside_the_jd_is_not_the_end_boundary():
    assert F._jd_body_plausible(REAL_PAGE) is True, "标题后紧跟的「公司介绍」是 JD 内部小节，不是懒渲染空壳"
    jd = F.extract_jd(REAL_PAGE)
    assert "负责公司 Web 端核心产品" in jd, jd[:200]
    assert "3 年以上前端经验" in jd
    assert "深耕海外营销平台" in jd, "JD 里那段公司介绍属于正文，要留下"
    assert "全球信用保障科技领域的革新者" not in jd, "页尾公司卡片不许混进 JD"
    assert "竞争力分析" not in jd and "BOSS 安全提示" not in jd


def test_pure_company_footer_is_still_rejected():
    # 回归护栏：`_jd_body_plausible` 的本职（挡住「标题后只有页尾公司介绍」的坏样本）
    # 不许因为上面那条放宽而失效——公司介绍后面没有真正的 JD 小节标题，就还是空壳。
    assert F._jd_body_plausible(JD_LONG_TAIL) is False
    assert len(F.extract_jd(JD_LONG_TAIL)) < 20


def test_real_failing_job_extracts_on_the_first_read(fake_jd):
    st = fake_jd([REAL_PAGE])
    res = F.fetch_jd_native("d13eca496b1034fe0nN439W-EVRT",
                            "https://www.zhipin.com/job_detail/d13eca496b1034fe0nN439W-EVRT.html",
                            "AI前端工程师", "深圳市易京科技")
    assert res["ok"] is True, res["error"]
    assert "负责公司 Web 端核心产品" in res["jd"]
    assert st["uia_calls"] == 1, "第一次读页就该成，不该走补救轮询"
    assert st["click_calls"] == 0
