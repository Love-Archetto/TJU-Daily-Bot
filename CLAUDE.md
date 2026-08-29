# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Identity

**TJU Daily Bot** — 天津大学每日智能信息简报系统. Daily crawler for TJU official websites and WeChat public accounts, AI-generated personalized briefings, pushed to the user's remote repo.

- **Automation**: GitHub Actions (daily cron, cloud)
- **Interaction**: Textual TUI (local, for config/management/manual ops)
- **WeChat data**: pure-remote MP API (`mp.weixin.qq.com/cgi-bin/appmsg`, Cookie+Token+fakeid) primary, RSSHub fallback. Runs in CI (`CI=true`) too — no local service needed. Each source needs a `fakeid` field (generated via `tools/query_biz.py`). Cookie expiry triggers email alert via `src/notifier.py`.

## Reference Documents (read these first)

| Document | Purpose |
|---|---|
| `PROJECT_PLAN.md` | Architecture, feature list, risk plan, acceptance criteria |
| `AGENTS.md` | Development standards, Git discipline, CI/CD rules, tool permissions |
| `BUILD_STEPS.md` | Atomic step-by-step build instructions (send to AI one at a time) |

## Architecture Overview

```
GitHub Actions (cron)          Local TUI (Textual)
   (runs we-mp-rss service)            │
        │   we-mp-rss /rss/fresh       ▼
        ▼                            tui/app.py
   src/main.py                  ├─ agent.py (LLM agent w/ tool calling)
   ├─ crawler/                  ├─ tools.py (path-whitelisted file ops)
   │  ├─ web_crawler.py         ├─ local_git.py (commit/push/pull)
   │  └─ wemp_rss_crawler.py    └─ search_handler.py (unused for TUI search)
   ├─ ai_engine/
   │  ├─ fault_tolerant_client.py
   │  └─ independent_checker.py
        │
        ▼
   output/YYYY-MM-DD_HH-MM-SS.md  ← pushed to remote
   state.json                      ← pushed to remote
   config/                         ← only pushed via TUI button
   history/                        ← NEVER pushed (.gitignore)
```

**Data flow**: In Actions, we-mp-rss service container (扫码授权微信读书) publishes公众号 RSS; `main.py` fetches `{WE_MP_RSS_BASE}/rss/fresh` + web_crawler → AI classifies into 3 parts (keyword hits / AI-recommended / rest) → independent checker validates → report written to `output/` → state.json updated → pushed. Reports accumulate in git (never deleted) — local TUI full-text searches `output/*.md`, auto `git pull` before search.

**Local search**: 本地不装 we-mp-rss。TUI 搜索 = 全文搜 `output/` 历史报告（搜索前自动 `git pull` 同步云端累积的历史）。

## Key Constraints (must follow)

1. **Git**: No `--force`. Code changes use `feat:`/`fix:` prefix (manual). Runtime/data changes use `data:` prefix (TUI/Actions auto).
2. **Path whitelist for tools.py**: read only `config/`, `output/`, `state.json`; write only `config/`; **never** write `state.json` or `history/`.
3. **WeChat source**: 只在 GitHub Actions 内跑 we-mp-rss 容器（service + actions/cache 持久化数据卷）。`main.py` 从 `WE_MP_RSS_BASE`（默认 `http://localhost:8001`）拉公众号 RSS。本地不装 we-mp-rss。
4. **Model degradation**: Function Calling first → on error, retry without `tools` param → natural language mode with JSON repair (`jsonrepair` + regex) → max 2 retries.
5. **Push separation**: `output/` + `state.json` auto-pushed by Actions; `config/` only by TUI button; `history/` never pushed. Reports never deleted → history stays complete for local search.

## Commands

```bash
# Run TUI locally
python -m tui.app

# Run main pipeline directly (needs WEREAD_COOKIE + MP_QUERY_TOKEN for WeChat)
python src/main.py

# Simulate Actions environment
CI=true python src/main.py

# Generate missing fakeid for WeChat sources (once, pre-seeded with a valid Cookie)
python tools/query_biz.py

# Install dependencies (into .venv virtualenv)
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
```

## TUI Command Whitelist

Only these exact matches are treated as commands (everything else is chat input):

| Command | Action |
|---|---|
| `/load_history` | Load last conversation from `history/` |
| `/save` | Save current conversation + config (no Git) |
| `/quit` | Save + Commit & Push, then exit |