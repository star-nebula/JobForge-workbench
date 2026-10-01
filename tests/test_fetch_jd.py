"""clean_jd 的回归测试（对应 2026-09-25/26 的清洗规则迭代与 Decisions/JobForge-workbenchJD清洗只留正文）。

外加 stdout JSON 契约的真子进程测试（2026-10-01：管道上按区域设置选码 = cp936，
JD 里的 U+FFFC 崩掉 print，抓取成功却 rc=1、stdout 空）。
"""
import json
import os
import subprocess
import sys

from jobforge import paths
from jobforge.fetch_jd import clean_jd


def test_anchor_found_tag_block_dropped():
    text = "Python\nReact\n5年经验\n岗位职责：\n1. 负责前端开发\n2. 优化性能\n公司介绍 XXX 集团"
    out = clean_jd(text)
    assert "Python" not in out          # 锚点前的标签区整块丢弃
    assert "岗位职责" in out
    assert "负责前端开发" in out
    assert "公司介绍" not in out        # 板块哨兵截断


def test_anchor_with_prefix():
    text = "React\n你的工作内容：\n1. 写页面\n工商信息 某某公司"
    out = clean_jd(text)
    assert "React" not in out           # 「你的」前缀锚点可定位，标签区照丢
    assert "写页面" in out
    assert "工商信息" not in out


def test_no_anchor_sentinel_truncates_and_strips_tag_prefix():
    text = "标签：React\n负责开发\n公司介绍 某某"
    out = clean_jd(text)
    # 无锚点：哨兵截断 + 历史「标签：」前缀剥离（技能词本身保留）
    assert out == "React\n负责开发"


def test_no_anchor_no_sentinel_passthrough():
    text = "自由文本 JD\n没有小节标题与哨兵"
    assert clean_jd(text) == text       # 保守：无任何边界不误杀


def test_unnumbered_title_body_not_killed():
    # 锚点前的内容带编号 → 不是标签区，降级并入输出开头
    text = "1. 负责开发\n2. 带团队\n任职要求：\n1. 五年经验"
    out = clean_jd(text)
    assert "负责开发" in out
    assert "五年经验" in out


def test_idempotent():
    text = "React\n岗位职责：\n1. 开发\n公司介绍 X"
    once = clean_jd(text)
    assert clean_jd(once) == once


def test_kangxi_radical_normalized():
    # BOSS 字体混淆的康熙部首 ⽤(U+2F64) 还原为「用」
    text = "岗位职责：\n1. 负责开发（使⽤ React）"
    out = clean_jd(text)
    assert "使用 React" in out
    assert "⽤" not in out


def test_clean_jd_strips_uia_placeholders():
    """UIA 图标占位符 U+FFFC / 替换符 U+FFFD / 零宽类：读不出信息，还会让
    GBK 端崩掉（2026-10-01 真机），清洗时一并剥掉，正文文字必须连着。"""
    text = "岗位职责：\n1. 前端\u200b开发\ufffc与\ufeff测试\ufffd收尾\n公司介绍 X"
    out = clean_jd(text)
    for junk in ("\u200b", "\ufffc", "\ufeff", "\ufffd"):
        assert junk not in out
    assert "前端开发与测试收尾" in out


def _emit_child(payload):
    """按 server 的真实拉起方式跑一个只调 emit 的子进程：stdout 是管道。
    刻意关掉 UTF-8 模式与 PYTHONIOENCODING，让孩子落回系统区域码（中文机 = cp936）
    ——那才是事故现场，不然测试在任何机器上都恒过。"""
    code = "from jobforge import fetch_jd; fetch_jd.emit(%r)" % (payload,)
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    env.update({"PYTHONUTF8": "0", "PYTHONPATH": paths.SRC_DIR})
    return subprocess.run([sys.executable, "-c", code], capture_output=True,
                          timeout=120, cwd=paths.PROJECT_ROOT, env=env)


def test_emit_writes_utf8_json_over_pipe():
    """emit 的契约：管道 + 非 GBK 字符也要 rc=0 且吐得出 UTF-8 JSON。
    修复前这里是 rc=1 + stdout 空（UnicodeEncodeError），上层只能报「抓取超时」。"""
    p = _emit_child({"ok": True, "jd": "岗位职责：\n1. 做\ufffc图标与中文测试"})
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")[-400:]
    data = json.loads(p.stdout.decode("utf-8"))
    assert data["jd"].count("\ufffc") == 1        # emit 只钉编码，不删内容（删是 clean_jd 的活）
