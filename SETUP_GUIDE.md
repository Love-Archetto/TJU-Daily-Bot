# TJU Daily Bot — GitHub Actions 首次跑通清单（126 邮箱版）

面向首次在 GitHub Actions 上跑通本项目的完整操作指南。假设使用 **126 邮箱** 作为告警邮箱。

---

## 一、前置：已确认的内容

- 代码已推送到 `github.com/Love-Archetto/TJU-Daily-Bot`（main 分支）
- 公众号 fakeid 已内置于 `config/sources.yaml`（110 个）
- AI 主模型：TJU 开源大模型平台 `tju-llm`（vLLM 服务）
- 公众号数据：微信 MP API（公网可达）+ RSSHub 降级

## 二、需要准备的 6 项素材（先备齐再配置）

| # | 素材 | 说明 / 获取位置 |
|---|---|---|
| 1 | **TJU API Key** | 你的 TJU 平台 key（以 `tk-` 开头），TJU 平台后台获取 |
| 2 | **微信 Cookie** | `slave_user=...`，mp.weixin.qq.com 后台 F12 获取 |
| 3 | **微信 token** | 同页面 URL 中 `token=` 后面的数字 |
| 4 | **126 邮箱地址** | 你的完整邮箱，如 `you@126.com` |
| 5 | **126 邮箱授权码** | 见下方「获取 126 授权码」 |
| 6 | **通知目标邮箱** | 告警发给哪个邮箱，通常就是你的 126 邮箱 |

## 三、获取 126 邮箱授权码（重点）

> ⚠️ 授权码 ≠ 邮箱密码。授权码是网易用于第三方客户端登录的专用码。

1. 登录 126 邮箱网页版 `https://mail.126.com`
2. 点击顶部 **设置**（或右上角齿轮）→ **常规设置** 或 **来信分类** 旁的标签
3. 找到 **POP3/SMTP/IMAP**（不同版本入口：设置 → 客户端协议 / POP3/SMTP 服务）
4. 勾选开启 **SMTP 服务**（或 IMAP/SMTP 服务）
5. 按提示用**手机发送短信验证**
6. 验证后，页面**显示一串 16 位客户端授权码**（字母）
7. **立即复制保存**——授权码只显示一次，刷新后不再显示

> 若找不到入口：顶部搜索「POP3」或「客户端授权」，通常「设置」→ 全部设置 →「POP3/SMTP/IMAP」。

## 四、配置 GitHub Secrets

进入仓库 → **Settings → Secrets and variables → Actions → New repository secret**，逐个添加：

### 必需（AI + 公众号抓取）

| Secret 名 | 值 |
|---|---|
| `TJU_API_KEY` | 你的 `tk-` 开头 TJU 平台 key |
| `WEREAD_COOKIE` | 你那整串 Cookie（`slave_user=...` 到 `...ij8A`） |
| `MP_QUERY_TOKEN` | mp.weixin.qq.com URL 里的 token 数字 |

### 告警（126 邮箱，可选但推荐）

| Secret 名 | 值（126 邮箱） |
|---|---|
| `SMTP_HOST` | `smtp.126.com` |
| `SMTP_PORT` | `465` |
| `SMTP_TLS` | `true` |
| `SMTP_USER` | 你的 126 邮箱，如 `you@126.com` |
| `SMTP_PASSWORD` | 上面获取的 **16 位授权码**（不是密码） |
| `SMTP_FROM` | 你的 126 邮箱（同 SMTP_USER） |
| `NOTIFY_TO` | 告警接收邮箱，如 `you@126.com` |

### 可选（备用，不配也能跑）

| Secret 名 | 值 |
|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek key（若你有） |
| `OPENAI_API_KEY` | OpenAI key（若你有） |

---

## 五、验证配置

1. 配置完成后，GitHub Secrets 页面应看到所有 Secret 名称（值被隐藏为 `••••`）
2. 确认没有把 `.env` 提交到仓库（它含真实 key，git 已忽略）

## 六、触发工作流

1. 仓库 → **Actions** 标签
2. 左侧选 **TJU Daily Bot**
3. 右侧 **Run workflow** → 确认 → **Run workflow**
4. 等待完成（110 个公众号 × 6 秒 ≈ **11~15 分钟**）

## 七、如何解读运行结果

进入该次运行 → **build** job → **python src/main.py** 步骤日志，重点看：

| 日志特征 | 含义 | 处理 |
|---|---|---|
| `Fetched N articles`（有数量） | 公众号抓取成功 | ✅ 方案验证通过 |
| `未配置 WEREAD_COOKIE` | Cookie secret 没配对 | 检查 Secret 名 |
| `freq control` | 触发了微信频控 | 提高 `MP_DELAY_SECONDS`，或减少公众号 |
| `cookie expired` | Cookie 失效 | 重新获取并更新 Secret，应收到告警邮件 |
| 无 AI 内容 | `tju-llm` 没生效 | 确认 `TJU_API_KEY` secret |
| 报错 `Authentication` | 126 授权码错误 | 重新获取授权码 |

## 八、首次跑通后

- `output/` 下生成 `YYYY-MM-DD_HH-MM-SS.md` 报告并自动 push
- 后续每日 8:00（UTC+8 需换算）由 schedule 自动运行

---

## 常见问题（126 邮箱 / 微信）

- **126 邮箱 SMTP 连不上**：确认授权码正确、SMTP_PORT=465、SMTP_TLS=true
- **微信 Cookie 常过期**：属正常，过期时会收到邮件提醒（若配好 SMTP）
- **想看某个公众号有没有抓到**：在 `output/` 报告里搜公众号名
