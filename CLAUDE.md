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
6. **Branch invariant**: 自动流程（`CI=true` 的 `main.py`）产生的 `data:` 提交**只允许落在 `main`**。本地 `Run-Daily.bat` 发现 HEAD 不在 main 时：工作区干净 → 自动 `checkout main` 再跑；有未提交改动 → **拒绝退出**（exit 1，绝不 stash）。GHA 不自动切，非 main 触发直接失败。推送后用 `ls-remote` 比对远端 SHA —— 防"`git push` 返回 0 但远端没动"的假成功。退出码：`0` 成功 / `1` 硬失败 / `2` 报告已生成但未推送。
7. **Do NOT use `--force`**（任何形式，含 `--force-with-lease`）—— 与约束 1 同源，此处单独强调：non-fast-forward 的恢复手段是 `pull --rebase` 后重试，不是强推。
8. **state.json 完整性**: 它是去重历史的唯一载体（`processed_links` 数千条），被清空的后果是整轮重复处理 + 用空历史覆盖远端。因此：写入必须原子（`save_state` 走同目录 `.tmp` + `os.replace`，防截断 JSON）；磁盘上缺失但 `HEAD` 里有它 → 视为异常**中止**而非从零开始（确实要重置时设 `ALLOW_STATE_RESET=1`）；`commit_data_and_push` 遇到不存在的路径按单个跳过，不让 `git add` 整体失败。

## Commands

```bash
# Run TUI locally
python -m tui.app

# Run main pipeline directly (needs WEREAD_COOKIE + MP_QUERY_TOKEN for WeChat)
python src/main.py

# Simulate Actions environment
CI=true python src/main.py

# Git 提交/推送流程的回归测试（零网络、零鉴权，全部在 tempfile 建的裸库夹具里跑）。
# 改 tui/local_git.py 或 main.py 的收尾逻辑后必须跑。
# 安全契约：main() 第一件事就装上写护栏（凡写真实 state.json / output/ 立即抛异常），
# 末尾用场景 Q 断言真实 state.json 指纹全程未变。新增场景若忘了重定向 STATE_PATH，
# 会当场变红而不是静默改数据 —— 这套件曾两次把真实 state.json 清成 `{}` 且报 ALL PASS。
python tools/test_git_flow.py

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
| `/quit` | Save conversation, then exit (**不碰 Git** — 实测 `tui/app.py` 里只有 `save_history()` + `exit()`，无任何 git 调用) |