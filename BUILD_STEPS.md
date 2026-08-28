# 文件 3：BUILD_STEPS.md（可逐条发送的构建指令）

# BUILD_STEPS.md — 可逐条发送给 AI 的构建指令

## 使用说明

本文件将整个项目拆分为 **原子化步骤**，每一步都明确告知要创建的文件、写入的内容或执行的命令。您可**逐条复制**发给 AI，让其执行并确认结果，再继续下一步。

---

### Step 0：环境初始化

**发送给 AI 的指令**：

```
在项目根目录执行以下操作：
1. 初始化 Git 仓库：git init
2. 创建以下目录：tui/ config/ src/crawler/ src/ai_engine/ output/ history/
3. 创建 .env.example 文件，内容如下：
OPENAI_API_KEY=your_key_here
DEEPSEEK_API_KEY=your_key_here
QWEN_API_KEY=your_key_here
WEREAD_COOKIE=your_weread_cookie_here  # we-mp-rss 需要的微信读书 Cookie

4. 创建 .gitignore 文件，内容如下：
.env
__pycache__/
*.pyc
history/
playwright-browsers/
search_index.db
.DS_Store
.vscode/
.idea/
wechat-rss-data/  # we-mp-rss 数据目录

5. 创建 requirements.txt，内容如下：
textual>=0.41.0
requests>=2.31.0
beautifulsoup4>=4.12.0
openai>=1.0.0
pyyaml>=6.0
python-dotenv>=1.0.0
json-repair>=0.1.0
feedparser>=6.0.0  # 用于解析 RSS

6. 安装依赖：pip install -r requirements.txt
```

---

### Step 1：创建 docker-compose.yml（we-mp-rss 服务）

**发送给 AI 的指令**：

```
创建 docker-compose.yml，写入以下内容：
version: '3.8'
services:
  we-mp-rss:
    image: rachelos/we-mp-rss:latest
    container_name: wechat-rss
    ports:
      - "4000:4000"
    volumes:
      - ./wechat-rss-data:/app/data
    environment:
      - WEREAD_COOKIE=${WEREAD_COOKIE}
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:4000/health"]
      interval: 30s
      timeout: 10s
      retries: 3

说明：镜像名需根据 we-mp-rss 项目实际发布情况调整。
     用户首次启动前需执行 docker-compose up -d。
```

---

### Step 2：创建信源配置文件

**发送给 AI 的指令**：

```
创建 config/sources.yaml，写入以下内容：
sources:
  # 网站信源
  - name: "天大官网-通知公告"
    type: "web"
    url: "https://www.tju.edu.cn/tzgg/"
    selectors:
      title: "h2 a"
      link: "h2 a"
      time: ".date"
  
  - name: "天大教务处"
    type: "web"
    url: "https://jwc.tju.edu.cn/tzgg/"
    selectors:
      title: ".list-title a"
      link: ".list-title a"
      time: ".list-time"
  
  # 公众号信源（通过 we-mp-rss）
  - name: "天大官微"
    type: "wechat_rss"
    rss_url: "http://localhost:4000/feed/天津大学"  # we-mp-rss 生成的 RSS 地址
    # 或者使用 API 方式（如 we-mp-rss 支持）
    # api_url: "http://localhost:4000/api/articles"
    gzh_name: "天津大学"
```

---

### Step 3：创建模型配置文件

**发送给 AI 的指令**：

```
创建 config/models.yaml，写入以下内容：
main_models:
  - model_name: "deepseek-chat"
    api_base: "https://api.deepseek.com/v1"
    api_key_env: "DEEPSEEK_API_KEY"
  - model_name: "gpt-4o-mini"
    api_base: "https://api.openai.com/v1"
    api_key_env: "OPENAI_API_KEY"
  - model_name: "qwen-max"
    api_base: "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key_env: "QWEN_API_KEY"

checker:
  model_name: "gpt-4o-mini"
  api_base: "https://api.openai.com/v1"
  api_key_env: "OPENAI_API_KEY"
```

---

### Step 4：创建 TUI Agent 配置文件

**发送给 AI 的指令**：

```
创建 config/tui_agent.yaml，写入以下内容：
prefer_function_calling: true

model:
  model_name: "deepseek-chat"
  api_base: "https://api.deepseek.com/v1"
  api_key_env: "DEEPSEEK_API_KEY"

system_prompt: |
  你是一个帮助天津大学学生管理日常信息的助手。你可以调用工具完成操作，请根据用户意图选择合适的工具。
  调用工具时，请严格按照以下 JSON 格式返回（不要添加任何额外文字）：
  {"tool": "工具名称", "args": {"参数名": "参数值"}}
  
  可用工具列表：
  - read_file: 读取配置文件或报告
  - write_file: 写入配置文件（仅限 config/ 目录）
  - append_keyword: 添加关键词到 keywords.txt
  - update_profile: 更新用户画像（degree/college/major）
  - list_outputs: 列出所有报告
  - open_report: 打开指定报告
  - search: 在已抓取内容中搜索
  - git_commit_only: 仅提交不推送
  - git_commit_push: 提交并推送

temperature: 0.3
max_tokens: 800
max_history_turns: 10
history_dir: "history"
max_history_files: 30

tools_schema:
  - type: "function"
    function:
      name: "read_file"
      description: "读取文件内容"
      parameters:
        type: "object"
        properties:
          path: {"type": "string", "description": "文件路径"}
        required: ["path"]
  - type: "function"
    function:
      name: "write_file"
      description: "写入配置文件（仅限 config/ 目录）"
      parameters:
        type: "object"
        properties:
          path: {"type": "string", "description": "目标路径"}
          content: {"type": "string", "description": "文件内容"}
        required: ["path", "content"]
  - type: "function"
    function:
      name: "append_keyword"
      description: "追加关键词到 keywords.txt"
      parameters:
        type: "object"
        properties:
          word: {"type": "string", "description": "要添加的关键词"}
        required: ["word"]
  - type: "function"
    function:
      name: "update_profile"
      description: "更新用户画像"
      parameters:
        type: "object"
        properties:
          field: {"type": "string", "enum": ["degree", "college", "major"]}
          value: {"type": "string"}
        required: ["field", "value"]
  - type: "function"
    function:
      name: "list_outputs"
      description: "列出所有报告"
      parameters:
        type: "object"
        properties: {}
  - type: "function"
    function:
      name: "open_report"
      description: "打开指定报告"
      parameters:
        type: "object"
        properties:
          filename: {"type": "string", "description": "报告文件名"}
        required: ["filename"]
  - type: "function"
    function:
      name: "search"
      description: "搜索已抓取内容"
      parameters:
        type: "object"
        properties:
          query: {"type": "string", "description": "搜索关键词"}
          source: {"type": "string", "description": "限定信源名称"}
        required: ["query"]
  - type: "function"
    function:
      name: "git_commit_only"
      description: "仅提交不推送"
      parameters:
        type: "object"
        properties:
          message: {"type": "string"}
        required: ["message"]
  - type: "function"
    function:
      name: "git_commit_push"
      description: "提交并推送"
      parameters:
        type: "object"
        properties:
          message: {"type": "string"}
        required: ["message"]
```

---

### Step 5：创建用户画像和关键词文件

**发送给 AI 的指令**：

```
创建 config/user_profile.yaml，写入：
degree: "本科"
college: "计算机科学与技术学院"
major: "计算机科学与技术"

创建 config/keywords.txt，写入：
二次选拔
转专业
奖学金
实习
保研
竞赛
```

---

### Step 6：实现网站爬虫

**发送给 AI 的指令**：

```
创建 src/crawler/web_crawler.py，实现：
- fetch_articles_from_list_page(url, selectors)
  - 使用 requests + BeautifulSoup
  - 根据 selectors 提取标题、链接、发布时间
  - 返回 [{"title":..., "link":..., "publish_time":...}]
- User-Agent 轮换，最多重试 3 次
- 错误时返回空列表并记录日志
```

---

### Step 7：实现公众号 RSS 爬虫（we-mp-rss）

**发送给 AI 的指令**：

```
创建 src/crawler/wechat_rss_crawler.py，实现：

1. check_service_health():
   - 请求 http://localhost:4000/health（或 /）
   - 返回 True/False

2. fetch_from_rss(rss_url):
   - 使用 feedparser 解析 RSS
   - 提取 title, link, published, summary
   - 返回 [{"title":..., "link":..., "publish_time":..., "summary":...}]

3. fetch_from_api(api_url, gzh_name):
   - 如果 we-mp-rss 提供 REST API，请求 JSON
   - 返回相同格式列表

4. fetch_articles_from_gzh(gzh_name):
   - 从 sources.yaml 读取该公众号的 rss_url 或 api_url
   - 调用对应方法获取数据
   - 若服务不可用，返回空列表并设置状态为 "manual_required"
   - 记录日志

5. 检测是否在 Actions 环境：
   - 若 os.environ.get('CI') == 'true'
   - 直接返回空列表，记录 "Skipping wechat RSS in Actions"
```

---

### Step 8：实现主模型故障转移客户端

**发送给 AI 的指令**：

```
创建 src/ai_engine/fault_tolerant_client.py，实现：
- 类 FaultTolerantClient
- 初始化时从 models.yaml 加载 main_models
- call(prompt, **kwargs) 方法：
  - 按顺序尝试每个模型（OpenAI 兼容接口）
  - 记录失败日志
  - 全部失败则抛出异常
- 支持传入 tools 参数
```

---

### Step 9：实现独立检查模块

**发送给 AI 的指令**：

```
创建 src/ai_engine/independent_checker.py，实现：
- 类 IndependentChecker
- 初始化时从 models.yaml 加载 checker
- check(report_content) -> dict
  - 校验三部分分类、链接有效性、增量逻辑
  - token 上限 500，超时 15s
  - 返回 {"passed": bool, "errors": [...]}
  - 失败返回 None
```

---

### Step 10：实现 Git 封装

**发送给 AI 的指令**：

```
创建 tui/local_git.py，实现：
- commit_only(message): git add -A && git commit -m "data: {message}"
- commit_and_push(message): commit_only + git push origin main
- pull_latest(): git pull --rebase origin main
- get_output_files(): 返回 output/ 下 .md 文件名列表
- check_conflicts(): 检测冲突文件列表
- 所有操作捕获异常并返回友好错误信息
```

---

### Step 11：实现搜索索引（SQLite）

**发送给 AI 的指令**：

```
创建 tui/search_handler.py，实现：
- 初始化 SQLite 数据库：search_index.db
- 创建表 articles (id, title, summary, link, source, publish_time, output_file)
- index_article(article): 插入或忽略重复 link
- search(query, source=None): 在 title/summary 中模糊匹配
- clear_stale(): 删除 output_file 已不存在的记录
- 搜索优先查索引，无结果则降级为全文扫描（记录日志）
```

---

### Step 12：实现总入口 main.py

**发送给 AI 的指令**：

```
创建 src/main.py，实现：

1. 加载 state.json（若无则初始化）
2. 加载 sources.yaml，遍历信源：
   - type="web": 调用 web_crawler.fetch_articles_from_list_page
   - type="wechat_rss": 
     - 若在 Actions 环境（CI=true），跳过并记录日志
     - 否则调用 wechat_rss_crawler.fetch_articles_from_gzh
3. 增量过滤：发布时间 > last_run 且 link not in processed_links
4. 加载 keywords.txt 和 user_profile.yaml
5. 对新文章分类：Part1（关键词命中）、Part2（AI 推荐）、Part3（其余）
6. 生成 Markdown 报告，写入 output/YYYY-MM-DD_HH-MM-SS.md
7. 调用独立检查器，追加检查结果
8. 更新 state.json
9. 更新搜索索引
10. 若在 Actions 环境，执行 commit_and_push("data: daily report")
```

---

### Step 13：实现工具层 tools.py

**发送给 AI 的指令**：

```
创建 tui/tools.py，实现：
- read_file(path): 仅允许 config/, output/, state.json
- write_file(path, content): 仅允许 config/，否则抛 PermissionError
- append_keyword(word): 追加到 config/keywords.txt
- update_profile(field, value): 修改 user_profile.yaml
- list_outputs(): 返回 output/ 下 .md 列表
- open_report(filename): 调用系统编辑器（open/xdg-open/start）
- search(query, source=None): 调用 search_handler.search()
- git_commit_only(message): 调用 local_git.commit_only()
- git_commit_push(message): 调用 local_git.commit_and_push()
- 所有写入操作仅修改内存，不自动提交
```

---

### Step 14：实现 Agent 引擎 agent.py

**发送给 AI 的指令**：

```
创建 tui/agent.py，实现：
- 加载 config/tui_agent.yaml
- prefer_function_calling=true: 携带 tools 参数
- 响应处理：tool_calls 执行工具，普通文本视为回复
- API 报错 tools 不支持：自动移除 tools 并重试一次
- prefer_function_calling=false: 使用自然语言提示
- JSON 纠错：json.loads → jsonrepair → 正则提取
- Few-shot 示例注入
- 常见意图预判
- load_history(): 加载最近历史
- save_history(): 序列化对话，实现轮转（保留最近 max_history_files 个）
- 对话轮数裁剪：保留最近 max_history_turns 轮
- 解析失败重试最多 2 次
```

---

### Step 15：实现 TUI 主界面 app.py

**发送给 AI 的指令**：

```
创建 tui/app.py，使用 Textual 框架：
- 布局：左侧 RichLog（对话），右侧 ListView（output/ 文件列表）
- 底部四个按钮：仅Commit、Commit & Push、仅Save、退出
- 仅Commit: git_commit_only("data: save session")
- Commit & Push: git_commit_push("data: save session")
- 仅Save: agent.save_history()
- 退出: agent.save_history() + Commit & Push
- 命令白名单：/load_history, /save, /quit
- 启动时检测 history/ 是否有历史文件，提示加载
- 提供「加载上次对话」按钮
- 右侧列表定时刷新（30秒）
- 窗口关闭事件自动执行「退出」
- 环境检查：.env 不存在则引导创建
- we-mp-rss 服务健康检查：不可用时显示警告
```

---

### Step 16：创建 GitHub Actions 工作流

**发送给 AI 的指令**：

```
创建 .github/workflows/daily.yml：
name: TJU Daily Bot
on:
  schedule:
    - cron: '0 8 * * *'
  workflow_dispatch:
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.10'
      - run: pip install -r requirements.txt
      - run: python src/main.py
        env:
          CI: true
          DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          QWEN_API_KEY: ${{ secrets.QWEN_API_KEY }}
      - name: Push output/ and state.json
        run: |
          git config user.name "TJU Bot"
          git config user.email "bot@tju.edu"
          git add output/ state.json
          git commit -m "data: daily report $(date +%Y-%m-%d)" || echo "No changes"
          git pull --rebase origin main || echo "Conflict, skip push"
          git push origin main || echo "Push failed, check manually"
```

---

### Step 17：集成测试与验收

**发送给 AI 的指令**：

```
1. 启动 we-mp-rss：docker-compose up -d
2. 验证服务健康：curl http://localhost:4000/health
3. 手动触发 Actions（workflow_dispatch）验证官网抓取
4. 运行 TUI：python -m tui.app
5. 测试：对话添加关键词、修改画像、列出/打开报告、搜索
6. 测试 we-mp-rss 服务不可用时的降级（docker-compose stop）
7. 测试 Git 各按钮功能
8. 测试历史加载和轮转
9. 检查增量逻辑（连续运行两次无重复）
10. 检查独立检查器
11. 验证 .gitignore 排除 history/ 和 .env
12. 所有验收标准逐条核对（见 PROJECT_PLAN.md 第9节）
```