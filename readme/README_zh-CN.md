# JobForge Workbench

**Language / 语言 / 言語 / Idioma / 언어 / Langues / Sprachen:** [English](../README.md) | [简体中文](README_zh-CN.md) | [日本語](README_ja.md) | [한국어](README_ko.md) | [Français](README_fr.md) | [Deutsch](README_de.md) | [Español](README_es.md)

本地求职工作台：简历 → 关键词 → 岗位抓取（BOSS 直聘）→ 匹配度排序，一条龙单机工具。

![数据看板](../docs/screenshots/dashboard.png)

## 功能

- **个人资料**：简历上传解析、字段比对采纳、本地规则评分与优化建议、简历预览、导出 PDF
- **智能抓取**：从简历关键词抓取 BOSS 岗位（原生浏览器通道），抓取历史 + 岗位级防重复
- **岗位市场**：岗位落库、JD 详情抓取与正文清洗、匹配度排序、JD 获取统计（已获取/未获取/疑似残缺，胶囊可点击筛选；疑似残缺可在详情弹窗人工确认或重抓）。匹配度两套分工：**标签初筛**（本地规则 4 维：技能/意向/薪资/城市，技能分母=岗位技能标签、词归一全等匹配、薪资单位归一到 K、真实 0-100 无保底）抓取时算好，**JD 精配**（LLM 通读 JD + 标签/薪资/城市硬信息）在详情弹窗/批量分析产出，卡片带蓝色「AI xx」徽章
- **AI 能力**（多模型配置，OpenAI 兼容协议，密钥仅存本机 SQLite）：BOSS 打招呼语生成、岗位 AI 匹配分析（详情弹窗自动运行 + 岗位市场一键批量）、简历润色（diff 比对后采纳）
- **投递流水线**：看板拖拽管理 6 状态（discovered / reviewing / applied / interviewing / rejected / offered）
- **面试日程**：从「面试中」岗位安排面试时间与备注
- **消息中心**：只读同步 BOSS 直聘会话（CDP 响应拦截）
- **悬浮进度窗**：抓取期间置顶显示进度，带暂停/继续/结束按钮。窗口不抢焦点、鼠标点击穿透，因此**不会打断正在进行的键鼠抓取**；可拖 ⠿ 手柄移动位置

## 目录结构

```
JobForge-workbench/
├─ src/jobforge/                 # 代码：Python 包
│  ├─ server.py                  # FastAPI 入口
│  ├─ paths.py                   # 项目路径唯一来源（代码位置与数据位置解耦）
│  ├─ spider.py  fetch_jd.py  fetch_jd_native.py  fetch_gate.py
│  ├─ db.py  llm.py  profile_score.py
│  └─ tools/                     # 子进程脚本，由 server 以 `python -m jobforge.tools.*` 拉起
│     └─ hud.py  messages.py  grab_cookies.py
├─ web/job-workbench.html        # 前端单页（6 视图）
├─ data/                         # 运行时数据（不入库）：jobs.db、cookies.json、messages.json、闸门/节流文件、chrome-profile/
├─ tests/                        # pytest 回归（`venv\Scripts\python.exe -m pytest tests/`；例数不进文档，以现跑为准）
├─ run.bat  setup.bat  requirements.txt  README.md
```

数据文件路径一律从 `paths.py` 取，不再各自用 `__file__` 推算——搬代码不会带走数据。
CDP 调试 Chrome 的 user-data-dir（含 BOSS 登录态）也在数据目录下：`data/chrome-profile/`。

投递状态只有一张词汇表：前端 `STATUS_META`（看板列、详情弹窗状态选择器、仪表盘管道都从它派生），
后端 `db.VALID_STATUSES`。两者同集合、且每个状态都有可达的写库入口，由
`tests/test_frontend_status_contract.py` 钉住——曾经声明 6 态、看板只渲染 4 列，
`rejected`/`offered` 在 UI 上无从设置，库里 114 岗于是只剩两个值。

注意：`.bat` 必须保持 CRLF 行尾（`.gitattributes` 已声明 `*.bat text eol=crlf`）——cmd.exe 在
「`chcp` 切换代码页 + 中文注释 + 裸 LF」下会按字节偏移错位解析，静默吃掉 `set "PYTHONPATH=..."` 的
行首，表现为启动时 `ModuleNotFoundError: No module named 'jobforge'`。

手动启动（IDE / 命令行）需要 `src` 在 `PYTHONPATH` 里，否则 `import jobforge` 找不到：

```
set PYTHONPATH=%CD%\src
venv\Scripts\python.exe -m jobforge.server
```

## 架构

| 文件 | 作用 |
|---|---|
| `src/jobforge/server.py` | FastAPI 入口，`127.0.0.1:8080`（`--port` 可改，供调试起第二实例） |
| `src/jobforge/paths.py` | 项目路径唯一来源：`PROJECT_ROOT` / `DATA_DIR` / `WEB_DIR` |
| `src/jobforge/spider.py` / `fetch_jd_native.py` | 岗位列表与 JD 详情抓取（原生键鼠 + UIA TextPattern 通道） |
| `src/jobforge/fetch_jd.py` | JD 抓取总入口（native 优先、CDP 兜底）+ JD 正文清洗 |
| `src/jobforge/tools/hud.py` | 悬浮进度窗（独立进程；置顶 + 不抢焦点 + 点击穿透的三条窗口约束见文件头注释） |
| `src/jobforge/fetch_gate.py` | 跨进程暂停/停止闸门：信号落 `data/fetch_gate.json`，server 线程与抓取子进程共用 |
| `src/jobforge/tools/messages.py` | BOSS 消息同步（Playwright CDP 连 9222 浏览器拦截页面响应） |
| `src/jobforge/db.py` | SQLite（WAL）：岗位 / 消息 / 个人资料 / 抓取历史 |
| `src/jobforge/profile_score.py` | 个人资料本地规则评分引擎（13 项检查） |
| `src/jobforge/llm.py` | LLM 能力层（OpenAI 兼容 chat 客户端 + 打招呼语 / 匹配分析 / 简历润色三个功能函数） |
| `web/job-workbench.html` | 前端单页（6 视图） |
| `src/jobforge/tools/grab_cookies.py` | 抓取浏览器登录态 cookie 写 `data/cookies.json` |

## 使用

1. 双击 `setup.bat` 创建 venv 并安装依赖
2. 双击 `run.bat` 启动，浏览器打开 <http://127.0.0.1:8080>
3. 桌面 Chrome 打开并登录 zhipin.com 后即可抓取（抓取会接管键鼠约 8~15 秒；消息刷新需 9222 调试端口的浏览器）
4. 右上角 ⚙ 配置 AI 模型后可用打招呼语 / 匹配分析 / 简历润色（支持 DeepSeek / 通义 / 智谱 / Ollama 等任何 OpenAI 兼容服务）
5. 批量 AI 分析的前提：桌面 Chrome 已打开并登录 zhipin.com（窗口不要最小化）；分析会接管键鼠，连续 3 个失败会自动熔断停止。server 有单实例护栏——重复启动会被拒绝
6. 抓取进度看悬浮窗（顶栏「🪟 进度悬浮窗」手动打开，抓取开始时也会自动拉起）：
   - **暂停**只挡在安全点（岗位边界、节流等待），不会把一次键鼠动作撕成两半；暂停时长不计入节流，恢复后不用重等
   - **结束**会在秒级生效（同时 kill 在跑的抓取子进程），已抓到的岗位与已完成的 AI 分析都保留
   - 窗口默认在屏幕右下角，拖 ⠿ 移动；任务结束后停留几秒展示结果再自动关闭，✕ 可立即关闭

## 隐私说明

`data/`（`jobs.db`、`messages.json`、`cookies.json`、`fetch_gate.json`、`hud_pos.json` 等）与浏览器 profile（`chrome-profile/`，正式位置 `data/chrome-profile`）均在 `.gitignore` 中排除，不入库。
