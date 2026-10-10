"""前端投递状态契约：状态词汇表与 db.VALID_STATUSES 同集合，且每个状态都可达。

由来（2026-10-02 体检 A1）：前端曾声明 6 个状态、看板只渲染 4 列，`rejected`/`offered`
在 UI 上没有任何入口可达——库里 114 岗因此只有 discovered/interviewing 两值，投递闭环断头。
「词汇表」与「可达集合」分叉是这类缺陷的形态，本文件就是把可达性钉成断言。

2026-10-03 流转定稿：discovered 是岗位市场的归属状态，不进流水线看板；
粗筛通过（keep=1）由后端自动升入评估列；终态各自一列——曾合并成「已结束」，
拖入即写 rejected，拿到 Offer 反而被记成不合适。

2026-10-09 改判（P4/P5）：进评估列改由 AI 分析分驱动，L1 粗筛接口族整体下线
（`triage_keep` 列与历史数据按用户裁定保留），本文件里带日期的段落是当时口径，不是现状。
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


def test_market_sort_and_status_filter(src):
    """阶段2 A2：市场默认排序=综合分（10-03 用户裁定）；状态下拉选项从 STATUS_META 派生。
    状态下拉若另写一份词汇表，这里会红。"""
    assert re.search(r'<select id="sortSel".*?<option value="smart"', src, re.S)
    assert "_sortKey='smart'" in src
    assert "statusSel.addEventListener" in src and "STATUS_META.forEach" in src
    assert "_clearAllFilters" in src


def test_triage_surface_is_retired(src):
    """P5（2026-10-09 裁定：删后端 L1 这一族、保留 `triage_keep` 列与历史数据）：
    前端不再有粗筛残留——筛选状态、胶囊配色、卡片变暗、死函数都不该留着，
    尤其 .kcard 不许再按历史 triage_keep=0 无解释变暗。措辞统一为「AI 分析」。"""
    for dead in ("_triFilter", "_triCounts", "_triBadge", "data-tri-filter", "jf_tri",
                 "tri-dim", "tri-keep-pill", "tri-drop-pill", "tri-none-pill", "api/triage"):
        assert dead not in src, f"粗筛残留未删净：{dead}"
    assert "粗筛" not in src and "精配" not in src


def test_market_has_review_entry(src):
    """P4（2026-10-09 裁定「自动升 + 手动一键」）：discovered 岗在岗位市场有可点的「→ 评估」。

    自动升档只捞达标的新岗，库里 108 个 discovered 存量与差几分想看看的岗全靠这条手动路；
    升档门槛数字必须来自 /api/jobs，前端写死就等于和后端常量各说各话。
    """
    btn = re.search(r'<button[^>]*data-act="review"[^>]*>\s*→ 评估\s*</button>', src)
    assert btn, "岗位市场卡片缺「→ 评估」一键入口"
    assert "j.status==='discovered'?" in src[btn.start() - 200:btn.start()], "入口没绑定 discovered"
    assert 'closest(\'[data-act="reject"],[data-act="review"]' in src, "点击分支没接上一键评估"
    assert "b.dataset.act==='review'?'reviewing'" in src          # 点的确实是升到 reviewing
    assert "_reviewFloor=d.review_floor" in src                   # 门槛从后端取
    assert "${_reviewFloor}" in src and "≥70" not in src          # 提示语里不写死分数线


def test_cd_polish_batch(src):
    """C/D 打包批（2026-10-03）：🔔 真化接线、装饰按钮已删、筛选持久化、
    下架角标、消息关联岗位、原生 prompt 已替换为输入模态。"""
    assert 'id="notifBadge"' in src and "_refreshNotifBadge" in src   # 🔔 有角标与刷新逻辑
    assert "<b>B</b>" not in src and "岗位适配" not in src            # 装饰按钮已删
    assert "_saveMktFilters" in src and "jf_sort" in src               # 筛选/排序持久化
    assert "_staleBadge" in src                                        # 下架感知角标
    assert '_matchJobForMsg' in src                                    # 消息↔岗位关联
    assert "_askText" in src and "prompt('" not in src                 # 原生 prompt 清零


def test_batch_result_is_visible(src):
    """P6（2026-10-09 用户裁定「直接做」）：一次任务跑完的结果必须看得见。

    旧形态：chip 只留「最后一条错误 + .slice(0,60)」，熔断那句「冷却 1~2 小时后重抓
    剩余 N 个」正好被切掉；失败岗只能逐个开详情弹窗重试；结束态 chip 点一下直接消失。
    """
    assert ".slice(0,60)" not in src and "悬停进度条看原因" not in src
    assert 'id="batchResultModal"' in src
    order = re.search(r"const ORDER=\[(.*?)\];", src).group(1)
    assert "'batchResultModal'" in order, "结果面板没接进 Esc 关闭秩序"
    assert "failed_jobs" in src, "面板只吃截断文本、不吃结构化失败清单"
    assert "重试这" in src and re.search(r"retry:", src), "重试入口没把失败岗点名发给后端"
    assert re.search(r"fetch\('/api/analyze-batch',\{method:'POST'", src)
    assert "_openBatchResult(s)" in src, "结束态 chip 仍在「点了就消失」"
    assert '_openBatchResult(_lastBatch)' in src, "面板没在失败时自动开一次"
    assert "_batchResultProbe" in src, "刷新后结果与进度都看不见了"
    assert re.search(r"const hasResult=[^;]*errors[^;]*failed_jobs", src), \
        "只按 done 判「有没有结果」：抓列表就没新岗时 done=0，chip 直接消失、提示没人能看见"


def test_market_has_one_click_fetch_and_analyze(src):
    """岗位市场头部必须有「一键抓取并分析」（2026-10-09 用户裁定：别把抓 JD 砍掉）。

    智能抓取可能只抓到列表、JD 没抓到；这批岗若只剩「重跑整条流水线」一条路，
    补 50 个岗位就要先白等一遍抓列表（约 1 分钟/城）。入口必须显式说清代价
    （花 BOSS 额度、接管键鼠），并且走常规队列而不是失败清单点名重试。
    """
    btn_at = src.index('id="fetchAnalyzeBtn"')
    assert src.index('<div class="view" id="jobs"') < btn_at < src.index('<div class="view" id="pipeline"'), \
        "「一键抓取并分析」不在岗位市场头部"
    handler_at = src.index("getElementById('fetchAnalyzeBtn')")
    seg = src[handler_at:handler_at + 2600]
    assert "一键抓取并分析" in src and "BOSS" in seg and "额度" in seg, "确认框没如实播报 JD 抓取代价"
    assert re.search(r"\{only:", seg) and "retry:" not in seg, \
        "入口没走常规队列（该带 only 收窄候选集，而不是 retry 点名）"
    assert "_pollBatch()" in seg, "启动后没接上进度轮询"
    # 标题必须反映这批碰没碰 BOSS：写死「批量分析」正是「点分析却在重抓全库」看不见的来源
    assert re.search(r"function _batchTitle\(s\)\{.*?analyze_only.*?抓取并分析", src, re.S), \
        "批次标题没按 mode 区分「抓取并分析 / AI 分析」"
    assert "_batchTitle(s)" in src and "'批量分析'}结束" not in src


def test_batch_result_panel_ages_out_to_read_only(src):
    """结果面板的失败清单是**进程内快照**：刷新页面还能看见，但它可能已经隔了几小时。

    旧形态过期照旧「一键补抓」，等于把一份很旧的岗位清单重新排队——期间这些岗可能已经
    手工补抓过、已分析过、甚至已下架，而用户看到的只是「补抓 N 个」。
    2026-10-09 用户裁定：过期只读不许补抓，改走库里实况的入口（岗位市场那颗按钮本来就是
    按「缺 JD / 未分析」现算的，比旧清单更准且不重复花额度）。
    """
    assert re.search(r"const BATCH_RETRY_TTL_MIN=\d+;", src), "过期阈值写死在判断里，改一处就漂移"
    stale = re.search(r"function _batchStale\(s\)\{.*?finished_at.*?BATCH_RETRY_TTL_MIN", src, re.S)
    assert stale, "没有统一的「这批是否过期」判据"

    panel = src[src.index("function _openBatchResult"):]
    panel = panel[:panel.index("\nfunction ")]
    assert "const stale=_batchStale(s)" in panel
    assert re.search(r"retry\.style\.display=\(fl\.length&&!stale\)", panel), \
        "过期快照仍给「一键补抓」按钮"
    assert re.search(r"live\.style\.display=\(fl\.length&&stale\)", panel), \
        "过期时没给出库里实况的替代入口"
    assert "已过期" in panel and "只读" in panel, "面板没告诉用户这份结果为什么点不动"

    # 替代入口必须落到那颗按库里实况算队列的按钮，而不是另起一套逻辑
    live = src[src.index("document.getElementById('batchLiveBtn').addEventListener"):]
    live = live[:live.index("});")]
    assert '.nav-item[data-view="jobs"]' in live and "fetchAnalyzeBtn" in live, \
        "「按库里实况」没接回岗位市场的一键抓取并分析"

    # 时间戳由后端 _batch 自己带（status 原样 {**_batch} 吐出来），前端不另猜：
    # 猜错的代价是把几小时前的清单当刚跑完的。重试按钮本身也要再过一遍新鲜度
    assert "_batchStale(_lastBatch)" in src, "重试处理器没独立判一次新旧"
    assert "_batchAgeLabel(" in src[src.index("function _renderBatchChip"):
                                   src.index("function _openBatchResult")], \
        "结束态 chip 没说这份结果是几小时前的"


def test_market_fetch_analyze_follows_list_filters(src):
    """「一键抓取并分析」的候选集＝屏幕上看得见的那批（2026-10-10 用户裁定）。

    旧形态两头都对不上：确认框按 `jobCache` 全量算 N，后端按全库现算，用户点了
    「未获取 JD」却把已获取的岗一起排队——多出来的每一个都是真金白银的 BOSS 额度。
    """
    vis_at = src.index("function _visibleJobs")
    vis = src[vis_at:vis_at + 600]
    assert "_filterJobs" in vis and "_matchJdFilter" in vis and "_statusFilter" in vis, \
        "可见集合没把搜索、JD、状态三个筛选都算进来"
    assert src.count("shown.filter(_matchJdFilter)") == 1, \
        "_renderJobList 自己又拼了一遍筛选，两份谓词必然漂移"
    rl = src[src.index("function _renderJobList"):src.index("function _renderJobList") + 1400]
    assert "_visibleJobs(jobs,q)" in rl, "列表渲染没走同一个可见集合口径"

    h = src.index("getElementById('fetchAnalyzeBtn')")
    seg = src[h:h + 2600]
    assert "_visibleJobs(jobCache,_searchQ())" in seg, "确认框还按全量 jobCache 算，数字和屏幕对不上"
    assert re.search(r"\{only:todo\.map\(", seg) and "platform" in seg and "job_id" in seg, \
        "没把可见集合传给后端收窄候选集"
    assert "筛选" in seg, "确认框没说本次只跑筛出来的这批"


def test_batch_panel_button_wordbook(src):
    """三颗按钮一套词（2026-10-10 用户裁定）：主路和按库里实况都叫「一键抓取并分析」，
    照失败清单重跑叫「重试这 N 个失败岗」。

    「一键补抓」和「按库里实况抓取并分析」必须彻底退场——两个名字都含「抓取」、
    又挤在同一个弹窗底部，用户找不到按钮就是这么来的。
    """
    assert "一键补抓" not in src, "旧名和新名并存，同一件事两个叫法"
    assert "按库里实况" not in src, "弹窗底部出现了第二颗「抓取并分析」的别名"
    at = src.index('id="batchRetryBtn"')
    foot = src[at - 260:at + 260]
    assert "重试" in foot and 'id="batchLiveBtn"' in foot and "一键抓取并分析" in foot, \
        "弹窗底部两颗按钮没按词表命名"
    assert re.search(r"retry\.textContent=`重试这 \$\{fl\.length\} 个失败岗`", src), \
        "运行时长得和 HTML 里那颗不是同一个名字"
