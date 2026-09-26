# JobForge Workbench

本地求职工作台：简历 → 关键词 → 岗位抓取（BOSS 直聘）→ 匹配度排序，一条龙单机工具。

## 功能

- **个人资料**：简历上传解析、字段比对采纳、本地规则评分与优化建议、简历预览、导出 PDF
- **智能抓取**：从简历关键词抓取 BOSS 岗位（原生浏览器通道），抓取历史 + 岗位级防重复
- **岗位市场**：岗位落库、JD 详情抓取与正文清洗、匹配度排序
- **投递流水线**：看板拖拽管理 6 状态（discovered / reviewing / applied / interviewing / rejected / offered）
- **面试日程**：从「面试中」岗位安排面试时间与备注
- **消息中心**：只读同步 BOSS 直聘会话（CDP 响应拦截）

## 架构

| 文件 | 作用 |
|---|---|
| `server.py` | FastAPI 入口，`0.0.0.0:8080` |
| `spider.py` / `fetch_jd_native.py` | 岗位列表与 JD 详情抓取（原生键鼠 + UIA TextPattern 通道） |
| `fetch_jd.py` | JD 抓取总入口（native 优先、CDP 兜底）+ JD 正文清洗 |
| `messages.py` | BOSS 消息同步（Playwright CDP 连 9222 浏览器拦截页面响应） |
| `db.py` | SQLite（WAL）：岗位 / 消息 / 个人资料 / 抓取历史 |
| `profile_score.py` | 个人资料本地规则评分引擎（13 项检查） |
| `job-workbench.html` | 前端单页（6 视图） |
| `grab_cookies.py` | 抓取浏览器登录态 cookie 写 `cookies.json` |

## 使用

1. 双击 `setup.bat` 创建 venv 并安装依赖
2. 双击 `run.bat` 启动，浏览器打开 <http://127.0.0.1:8080>
3. 桌面 Chrome 打开并登录 zhipin.com 后即可抓取（抓取会接管键鼠约 8~15 秒；消息刷新需 9222 调试端口的浏览器）

## 隐私说明

`jobs.db`、`messages.json`、`cookies.json`、浏览器 profile 等运行时数据与登录态凭据均在 `.gitignore` 中排除，不入库。
