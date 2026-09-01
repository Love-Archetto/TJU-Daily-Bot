# TJU Daily Bot — 天津大学每日智能信息简报

每天从天津大学官方站点 + 微信公众号抓取新闻，用 AI 分类汇总成一份 Markdown 日报，并推送到远程仓库。

- **网站源**：TJU 各学院官网通知页（`config/sources.yaml` 里 `type: web` 的信源）
- **公众号源**：通过**微信读书**抓取已订阅公众号文章（`type: wechat_rss`）
- **交付物**：`output/YYYY-MM-DD_HH-MM-SS.md` 日报 + `state.json`，推送回 git
- **交互**：本地 TUI（`python -m tui.app`）可选

---

## 一键全流程（推荐）

双击根目录的 **`Run-Daily.bat`**，或命令行运行：

```bat
Run-Daily.bat
```

它依次完成：检查 `.venv` 与 `.env` → 确保调试 Edge 就绪 → **抓取（官网 + 公众号）→ AI 汇总 → 生成日报 → Git 提交并推送**。

> 等价手动命令：`CI=true FORCE=1 .venv\Scripts\python.exe src\main.py`
> - `CI=true` → 自动 `git add -A && commit(data: 前缀) && push origin main`
> - `FORCE=1` → 绕过"一天一次"调度闸门，保证每次都真正运行

### 首次使用前

```bash
# 1) 建虚拟环境 + 装依赖
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt

# 2) 配置 .env（至少一个 AI Key）
copy .env.example .env
#    编辑 .env：填 DEEPSEEK_API_KEY 或 TJU_API_KEY 等

# 3) Git 凭据：确认本机能 push 到 origin/main（HTTPS 需已认证 / SSH 已配）
```

---

## 公众号抓取：微信读书 / 调试 Edge（CDP 复用真实会话）

公众号通过微信读书取，核心要求：**复用你已登录、已过验证码的真实浏览器（Edge）**——不能无头自开（会触发腾讯防水墙验证码，导致拿不到）。

### 第一次配置（一次性）

1. 运行启动器拉一个**专用调试 Edge**：

   ```bash
   python tools/start_weread_edge.py
   # 或双击 tools\start_weread_edge.bat
   ```

2. 在弹出的 Edge 里：
   - 手机微信 **扫码登录 `weread.qq.com`**（微信读书）
   - **点进任意一个你订阅的公众号**
   - 打开该公众号的**文章阅读器页**，保持这个标签页打开

> 之后保持这个 Edge 窗口开着即可，登录态长期有效；失效时重扫码一次即可。
> 这里用**专用 `user-data-dir`**（默认 `%LOCALAPPDATA%\TJUNews\weread_edge_profile`），与你的日常浏览器隔离。

### 原理（为什么这样）

`/web/mp/articles`（拿公众号文章列表）**必须在「阅读器页」上下文里发**：无头自开会弹验证码、或返回微信读书 `-2041` 错误。参考 [Pengyf04/weread-mp-fetcher](https://github.com/Pengyf04/weread-mp-fetcher) 的做法——**复用已登录的真实浏览器标签页**执行请求。我们用 Playwright `connect_over_cdp` 连到上述调试 Edge 的阅读器标签页完成抓取。

### 正文获取方式

公众号文章正文用 **UA 伪装法**（声明 `MicroMessenger` 的 User-Agent 直接 `requests` 拿完整 HTML），稳定、无需浏览器、无额外依赖。

### 相关配置（`.env`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `WEREAD_EDGE_PORT` | `9333` | 调试 Edge 的 CDP 端口 |
| `WEREAD_EDGE_PROFILE` | `%LOCALAPPDATA%\TJUNews\weread_edge_profile` | 专用 Edge 用户数据目录（登录态） |

不再需要 `WEREAD_COOKIE`（旧方案遗留，登录态现保存在 Edge 会话里）。

---

## 手动 / 调试命令

```bash
# 跑一次完整主流程（本地，不 push）
FORCE=1 FAST_TEST=1 .venv\Scripts\python.exe src\main.py

# 只抓公众号（验证这一路）
FAST_TEST=1 .venv\Scripts\python.exe -c "from src.crawler.weread_mp_crawler import fetch_wechat_articles as f; a,i=f(); print(len(a)); [print(x['title'],x['link']) for x in a[:3]]"

# 本地 TUI（配置/搜索历史）
python -m tui.app
```

> **调试提速**：设 `FAST_TEST=1` 可跳过公众号请求间的 3min 限频间隔。
> **真实运行**：不要乱加速（避免触发微信读书限频）。

---

## 架构速览

```
Run-Daily.bat  →  src/main.py  →  web_crawler（官网） + weread_cdp（复用 Edge 取公众号）
                                        ↓
                    fault_tolerant_client（AI 分类/摘要） + independent_checker（独立检查）
                                        ↓
                    output/YYYY-MM-DD_HH-MM-SS.md  →  commit_and_push 推回远程
```

详见 `AGENTS.md`（开发规范）、`PROJECT_PLAN.md`（架构）、`CLAUDE.md`（约束）。
