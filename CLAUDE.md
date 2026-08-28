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
        │                            │
        ▼                            ▼
   src/main.py                  tui/app.py
   ├─ crawler/                  ├─ agent.py (LLM agent w/ tool calling)
   │  ├─ web_crawler.py         ├─ tools.py (path-whitelisted file ops)
   │  └─ mp_wechat_crawler.py   ├─ local_git.py (commit/push/pull)
   ├─ ai_engine/                └─ search_handler.py (SQLite index)
   │  ├─ fault_tolerant_client.py
   │  └─ independent_checker.py
   ├─ notifier.py (SMTP alert)
   tools/query_biz.py (generate fakeid)
        │
        ▼
   output/YYYY-MM-DD_HH-MM-SS.md  ← pushed to remote
   state.json                      ← pushed to remote
   config/                         ← only pushed via TUI button
   history/                        ← NEVER pushed (.gitignore)
```

**Data flow**: Crawlers fetch articles (web + WeChat via MP API/RSSHub) → AI classifies into 3 parts (keyword hits / AI-recommended / rest) → independent checker validates → report written to `output/` → state.json updated → SQLite index updated. WeChat Cookie expiry sends an SMTP alert via `src/notifier.py`.

## Key Constraints (must follow)

1. **Git**: No `--force`. Code changes use `feat:`/`fix:` prefix (manual). Runtime/data changes use `data:` prefix (TUI/Actions auto).
2. **Path whitelist for tools.py**: read only `config/`, `output/`, `state.json`; write only `config/`; **never** write `state.json` or `history/`.
3. **WeChat in CI**: Uses MP API (`mp.weixin.qq.com/cgi-bin/appmsg`) which is public-network accessible — runs in CI normally (no skip). Requires `WEREAD_COOKIE` + `MP_QUERY_TOKEN` + per-source `fakeid`. If Cookie expires, `mp_wechat_crawler` flags it and `main.py` sends an SMTP alert (never silent-fail).
4. **Model degradation**: Function Calling first → on error, retry without `tools` param → natural language mode with JSON repair (`jsonrepair` + regex) → max 2 retries.
5. **Push separation**: `output/` + `state.json` auto-pushed by Actions; `config/` only by TUI button; `history/` never pushed.

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