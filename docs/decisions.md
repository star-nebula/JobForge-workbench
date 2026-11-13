# 项目决策记录

> 从 SemanticMemory 迁入（2026-10-31）。原 claim 已归档，`--include-history` 仍可查。

## JobForge-workbench 城市口径跟随 profile 与 L0 城市门槛

城市口径统一为三件事：期望城市只有一个来源、抓不到就报错不兜底、异地岗留着但默认看不见。 期望城市的唯一来源 = profile.city（server.profilecity()，未填返回空串）。抓取默认城市跟随它：CrawlReq.city 缺省由「全国」改为 None，解析顺序 req.city or profilecity() or "全国"（显式传参仍可覆盖，用于故意跨城搜）。 认不出的城市名一律报错中止，不静默按全国抓：spider.citycode() 归一城市名（「上海·闵行区」→「上海」、「杭州市」→「杭州」）后查 BOSS 编码表，查不到返回 None；crawlboss 见 None 直接返回 source='error'，错误文案列出可选城市并提示「已中止抓取，不会静默按全国搜」。空值与「全国/不限/全部」仍是合法的全国码——只有「无法识别」这一种输入不再被解释成全国。 城市作为 L0 硬门槛（spider.issamecity，纯本地二值、零成本、不进评分）：期望城市为空或全国 → 不限；岗位城市缺失 → True（不知道 ≠ 不匹配，与 calcmatchscore 的 50 中性分同口径）。门槛作用在三处——/api/jobs 默认只返回期望城市（附 cityok 与 ot

- 原 claim：`decision-2526a0129ba0`（已归档）
- 来源：`.backup/Memory-20261029/Decisions/JobForge-workbench城市口径跟随profile与L0门槛.md`

## JobForge-workbench 投递流水线流转口径（discovered 不进看板 · 粗筛通过自动升格 · 终态拆列）

10-02 A1 把 6 态词汇表钉进契约测试后，看板形态是「过程 4 列 + 终态合并列『已结束』」。用户实测截图提出两个结构性不满： 「已发现」列 112 张卡与「岗位市场」完全重复——已发现 = 已整理进市场的岗位，出现在投递流水线里只是噪音，淹没了真正需要人工动作的列。 「不合适」与「已 Offer」同列不合理：两者语义相反（失败 vs 成功），且不合适可能出自评估 / 投递 / 面试任一环节，合列把出局位置藏掉了。更实际的缺陷是「已结束」列的拖放目标硬编码 rejected（drop:'rejected'）——想把岗位标成已 Offer 而拖进该列，写库反而是「不合适」。 同时用户给出期望流转：粗筛（L1）筛掉的不进流水线；粗筛通过的自动进入评估列，由人工根据 AI 匹配度评估；人工投递 → 已投递，人工判不合适 → 不合适。 discovered 是岗位市场的归属状态，不渲染成看板列。词汇表 6 态保持不变（db.VALIDSTATUSES 零改动、零数据迁移），看板列改为 STATUSMETA.slice(1) 派生 = 除 discovered 外每个状态一列。（同日稍后按用户裁定改为显式列序 KANBANCOLORDER=['reviewing','applied','intervie

- 原 claim：`decision-3cf10537f032`（已归档）
- 来源：`.backup/Memory-20261029/Decisions/JobForge-workbench投递流水线流转口径.md`

## JobForge-workbench 整族下线本地规则评分（AI 分析分成为唯一匹配判断）

同一个匹配判断在系统里有两套并行实现：抓取时算好的本地 4 维规则分（spider.calcmatchscore → seenjobs.matchscore + scoredetail），与读完 JD 后的 AI 分（llm.analyzematch → llmanalysis）。用户体感是「要多看一个评分」，实测比这更糟： 一张市场卡叠了 4 个重叠信号（mini bars + matchscore 大字 + AI xx 胶囊 + 标签行），而看板卡只显示本地分、不显示 AI 分——同一个岗位在两个视图里读出来是两套结论。 本地分不携带任何 AI 看不到的信息：llm.py 的分析提示词输入是本地输入的严格超集（标题 / 薪资 / 城市 / 岗位标签 + JD[:2500]）。留着它只是多一个需要解释、会漂移、且会与 AI 打架的数字。 它还在隐形驱动额度花费：抓取管线按 matchscore 降序决定先给谁抓 JD——而 JD 抓取是全链路唯一昂贵资源（Decisions/JobForge-workbench岗位匹配三级漏斗 背景 3）。

- 原 claim：`decision-91c73bcb0101`（已归档）
- 来源：`.backup/Memory-20261029/Decisions/JobForge-workbench整族下线本地规则评分.md`

## JobForge-workbench 只用 BOSS 直聘

目前只用 BOSS 直聘。删除 spider.py 中 zhaopin/51job 全部代码，server.py /api/platforms 只暴露 BOSS。

- 原 claim：`decision-bd6cc413cf1f`（已归档）
- 来源：`.backup/Memory-20261029/Decisions/JobForge-workbench只用BOSS直聘.md`

## JobForge-workbench 一键智能抓取主线收敛（一个按钮 · 三段流水线 · 单循环体 · 双熔断）

三级漏斗（L0/L1/L2）逐层做齐之后，界面上留下了四个抓取入口：智能抓取、智能粗筛、JD 精配、抓取并精配（外加一个「精配上限」输入框）。用户实测后的不满不是功能缺失而是选择负担：「流程选择太多太乱」——每个按钮都要先判断自己此刻该按哪个、按了会花多少额度、下一步还剩什么。 同时 10-08 的真机实测暴露了抓取本身的费用结构：连续抓几十个 JD 之后 BOSS 会软性限流——不报错、不弹验证码，只静默挂起详情页。每岗代价是 150s × 3 层降级全超时，12 个岗连烧二十多分钟纯等待，零产出。这条只有「超时」这一个信号，所以必须靠超时计数来熔断。 主线只有一个按钮：「开始智能抓取」→ POST /api/pipeline，其余三个批量入口与「精配上限」输入框、粗筛筛选药丸全部移除。点击后跑三段，中间不问： 抓列表（原生通道，按简历关键词 + 期望城市） 抓 JD——范围口径见下面 P1 一次性 AI 分析（mode="analyzeonly"），全段落库

- 原 claim：`decision-fefac4bbe031`（已归档）
- 来源：`.backup/Memory-20261029/Decisions/JobForge-workbench一键智能抓取主线收敛.md`

## 目录重构必须扫描函数内懒导入并做真实子进程冒烟

目录或包结构调整后，要检查函数体内懒导入和平级裸导入，不能只验证模块级 import。对依赖真实子进程或外部环境的路径，设计可安全触达该分支的冒烟探针；多层兜底报错时先定位第一层根因。

- 原 claim：`lesson-lazy-import-refactor-v2`（已归档）
- 来源：`.backup/Memory-20261029/Lessons/目录重构漏改函数内懒导入与静态扫描防线.md`

## 整族下线要防止字段、数据口径和保护动作失效

整族下线的完成条件：

- 启动路径不再通过兼容迁移把已删字段加回来；迁移后重启并再次核对 schema。
- JSON-in-column 的读取与聚合共用同一解析实现，避免同一坏数据出现两套口径。
- 不可逆脚本的备份、端口护栏和回滚动作要在副本上真实演练，不能只走幂等提前退出分支。
- 负向契约闸门要只匹配真实残留，排除仍合法的同形词。
- 完成定义 = 代码不读不写 + 存储已删除 + 重启后不复活 + 闸门有效 + 展示面无残留。

- 原 claim：`lesson-retire-family-resurrection-v2`（已归档）
- 来源：`.backup/Memory-20261029/Lessons/整族下线的三个复活陷阱.md`
