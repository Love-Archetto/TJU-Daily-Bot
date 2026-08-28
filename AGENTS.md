# 文件 2：AGENTS.md（开发规范）

# AGENTS.md — 本文件仅用于指导 DeepSeek Harness 开发此项目

本文件定义 TJU Daily Bot 项目的开发规范、文档标准、Git 纪律和 CI/CD 要求，供编写代码的 DSH Agent 使用，与 TUI 对话界面的 Agent 配置（`config/tui_agent.yaml`）完全独立。

## 文档结构

遵循 dsh-doc-standards 原则：
- 每个事实只有一个家，其他地方通过链接引用。
- 自身主题保留完整细节，子节点仅按目的/职责/高级行为概括。

## 文档预算

- 单篇文档原则上不超过 500 行。

## 编写规则

- 写够保留契约所需的内容，删掉推理过程、重复和修饰。
- 每个事实只有一个家，其他地方通过链接引用。
- 代码注释解释"为什么"而非"是什么"。
- 公共 API 必须有文档注释。
- 不保留被注释掉的代码，一律删除。

## 参考项目技术复用原则

本项目以 [AI News Aggregator](https://github.com/your-analogy-repo) 为架构蓝本。遇到技术难点时：

1. 优先深入阅读参考项目的对应源码。
2. 在原有代码基础上做**减法**（删去非天大信源）和**加法**（增加三部分分类、故障转移、TUI Agent、SQLite 搜索索引）。
3. 除非原方案已严重过时，否则不另起炉灶。

---

## 8. 工作流执行规范（强制）

### 8.1 Git 提交控制规范

**Git 提交控制策略**：用户通过 TUI 底部的四个按钮控制提交流程：

| 按钮 | 行为 | 推送远端 | 提交信息前缀 |
| :--- | :--- | :--- | :--- |
| **仅Commit** | 提交本地所有变更 | ❌ 否 | `data:` |
| **Commit & Push** | 提交并推送所有变更 | ✅ 是 | `data:` |
| **仅Save** | 保存文件到本地，不触发 Git | ❌ 否 | 无 |
| **退出** | 自动保存 + Commit & Push | ✅ 是 | `data: session end` |

**GitHub Actions 自动提交**：
- 每日运行完成后，自动提交并推送 **仅 output/ 和 state.json**
- 提交信息使用 `data: daily report YYYY-MM-DD`
- **不推送 config/ 和 history/**

**提交内容分离原则**：
- `output/`：推送至远端
- `config/`：仅用户通过 TUI 按钮显式提交时推送
- `state.json`：Actions 和 TUI 共享，推送至远端
- `history/`：**永不推送**（已加入 .gitignore）

**禁止事项**：
- 禁止使用 `git push --force`
- 禁止提交 `.env` 文件
- 禁止将 `history/` 推送到远端

### 8.2 独立 AI 任务检查机制

- 每次 Actions 运行，生成简报后必须调用独立检查模型（从 `models.yaml` 的 `checker` 字段读取）进行校验。
- **校验内容**：
  - 三部分分类是否严格符合规则
  - 每条信息是否包含**有效总结**和**可访问的原文链接**
  - 增量逻辑是否正确
- **预算与降级**：
  - token 上限 ≤ 500
  - 超时 > 15s 则跳过并记录 WARNING
  - 若检查通过但发现分类错误，输出错误条目编号
  - 若链接失效超过 50%，中止推送
- 检查报告追加至文末。

### 8.3 TUI Agent 工具路径权限

`tools.py` 中的工具函数必须遵守以下约束：

| 工具函数 | 允许读取 | 允许写入 | 推送远端 |
| :--- | :--- | :--- | :--- |
| `read_file` | config/, output/, state.json | N/A | N/A |
| `write_file` | N/A | config/ | 仅用户触发 |
| `append_keyword` | N/A | config/keywords.txt | 仅用户触发 |
| `update_profile` | N/A | config/user_profile.yaml | 仅用户触发 |
| `list_outputs` | output/ | N/A | N/A |
| `open_report` | output/ | N/A | N/A |
| `search` | SQLite 索引 | N/A | N/A |
| `git_commit_only` | N/A | N/A | 用户控制 |
| `git_commit_push` | N/A | N/A | 用户控制 |

**重要约束**：
- 任何工具**禁止写入 state.json**（仅由爬虫模块修改）
- 任何工具**禁止写入 history/**（仅由 Agent 引擎内部管理）
- `write_file` 仅允许路径以 `config/` 开头，否则抛出 PermissionError

### 8.4 Agent JSON 纠错层要求

当 TUI Agent 使用自然语言降级模式时，必须实现 JSON 纠错：

1. 尝试 `json.loads` 解析模型返回的字符串。
2. 若失败，使用 `jsonrepair` 库尝试修复。
3. 若仍失败，使用正则表达式提取 `tool` 和 `args` 字段。
4. 若全部失败，返回友好提示并最多重试 2 次。

### 8.5 对话历史存储与轮转规范

- **存储**：每次用户退出 TUI，将当前对话序列化为 JSON 保存至 `history/` 文件夹。
- **文件命名**：`conversation_YYYY-MM-DD_HH-MM-SS.json`
- **内容结构**：
  ```json
  {
    "timestamp": "2026-08-28T15:30:00",
    "model": "deepseek-v4",
    "messages": [
      {"role": "user", "content": "帮我添加关键词：二次选拔"},
      {"role": "assistant", "content": "已添加", "tool_calls": [...]}
    ]
  }
  ```
- **轮转规则**：当文件数超过 `max_history_files`（默认 30）时，解析文件名中的时间戳，删除时间戳最早的（最旧的）文件。
- **推送规则**：`history/` **永不推送至远端**（已加入 .gitignore）。
- **加载方式**：
  - 启动时自动检测最近历史，提示用户是否加载。
  - 界面提供「加载上次对话」按钮。
  - 支持 `/load_history` 命令。
  - 若用户选择"否"，则清空当前对话，从空白状态开始。

### 8.6 搜索索引规范（SQLite）

为优化搜索性能，使用 SQLite 构建索引：

- **索引表结构**：
  ```sql
  CREATE TABLE articles (
    id INTEGER PRIMARY KEY,
    title TEXT,
    summary TEXT,
    link TEXT UNIQUE,
    source TEXT,
    publish_time TEXT,
    output_file TEXT
  );
  ```
- **更新时机**：每次新报告生成后，将文章信息插入索引。
- **搜索流程**：优先查询 SQLite，若命中则直接返回；若未命中，降级为全文扫描（记录日志）。
- **索引维护**：定期清理孤立记录（对应 output 文件已删除）。

### 8.7 `prefer_function_calling` 配置语义

`tui_agent.yaml` 中的 `prefer_function_calling` 字段含义：

| 值 | 行为 | 适用场景 |
| :--- | :--- | :--- |
| `true`（默认） | 优先尝试 Function Calling。若模型支持则使用 `tools` 参数；若 API 返回错误或模型返回普通文本，则自动移除 `tools` 参数，使用自然语言提示**重试一次**。 | DeepSeek-V4、GPT-4 等支持工具调用的模型 |
| `false` | 强制使用自然语言提示（在 `system_prompt` 中描述工具用法）。 | 不兼容的中转站或非工具调用模型 |

### 8.8 模型降级兜底方案

当模型在自然语言模式下仍无法正确调用工具时：

1. **严格 System Prompt**：明确要求只输出 JSON，格式为 `{"tool":"xxx","args":{...}}`
2. **Few-shot 示例**：在对话初始注入 1-2 个正确调用示例
3. **常见意图预判**：对"添加关键词""修改画像"等高频操作，在 Agent 层预先匹配关键词，跳过 AI 解析
4. **最多 2 次重试**：解析失败时，重新调用模型并告知上次错误原因
5. **友好错误提示**：若 2 次重试仍失败，返回"抱歉，我未能理解您的指令，请重新描述"，不阻塞 TUI 流程

### 8.9 命令白名单规则

`app.py` 中消息处理逻辑：仅当用户输入**完全匹配**以下白名单命令时，才作为命令处理：

| 命令 | 功能 |
| :--- | :--- |
| `/load_history` | 加载最近一次对话历史 |
| `/save` | 手动保存当前对话和配置（不触发 Git 操作） |
| `/quit` | 保存并退出 TUI（自动执行 Commit & Push） |

其他任何输入（包括以 `/` 开头但不在白名单中的内容）均视为普通对话输入。

### 8.10 网站爬虫实现规范

- **抓取策略**：列表页 → 详情页（深度限制为 1 层），不递归扫描全站子目录。
- **列表页配置**：用户须在 `sources.yaml` 中配置：
  - 列表页 URL
  - CSS 选择器：标题、链接、发布时间
- **增量检测**：
  - 从每个列表页解析文章链接及发布时间。
  - 若发布时间 > `state.json` 中的 `last_run`，且链接不在 `processed_links` 中，则视为新内容。
- **失败处理**：若某列表页抓取失败，记录 `ERROR` 日志并跳过该信源。

### 8.11 公众号爬虫实现规范（we-mp-rss 方案）

公众号数据通过 **we-mp-rss** 独立服务获取，而非直接抓取微信接口。

**we-mp-rss 服务部署**：
- 使用 `docker-compose.yml` 一键启动
- 服务提供 RSS 或 REST API 接口
- 用户需提供微信读书 Cookie（提供提取教程）

**爬虫实现**：

`src/crawler/wechat_rss_crawler.py` 必须实现：

1. `check_service_health()`：检测 we-mp-rss 服务是否可访问
2. `fetch_from_rss(rss_url)`：请求 RSS 源，解析 XML，返回文章列表
3. `fetch_from_api(api_url, gzh_name)`：如服务提供 REST API，直接请求 JSON
4. `fetch_articles_from_gzh(gzh_name)`：主函数，从 `sources.yaml` 读取 `rss_url` 或 `api_url`，调用对应方法

**服务不可用时的降级**：
- 若 `check_service_health()` 返回 False，在 TUI 中显示警告："⚠️ we-mp-rss 服务未启动，请执行 docker-compose up -d"
- 自动启用手动录入模式：TUI 提供表单，用户粘贴文章标题、链接、摘要
- 手动录入的数据与自动抓取的数据格式一致，存入 `state.json` 去重

**GitHub Actions 环境**：
- Actions 运行在云端，无法访问本地 `localhost` 的 we-mp-rss 服务
- 在 Actions 中检测到 `CI=true` 环境变量时，**跳过所有公众号信源**
- 日志记录："Skipping wechat RSS in Actions environment"
- 报告仅包含网站信源内容，公众号部分显示 "请使用本地 TUI 手动导入公众号文章"

### 8.12 初次运行环境检查

启动 TUI 或 Actions 时，必须执行以下检查：

1. **检测 .env 文件**：
   - 若不存在，输出引导信息并创建模板 `.env.example`
   - 引导用户填入 API 密钥
   - 提示用户执行 `cp .env.example .env` 并编辑

2. **检测 config/ 必要文件**：
   - `sources.yaml`：若不存在，从模板生成并引导用户配置信源
   - `models.yaml`：若不存在，从模板生成并提示用户检查 API 密钥
   - `user_profile.yaml`：若不存在，生成空模板

3. **检测 we-mp-rss 服务（TUI 模式）**：
   - 发送健康检查请求到 `http://localhost:4000/health`（或配置的地址）
   - 若不可用，显示警告并提示启动命令

4. **安全警告**：
   - 若检测到 .env 文件被提交到 Git，输出 WARNING 并建议立即从历史中移除

### 8.13 Git 冲突处理规范

当 `git push` 或 `git pull --rebase` 遇到冲突时：

1. **检测冲突类型**：
   - `output/` 文件冲突：保留远端版本，本地重命名并提示
   - `config/` 文件冲突：**停止推送**，记录冲突文件列表，提示用户手动解决
   - `state.json` 冲突：**停止所有操作**，提示用户手动合并

2. **用户交互**：
   - TUI 中显示冲突文件列表和解决指引
   - 提供命令：`/resolve` 尝试自动合并（仅对 output/ 有效）

3. **Actions 环境**：
   - 若冲突无法自动解决，放弃本次推送，记录 ERROR 日志
   - 触发通知，提示用户手动处理

### 8.14 `state.json` 数据结构规范

```json
{
  "last_run": "2026-08-28T08:00:00",
  "processed_links": [
    "https://www.tju.edu.cn/tzgg/2026/08/27/abc.html"
  ],
  "source_last_fetch": {
    "官网-通知公告": "2026-08-28T08:00:00",
    "天大官微": "2026-08-28T08:00:00"
  }
}
```

- `last_run`：上次抓取的时间戳
- `processed_links`：全局已处理链接集合
- `source_last_fetch`：每个信源的最后抓取时间

---

## 验收门禁

任何涉及文档变更的 PR 须通过以下检查：

- [ ] 所有文档单篇 ≤ 500 行
- [ ] `docker-compose.yml` 可正常启动 we-mp-rss 服务
- [ ] `wechat_rss_crawler.py` 能成功从 we-mp-rss 获取数据
- [ ] TUI 启动时检测 we-mp-rss 服务健康状态
- [ ] we-mp-rss 不可用时，TUI 提供手动录入入口
- [ ] Actions 运行日志正确显示 "Skipping wechat RSS in Actions environment"
- [ ] `tools.py` 实现路径白名单
- [ ] `agent.py` 实现 JSON 纠错层及 `prefer_function_calling` 判断逻辑
- [ ] `agent.py` 实现历史保存与轮转清理
- [ ] `app.py` 实现四个 Git 控制按钮
- [ ] `app.py` 实现「加载上次对话」按钮
- [ ] 命令识别采用白名单方式
- [ ] 网站爬虫仅抓取 `sources.yaml` 中配置的列表页
- [ ] `search_handler.py` 使用 SQLite 索引
- [ ] `.gitignore` 包含 `history/` 和 `.env`
- [ ] 初次运行检测并引导创建 `.env` 文件
- [ ] Git 提交不使用 `--force`，推送分支为 `main`
- [ ] Actions 提交仅包含 `output/` 和 `state.json`