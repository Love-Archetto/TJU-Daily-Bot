# 北洋维基查询 Skill（tju-wiki）

天津大学（北洋）校园信息查询技能。数据来自 **北洋维基**（https://wiki.tjubot.cn/），覆盖招生、报到、校历、课程、宿舍、图书馆、食堂、军训、奖学金、转专业、考研保研、出国交流等词条。

- 作者维护者：WorkBuddy 生成，欢迎贡献
- 版本：v1.0.0

---

## 一、安装包内容

```
tju-wiki-skill/
├── SKILL.md          # skill 定义（frontmatter + 使用说明）
├── tju_wiki.py       # 查询工具（Python 3，依赖 requests + beautifulsoup4）
├── install.sh        # Linux / macOS 一键安装脚本
├── install.ps1       # Windows PowerShell 一键安装脚本
└── README.md         # 本文档
```

## 二、快速安装

### Linux / macOS

```bash
unzip tju-wiki-skill-v1.0.0.zip
cd tju-wiki-skill
bash install.sh
```

指定自定义目标目录：

```bash
bash install.sh ~/my-agents/skills/tju-wiki
```

### Windows（PowerShell）

```powershell
Expand-Archive tju-wiki-skill-v1.0.0.zip
cd tju-wiki-skill
powershell -ExecutionPolicy Bypass -File install.ps1
```

自定义目录：

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 -Target "$env:USERPROFILE\my-agents\skills\tju-wiki"
```

### 手动安装（任意 agent）

把 `SKILL.md` 和 `tju_wiki.py` 放入 agent 的 skills 目录下的 `tju-wiki/` 子目录即可。常见位置：

| 平台 | skills 目录 |
|---|---|
| CodeBuddy / WorkBuddy | `~/.codebuddy/skills/` 或 `%USERPROFILE%\.codebuddy\skills\` |
| Claude Desktop (Claude Code) | `~/.claude/skills/` |
| 其他 agent | 按各自 skills 目录约定放置 |

依赖：`python3` + `pip install requests beautifulsoup4`（安装脚本会自动处理）。

## 三、验证安装

```bash
python3 <skills目录>/tju-wiki/tju_wiki.py search 校历
# Windows: python <skills目录>\tju-wiki\tju_wiki.py search 校历
```

看到词条列表即安装成功。安装脚本还会自动跑一次网络验证。

## 四、在 agent 中使用

安装后，直接向 agent 提问即可自动触发，例如：

- 「天大的校历是怎么安排的？」
- 「新生报到需要带哪些材料？」
- 「图书馆座位怎么预约？」
- 「转专业有什么政策？」
- 「北洋园校区有哪些食堂？」

也可手动调用命令（供 agent 或命令行使用）：

| 命令 | 作用 |
|---|---|
| `tju_wiki.py search <关键词> [页码]` | 全文搜索词条 |
| `tju_wiki.py cat <分类名或slug> [页码]` | 浏览分类词条列表 |
| `tju_wiki.py cats` | 列出全部分类 |
| `tju_wiki.py read <词条URL或slug> [--no-images]` | 阅读词条全文（表格转文本、图片转链接） |
| `tju_wiki.py latest [数量]` | 最近更新词条 |
| `tju_wiki.py home` | 首页推荐词条 |

## 五、技术说明

- 站点为 Typecho 系统，**会拦截 curl / wget**（TLS 指纹检测，返回空响应），必须使用 Python `requests` 并携带浏览器 UA——本工具已封装。
- 词条按学年/年份区分版本，回答时效性问题时应优先最新版本。
- 站点可能限流，连续请求请稍作间隔。

## 六、常见问题

**Q: 安装后 agent 不自动调用？**
检查 SKILL.md 是否放在 skills 目录的 `tju-wiki/` 子目录下（需有子目录，不能直接平铺在 skills 根目录）。

**Q: 网络验证失败？**
部分环境无法直连外网；确认 `curl -I https://wiki.tjubot.cn` 有响应即可（注意 curl 可能被站点拦截，请以 Python 请求为准）。

**Q: 想给其他 agent 平台用？**
SKILL.md 的 frontmatter 是通用格式，多数 agent 支持；个别平台如不符合可自行改写 frontmatter 字段名。
