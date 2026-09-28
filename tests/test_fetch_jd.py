"""clean_jd 的回归测试（对应 2026-09-25/26 的清洗规则迭代与 Decisions/JobForge-workbenchJD清洗只留正文）。"""
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
