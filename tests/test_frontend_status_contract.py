"""前端投递状态契约：状态词汇表与 db.VALID_STATUSES 同集合，且每个状态都可达。

由来（2026-10-02 体检 A1）：前端曾声明 6 个状态、看板只渲染 4 列，`rejected`/`offered`
在 UI 上没有任何入口可达——库里 114 岗因此只有 discovered/interviewing 两值，投递闭环断头。
「词汇表」与「可达集合」分叉是这类缺陷的形态，本文件就是把可达性钉成断言。
"""
import re
from pathlib import Path

import pytest

from jobforge import db

HTML = Path(__file__).resolve().parents[1] / "web" / "job-workbench.html"


@pytest.fixture(scope="module")
def src():
    return HTML.read_text(encoding="utf-8")


def _status_meta_keys(src):
    block = re.search(r"const STATUS_META=\[(.*?)\];", src, re.S).group(1)
    return re.findall(r"\{key:'([a-z_]+)'", block)


def _terminal_keys(src):
    block = re.search(r"const TERMINAL_KEYS=\[(.*?)\]", src).group(1)
    return set(re.findall(r"'([a-z_]+)'", block))


def test_status_vocabulary_matches_db(src):
    """前端词汇表 = 后端唯一口径，且只允许声明一次（多处声明必然漂移）。"""
    keys = _status_meta_keys(src)
    assert len(keys) == len(set(keys))
    assert set(keys) == db.VALID_STATUSES
    assert src.count("const STATUS_META=") == 1


def test_kanban_columns_cover_every_status(src):
    """看板列的状态并集必须等于全状态：漏一个就是「有状态、没出口」。"""
    block = src[src.index("const KANBAN_COLS="):]
    block = block[:block.index("];")]
    assert "STATUS_META.slice(0,4)" in block        # 过程 4 列从词汇表派生，不另写一份
    assert "statuses:TERMINAL_KEYS" in block        # 终态合并进「已结束」一列
    keys = _status_meta_keys(src)
    assert set(keys[:4]) | _terminal_keys(src) == set(keys)
    assert _terminal_keys(src) == {"rejected", "offered"}


def test_each_status_has_reachable_control(src):
    """弹窗状态选择器逐状态生成**且可点击写库**；看板拖放与岗位市场都落到同一 PATCH 通道。

    「可点击」是本条的全部意义：只读胶囊同样含 `data-status`，只看字符串会恒绿。
    """
    picker = re.search(r'class="jd-status".*?</div>', src, re.S).group(0)
    assert "STATUS_META.map" in picker and 'data-status="${s.key}"' in picker
    assert '<button class="jd-status-btn' in picker               # 必须是控件，不是标签
    assert "e.target.closest('.jd-status-btn')" in src             # 有点击分支
    assert re.search(r"const patch=\{status:st\}", src)            # 分支真的把状态发出去
    assert 'data-status="${col.drop}"' in src                     # 每列都是可放置的写库目标
    assert "api/jobs/${encodeURIComponent(platform)}" in src       # 改状态/备注共用一条 PATCH


def test_notes_field_is_sent(src):
    """后端一直收 notes，前端曾只发 {status}——备注必须真的发出去。"""
    assert re.search(r"notes:ni\.value", src)
