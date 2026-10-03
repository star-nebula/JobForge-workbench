"""前端投递状态契约：状态词汇表与 db.VALID_STATUSES 同集合，且每个状态都可达。

由来（2026-10-02 体检 A1）：前端曾声明 6 个状态、看板只渲染 4 列，`rejected`/`offered`
在 UI 上没有任何入口可达——库里 114 岗因此只有 discovered/interviewing 两值，投递闭环断头。
「词汇表」与「可达集合」分叉是这类缺陷的形态，本文件就是把可达性钉成断言。

2026-10-03 流转定稿：discovered 是岗位市场的归属状态，不进流水线看板；
粗筛通过（keep=1）由后端自动升入评估列；终态各自一列——曾合并成「已结束」，
拖入即写 rejected，拿到 Offer 反而被记成不合适。
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


def test_kanban_columns_cover_pipeline(src):
    """看板列 = 除 discovered 外每个状态一列：已发现归岗位市场，终态不再合并。

    列键集合与词汇表派生比对（另写一份必然漂移，A1 就是这么来的）；
    列序为前端显式声明（2026-10-03 用户裁定：不合适放最后一列收尾），
    列序漂了或词汇表增删状态没跟上，这里都会红。
    """
    order_block = src[src.index("const KANBAN_COL_ORDER="):]
    order_block = order_block[:order_block.index(";")]
    assert "discovered" not in order_block          # 已发现不渲染成看板列
    order = re.findall(r"'([a-z_]+)'", order_block)
    assert src.count("const KANBAN_COLS=") == 1
    cols_stmt = src[src.index("const KANBAN_COLS="):]
    cols_stmt = cols_stmt[:cols_stmt.index(";")]
    assert "TERMINAL_KEYS" not in cols_stmt         # 终态各自一列，没有「已结束」合并列
    keys = _status_meta_keys(src)
    assert keys[0] == "discovered"                  # 词汇表里被看板跳过的必须正是它
    assert set(order) == set(keys) - {"discovered"}  # 一状态一列，不重不漏
    assert order == ["reviewing", "applied", "interviewing", "offered", "rejected"]


def test_terminal_keys_are_rejected_and_offered(src):
    """TERMINAL_KEYS 供终态角标/市场改判按钮使用，词汇表里终态就这两个。"""
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
