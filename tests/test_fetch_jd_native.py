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
