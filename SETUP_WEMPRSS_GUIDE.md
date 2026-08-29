# we-mp-rss 本地初始化操作指引

本指引引导你在**本地**完成 we-mp-rss（微信公众号 RSS 服务）的**一次性初始化**：
装 Docker → 启动容器 → 扫码授权微信读书 → 添加 110 个公众号订阅 → 导出数据作为云端种子。

> ⚠️ **为什么必须本地初始化**：we-mp-rss 首次需要**扫码绑定你的微信读书账号**（浏览器操作）并**添加订阅**。
> 这些涉及你的账号，只能在本地做一次。初始化后的数据要导出，作为 GitHub Actions 首次 cache 的种子，
> 这样云端每次运行才有订阅配置和登录态。

---

## 前置：本机需要 Docker

当前检测到你的电脑**没有 Docker**。请先安装：

- **Windows**：安装 **Docker Desktop**（https://www.docker.com/products/docker-desktop/）
  - 安装后启动 Docker Desktop，等待右下角图标变绿
  - 验证：新开终端执行 `docker --version` 能看到版本号

---

## Step 1：启动 we-mp-rss 容器

在一个终端执行（按需用国内镜像加速）：

```bash
# 创建数据目录
mkdir -p we-mp-rss-data

# 启动容器（端口 8001，挂载数据卷）
docker run -d --name we-mp-rss -p 8001:8001 -v "$(pwd)/we-mp-rss-data:/app/data" rachelos/we-mp-rss:latest
```

> 若拉取慢，可换用国内加速镜像：`docker.1ms.run/rachelos/we-mp-rss:latest`

验证容器运行：

```bash
docker ps            # 应看到 we-mp-rss 容器 Up
docker logs we-mp-rss  # 查看启动日志,确认无报错
```

---

## Step 2：扫码授权微信读书账号

1. 浏览器打开 **http://localhost:8001**
2. 首次进入会要求**登录**（默认账号 `admin` / `admin@123`，来自容器 env）
3. 进入后按界面提示：**扫码授权微信读书**（用微信扫页面二维码，绑定你的微信读书账号）
   - 授权成功后，we-mp-rss 才能抓取公众号文章
   - 这一步是「微信号」或「微信读书」的绑定，凭此抓取订阅的公众号

> 💡 如果页面是英文，右上角可切换中文。授权成功通常会看到「授权成功」提示。

---

## Step 3：添加 110 个公众号订阅

有三种方式，按效率选择：

### 方式 A：网页逐个添加（最直观，但 110 个很累）
- 在管理页「公众号/订阅」处，用**搜索**找到公众号 → 添加
- 优点：所见即所得 | 缺点：110 个要一个一个点，耗时约 20-40 分钟

### 方式 B：API 批量添加（推荐，一次加完）
先用网页登录拿 token，再调 API 批量加。

1. 浏览器 F12 → Console 获取登录后的 **token / Cookie**（用于认证）
2. 用仓库里的 `tools/query_biz.py` 已有 110 个公众号的 **fakeid**
3. 逐个 `POST {we-mp-rss}/mps` 添加（body 含公众号名称 + fakeid/bookId）
   - 具体字段以网页「添加订阅」的网络请求为准，可在 F12 抓包查看

> 也可以先用方式 A 加几个测试通，再用方式 B 补全 —— 不必纯用一种。

> 💡 **提示**：如果你觉得 110 个太多，可以**只先加最重要的几十个**（如各学院/官微/教务处），
> 后续在云端也能随时补充。

---

## Step 4：验证抓取成功

添加订阅后，手动触发一次更新并看 RSS：

```bash
# 触发更新并拉取聚合 RSS（网页也会自动定时更新）
curl "http://localhost:8001/rss/fresh" -o fresh.xml
head -50 fresh.xml
```

- 能看到 `<?xml ...<rss>` 且里面有你添加的公众号的文章条目 → 成功
- 若为空/报错 → 检查扫码授权是否有效、订阅是否添加成功

---

## Step 5：导出数据（云端种子）

把包含订阅配置 + 登录态 + 已积累文章的数据目录打包导出：

```bash
# 停容器，确保数据写完
docker stop we-mp-rss

# 打包数据卷
tar -czf we-mp-rss-data.tar.gz we-mp-rss-data/
```

**这个 `we-mp-rss-data.tar.gz` 就是云端首次运行的种子数据**，保留好。

---

## Step 6（云端接入时完成）：把种子注入 GitHub Actions

当你准备让 GitHub Actions 跑起来时，把种子数据放入首次 cache：

- 方法（推荐）：本地用 `gh` CLI 或 GitHub 网页手动上传一次 seed 到 Actions cache：
  - 参考（手动创建 cache）：https://github.com/actions/gh-actions-cache
  - 或在 Actions 首次 job 里临时加一步解包种子到 `/tmp/we-mp-rss-data`（之后删除该步骤）

> ⚠️ 种子含你的登录态/会话，**不要提交进 git 仓库**。用 cache 或私有位置存。

---

## 完成标准

- [ ] `docker --version` 有版本
- [ ] 容器 `running`
- [ ] 网页能扫码授权
- [ ] 添加了 ≥1 个公众号订阅且 `curl /rss/fresh` 能拉到文章
- [ ] `we-mp-rss-data.tar.gz` 导出成功

---

## 常见问题

| 问题 | 处理 |
|---|---|
| 端口 8001 被占用 | 换 `-p 8002:8001`,改访问 localhost:8002 |
| 镜像拉取慢 | 用 `docker.1ms.run/rachelos/we-mp-rss:latest` |
| 扫码后仍抓不到 | 重新扫码;确认添加了订阅;`docker logs we-mp-rss` 看错误 |
| 登录密码 | 默认 admin / admin@123（容器 env 里可改） |
