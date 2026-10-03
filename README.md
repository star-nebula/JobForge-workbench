# JobForge Workbench

**Language / 语言 / 言語 / Idioma / 언어 / Langues / Sprachen:** [English](README.md) | [简体中文](readme/README_zh-CN.md) | [日本語](readme/README_ja.md) | [한국어](readme/README_ko.md) | [Français](readme/README_fr.md) | [Deutsch](readme/README_de.md) | [Español](readme/README_es.md)

A local job-hunting workbench: resume → keywords → job scraping (BOSS Zhipin) → match ranking, all in one single-machine pipeline.

![Dashboard](docs/screenshots/dashboard.png)

## Features

- **Profile**: resume upload & parsing, field-level comparison and adoption, local rule-based scoring with optimization suggestions, resume preview, PDF export
- **Smart scraping**: scrape BOSS jobs from resume keywords (native browser channel), with scrape history and per-job de-duplication
- **Job market**: jobs persisted to DB, JD detail fetching with body cleaning, match-based sorting, JD fetch stats (fetched / not fetched / suspected incomplete — click the pills to filter; suspected-incomplete ones can be manually confirmed or re-fetched in the detail modal). Two-tier matching: **tag pre-screening** (local 4-dimension rules: skills / intent / salary / city, computed at scrape time; skill denominator = job skill tags, word-normalized exact matching, salary normalized to K, true 0–100 with no floor) and **JD fine-matching** (LLM reads the full JD plus hard info — tags / salary / city — produced in the detail modal or batch analysis; cards show a blue "AI xx" badge)
- **AI capabilities** (multi-model config, OpenAI-compatible protocol, keys stored only in local SQLite): BOSS greeting-message generation, AI match analysis (auto-runs in the detail modal + one-click batch in the job market), resume polishing (diff comparison before adoption)
- **Application pipeline**: kanban with drag-and-drop across 6 statuses (discovered / reviewing / applied / interviewing / rejected / offered)
- **Interview schedule**: arrange interview times and notes from the "interviewing" column
- **Message center**: read-only sync of BOSS Zhipin conversations (CDP response interception)
- **Floating progress window**: stays on top during scraping with pause / resume / stop buttons. The window never steals focus and lets mouse clicks pass through, so it **will not interrupt an ongoing keyboard-and-mouse scraping session**; drag the ⠿ handle to move it

## Project Layout

```
JobForge-workbench/
├─ src/jobforge/                 # Code: Python package
│  ├─ server.py                  # FastAPI entry point
│  ├─ paths.py                   # Single source of project paths (code and data locations decoupled)
│  ├─ spider.py  fetch_jd.py  fetch_jd_native.py  fetch_gate.py
│  ├─ db.py  llm.py  profile_score.py
│  └─ tools/                     # Subprocess scripts, launched by the server as `python -m jobforge.tools.*`
│     └─ hud.py  messages.py  grab_cookies.py
├─ web/job-workbench.html        # Frontend single page (6 views)
├─ data/                         # Runtime data (not committed): jobs.db, cookies.json, messages.json, gate/throttle files, chrome-profile/
├─ tests/                        # pytest regression suite (`venv\Scripts\python.exe -m pytest tests/`; test counts are not documented here — run them to see)
├─ run.bat  setup.bat  requirements.txt  README.md
```

All data file paths come from `paths.py`; no module derives them from its own `__file__` — moving the code never drags the data along.
The user-data-dir of the CDP-debugged Chrome (holding the BOSS login state) also lives under the data directory: `data/chrome-profile/`.

There is a single vocabulary for application statuses: the frontend `STATUS_META` (kanban columns, the status selector in the detail modal, and the dashboard pipeline all derive from it) and the backend `db.VALID_STATUSES`. The two sets are identical and every status has a reachable write path, pinned by `tests/test_frontend_status_contract.py` — there was once a bug where 6 statuses were declared but the kanban rendered only 4 columns, so `rejected`/`offered` had no way to be set in the UI and 114 jobs in the DB were left with only two values.

Note: `.bat` files must keep CRLF line endings (`.gitattributes` declares `*.bat text eol=crlf`) — under "code-page switch via `chcp` + Chinese comments + bare LF", cmd.exe misparses by byte offset and silently eats the start of the `set "PYTHONPATH=..."` line, showing up as `ModuleNotFoundError: No module named 'jobforge'` at startup.

Manual startup (IDE / command line) requires `src` on `PYTHONPATH`, otherwise `import jobforge` fails:

```
set PYTHONPATH=%CD%\src
venv\Scripts\python.exe -m jobforge.server
```

## Architecture

| File | Role |
|---|---|
| `src/jobforge/server.py` | FastAPI entry point, `127.0.0.1:8080` (`--port` can override, useful for debugging a second instance) |
| `src/jobforge/paths.py` | Single source of project paths: `PROJECT_ROOT` / `DATA_DIR` / `WEB_DIR` |
| `src/jobforge/spider.py` / `fetch_jd_native.py` | Job-list and JD-detail scraping (native keyboard/mouse + UIA TextPattern channel) |
| `src/jobforge/fetch_jd.py` | JD fetching entry point (native first, CDP fallback) + JD body cleaning |
| `src/jobforge/tools/hud.py` | Floating progress window (separate process; the three window constraints — topmost + no focus stealing + click-through — are documented in the file-header comment) |
| `src/jobforge/fetch_gate.py` | Cross-process pause/stop gate: state lands in `data/fetch_gate.json`, shared by the server thread and scraping subprocesses |
| `src/jobforge/tools/messages.py` | BOSS message sync (Playwright CDP connects to the 9222 browser and intercepts page responses) |
| `src/jobforge/db.py` | SQLite (WAL): jobs / messages / profile / scrape history |
| `src/jobforge/profile_score.py` | Local rule-based profile scoring engine (13 checks) |
| `src/jobforge/llm.py` | LLM capability layer (OpenAI-compatible chat client + three feature functions: greeting / match analysis / resume polishing) |
| `web/job-workbench.html` | Frontend single page (6 views) |
| `src/jobforge/tools/grab_cookies.py` | Grabs browser login cookies and writes `data/cookies.json` |

## Usage

1. Double-click `setup.bat` to create the venv and install dependencies
2. Double-click `run.bat` to start, then open <http://127.0.0.1:8080> in a browser
3. Open desktop Chrome, log in to zhipin.com, and scraping becomes available (scraping takes over keyboard & mouse for ~8–15 s; message refresh requires the browser opened with the 9222 debug port)
4. Configure an AI model via the ⚙ at the top right to unlock greeting generation / match analysis / resume polishing (works with DeepSeek, Qwen, Zhipu, Ollama, or any OpenAI-compatible service)
5. Prerequisite for batch AI analysis: desktop Chrome is open and logged in to zhipin.com (window not minimized); analysis takes over keyboard & mouse, and 3 consecutive failures trip an automatic circuit breaker. The server has a single-instance guard — a second launch is rejected
6. Scrape progress shows in the floating window (open manually via "🪟 Progress Window" in the top bar; it also auto-opens when a scrape starts):
   - **Pause** only holds at safe points (job boundaries, throttle waits) and never splits a single keyboard/mouse action in half; paused time does not count against throttling, so no re-wait after resume
   - **Stop** takes effect within seconds (it also kills the running scrape subprocess); jobs already scraped and AI analyses already finished are kept
   - The window defaults to the bottom-right corner; drag ⠿ to move it. After a task ends it stays a few seconds to show results and then closes automatically; ✕ closes it immediately

## Privacy

`data/` (`jobs.db`, `messages.json`, `cookies.json`, `fetch_gate.json`, `hud_pos.json`, etc.) and the browser profile (`chrome-profile/`, canonical location `data/chrome-profile`) are excluded via `.gitignore` and never committed.
