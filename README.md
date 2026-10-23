# 汽车之家口碑抓取器

从 `k.autohome.com.cn` 抓取车系口碑，输出 UTF-8 BOM 的 CSV。

基于原始 Selenium 单文件脚本重构。当前架构：**列表页用 Playwright 渲染，详情页走纯 HTTP**，
不需要 chromedriver，浏览器可随项目分发。

补齐了去重、断点续抓、指数退避重试、配置外置、解析层与抓取层分离。

> **两种用法**
> - **本机 / 局域网 / 公网隧道**（有实时进度条）：`分享给他人.bat`，详见第 3 节
> - **没人值守、你关机也能用**（零成本，走 GitHub Actions + Pages）：
>   - 线上数据页：https://kk1231191.github.io/autohome-koubei/
>   - 仓库：https://github.com/kk1231191/autohome-koubei
>   - 部署与限制说明见 [`DEPLOY_GITHUB.md`](DEPLOY_GITHUB.md)，
>     重新部署一条命令：`python deploy_github.py all`

---

## 1. 这套代码为什么这样写

### 1.1 站点已改版（2026-10 实测）

原脚本的失效点，按严重程度排列：

| 原实现 | 现状 | 处理 |
|---|---|---|
| `div.list_buy_target__rsfaE` 抓「购车目的」 | 整页 0 次命中，字段已被删除 | 保留空列，见 §6 |
| `.main-series` / `.main-spec` / `div.timeline-con` | 不存在 | 改用 `.subnav-title-name` / `.list_timeline` |
| `span:contains('首次发表')` | `:contains` 是 jQuery 扩展，非合法 CSS，Selenium 抛 `InvalidSelectorException` | 删除 |
| XPath `matches()` 函数 | XPath 2.0，ChromeDriver 仅支持 1.0，必抛异常 | 删除（改用 Playwright，不再受此限制） |
| `div.options.fn-hide` + 滚动/悬停/`remove('fn-hide')` | 全部无效，目标元素本来就可见 | 删除，直接读 DOM |
| 写死完整类名 `list_kb_nums__iH75q` | 类名后缀是 CSS Modules 构建哈希，发版即变 | 改为 `contains(@class,"list_kb_nums")` 前缀匹配 |
| `kb-item` / `kb-item-msg` / `athm-star` | 仍然有效 | 保留 |

### 1.2 架构：两个引擎，各管一段

| 环节 | 引擎 | 依据（实测） |
|---|---|---|
| 列表页 | Playwright + 浏览器渲染 | 评论列表由前端 XHR 拉取，页面 HTML 的 `__NEXT_DATA__.list` 是空数组；数据源 `koubeiipv6.app.autohome.com.cn` 的 robots 为 `Disallow: /`，按合规要求不直连 |
| 详情页 | `requests` 纯 HTTP | 直接 GET 返回的 HTML 里 `kb-item-msg=10 / star-num=7 / item-info=5`，与浏览器渲染结果**完全一致** —— 详情页是 SSR |

由此带来的两个收益：

1. 浏览器只承担「每页一次」的列表渲染，2000 条口碑的详情抓取从约 1.7 小时降到约 13 分钟
   （单篇详情从约 3s 降到约 0.4s）
2. 浏览器不再需要 chromedriver —— Playwright 走 CDP，不存在「驱动与浏览器版本必须匹配」

### 1.3 关于浏览器：为什么不再用 chromedriver

原方案的坑：Chrome 自动更新、chromedriver 不会，差一个大版本就报
`SessionNotCreatedException: This version of ChromeDriver only supports Chrome version N`。
本项目实测踩过：PATH 里是 148，浏览器已是 155。

现在改 Playwright 通过 CDP 直接驱动浏览器，**没有 driver 这一层**。浏览器按以下顺序查找：

1. `config.yaml` 的 `browser.executable_path`（显式指定）
2. `<项目>/browsers/` 下的 Chrome for Testing（由 `准备浏览器.bat` 下载，**随项目分发**）
3. 系统安装的 Chrome

第 2 条是「换机即用」的关键：把整个项目目录拷到另一台 Windows 机器，
不需要装 Chrome、不需要装 driver。

还有一个必须处理的坑：**本机 `HTTP_PROXY` 会让 chromium 报 `ERR_PROXY_CONNECTION_FAILED`**。
`new_context(proxy={"server":"direct://"})` **不能**覆盖这个行为（已实测），
必须给 Chromium 传启动参数 `--no-proxy-server`。已在 `pw_browser._ARGS` 里固定。

### 1.4 合规边界

| 目标 | robots.txt | 本程序的用法 |
|---|---|---|
| `k.autohome.com.cn` | 禁 `/ajax/`、`/frontapi/`、`/series/`、`/_next/`、`/data/`、`/form/`、`/proxy/`、`/Controls/`、`/front/comments/` 等 | 只访问 `/{车系ID}/`、`/{车系ID}/index_N.html` 与 `/detail/view_*.html`，这些路径**未被 Disallow** |
| `koubeiipv6.app.autohome.com.cn` | **`Disallow: /`**（全站禁爬） | 不使用。评论列表 JSON 接口在此域下 |

补充约束：
- 请求间隔默认随机 1–3 秒（`rate_limit`），单线程，无并发。
- 列表页渲染由真实浏览器完成，浏览器自身发起的资源请求属于正常客户端行为；
  本程序不额外直接请求任何被禁接口。

---

## 2. 安装

```bash
# 建议独立虚拟环境
python -m venv --system-site-packages venv
venv\Scripts\python.exe -m pip install -r requirements.txt

# 把浏览器下载到项目目录（约 150MB，走 npmmirror 镜像）
venv\Scripts\python.exe setup_browser.py
```

或双击 **`准备浏览器.bat`**。

这一步做完，`browsers/` 里就有一份完整的 Chrome for Testing，
项目自带浏览器，不再依赖系统安装的 Chrome。

**换机器部署只需三步**：装 Python → `pip install -r requirements.txt` →
把整个项目目录拷过去（含 `browsers/`）。不需要装 Chrome，不需要装 chromedriver。

实测环境：Windows 10/11，Python 3.10.5，playwright 1.5x，Chrome for Testing 148.0.7778.96，
lxml 6.0.2。回退路径实测：系统 Chrome 155.0.8059.80 也可直接驱动。

---

## 3. 运行

### 3.1 网页控制台（推荐）

```bash
venv\Scripts\python.exe web.py
```

或直接双击 `启动控制台.bat`（会启动服务并自动打开浏览器）。

打开 **http://127.0.0.1:8760**，在页面里输入**车型名称**（如 `零跑C10`、`RAV4`、`宝马3系`），
从联想下拉里选中车系，设置页数，点「开始抓取」。
页面提供：实时进度条、滚动日志、最近入库预览、停止按钮、CSV 下载。

**默认只监听本机。** 要分享给他人请看 §3.4。
为什么不能像普通网站那样部署到云服务器：抓取依赖**本机**的浏览器执行 JS，
且汽车之家对云机房 IP 有风控（403 / 验证码）。

服务端实现见 `web.py`，只用标准库（`http.server`），无额外依赖。
抓取以子进程方式运行，停止时整棵进程树回收，不会留下孤儿浏览器。

### 3.2 命令行

```bash
# 用 config.yaml 的配置
venv\Scripts\python.exe main.py

# 按车型名抓（推荐）
venv\Scripts\python.exe main.py --series 零跑C10 --pages 2
venv\Scripts\python.exe main.py --series RAV4 宝马3系 --pages 3

# 按车系ID 抓（同样支持）
venv\Scripts\python.exe main.py --series 770 7382 --pages 2

# 只查询车型名对应的车系ID，不抓取
venv\Scripts\python.exe main.py --search 宝马3系

# 只抓列表页，不进详情页（快约 10 倍，但无全文）
venv\Scripts\python.exe main.py --series 零跑C10 --no-detail --pages 20

# 显示浏览器窗口（首次调试或需要人工过验证时用）
venv\Scripts\python.exe main.py --show-browser
```

### 3.3 为什么要求输入车型名，而不是车系ID

口碑页 URL 就是 `k.autohome.com.cn/{车系ID}/`，ID 没有可推导的规律。

凭印象填 ID 极易踩坑：实测 ID=**276** 的页面返回 **HTTP 200**，
但 title 是「【怎么样】_怎么样_…」（车系名为空），整页没有任何口碑卡片。
旧版本会表现为「列表页元素等待超时，重试 3 次后放弃」，耗时 60 秒且报错指不出真正原因。

现在改为输入车型名，由 `autohome_spider/search.py` 调官网联想接口
（`sou.api.autohome.com.cn/sug/_suggest`）解析出 ID，并顺带用 title 反查校验：

- 车型名 → 候选列表（同名车系如「宝马3系 / 宝马3系(进口) / 宝马3系GT」由用户点选）
- 无效 ID → **1 秒内**明确报错「该ID不是有效车系」，不再白等 60 秒

合规性：`sou.api.autohome.com.cn` 无 robots.txt（404）；
`sou.autohome.com.cn` 的 robots 只限制 `/zonghe_op`、`/luntan` 等路径，不含 `/sug/`。

### 3.4 分享给他人（公网链接）

```bash
venv\Scripts\python.exe share.py
```

输出类似：

```
==============================================================================
  分享链接（直接发给对方，点开即用，无需单独报口令）

    https://mask-stayed-visitors-swim.trycloudflare.com/?k=pFaUGurFZnSZ

  仅口令:  pFaUGurFZnSZ
  本机:    http://127.0.0.1:8760
==============================================================================
```

把这个链接发给对方，他打开就是一个完整的控制台：输车型名、选页数、开始抓取、
看进度、下载 CSV。**任务实际在你这台电脑上执行**，对方只是远程操作界面。

**实现原理**：不是把程序部署到云，而是用 `cloudflared` 快速隧道把本机 8760
映射成公网 https 地址。依赖 `D:\WorkBuddy\tools\cloudflared\cf.exe`
（也可用环境变量 `CLOUDFLARED` 指定）。

**访问控制**（已内置）：
- 本机直连 `127.0.0.1:8760` **免口令**
- 公网访问必须带口令：链接里的 `?k=` 会自动种 Cookie 并跳到干净 URL
- 无口令访问 → 401 登录页（`web_login.html`）
- 口令持久化在 `output/.share_token`，12 字符随机串；可用环境变量
  `AUTOHOME_SHARE_TOKEN` 覆盖
- 加口令不是可选项：一旦暴露到公网，任何拿到链接的人都能触发抓取
  （消耗你的出口 IP，可能招致汽车之家封禁你这台机器）并下载全量数据

**必须知道的限制**：

| 限制 | 说明 |
|---|---|
| 电脑必须开着 | 关机 / 断网 / 关闭 `share.py` 窗口，链接立即失效 |
| 域名会变 | Cloudflare 快速隧道免账号，**每次重启域名都不同**，要重新发链接 |
| 无可用性保证 | 官方明确声明 quick Tunnel 不承诺 SLA；长期使用请用 Cloudflare 账号建命名隧道 |
| 单任务 | 同一时刻只能跑一个抓取任务，第二个人会收到「已有任务在运行」 |
| 数据共享 | 所有访问者看到同一份 CSV 与日志，`输出文件` 可各自改名区分 |
| 对方看不到你的浏览器 | 抓取在后台 headless 运行 |

**换到长期固定域名**（可选）：注册 Cloudflare 账号 → 绑定一个域名 →
`cloudflared tunnel create koubei` → `cloudflared tunnel route dns koubei k.你的域名` →
`cloudflared tunnel run koubei`。此时可把 `share.py` 里的 `--url` 换成 `run <名字>`。

### 3.5 零成本方案：你关机后别人也能用

上面 3.4 的隧道方案有个硬伤：**你的电脑关机，链接就断**。要彻底摆脱这一点，
把抓取搬到 **GitHub Actions**（免费、永久在线），数据存仓库，
用 **GitHub Pages**（免费）做展示页。0 元/月。

**完整步骤见 [DEPLOY_GITHUB.md](DEPLOY_GITHUB.md)**，核心只有三步：

```bash
git init && git add . && git commit -m "init"
git remote add origin https://github.com/<你的用户名>/autohome-koubei.git
git push -u origin main
```

然后在 GitHub 上：开启 Actions → Settings/Pages 选 `main` + `/docs` →
Actions 页面点「Run workflow」填车型名。

| 谁 | 怎么用 |
|---|---|
| 你自己 | Actions 页面填表单，1–2 分钟出结果 |
| 别人 | 开一个标题为 `[抓取] 车型名` 的 issue，跑完自动回评论 |
| 定时 | 配仓库变量 `CRAWL_SERIES`，每天自动增量抓取 |

取舍：**没有实时进度条**，点完要等 1–2 分钟；触发抓取需要 GitHub 账号
（只看数据不需要，Pages 链接谁都能打开）。

本地版和云端版共用 `autohome_spider/` 抓取核心，改一处两边都生效：

| | 本地版（web.py） | 云端版（Actions） |
|---|---|---|
| 实时进度 / 日志流 | 有 | 无 |
| 需要你的电脑开机 | 是 | 否 |
| 成本 | 0 | 0 |

---

## 4. 配置项（config.yaml）

| 键 | 默认 | 说明 |
|---|---|---|
| `crawl.series_ids` | `[770]` | 车系 ID 列表 |
| `crawl.max_pages` | `2` | 每车系最大页数，每页 10 条 |
| `crawl.start_page` | `1` | 起始页 |
| `crawl.skip_existing` | `true` | 命中已收录口碑ID 则跳过（断点续抓核心） |
| `crawl.full_text` | `true` | false 时只用列表页摘要 |
| `output.csv_name` | `autohome_koubei.csv` | 输出文件名 |
| `browser.driver_path` | 见配置 | 留空则自动匹配 |
| `browser.sanitize_proxy_env` | `true` | 见 §1.2 坑 2 |
| `rate_limit.min_delay/max_delay` | 1.0 / 3.0 | 请求随机间隔（秒） |
| `retry.times` | `3` | 单请求重试次数 |
| `retry.backoff_base` | `2.0` | 第 n 次重试等待 `base ** n` 秒 |

---

## 5. 输出字段（50 列）

| 分组 | 列 |
|---|---|
| 标识 | 口碑ID、车系ID、车系名称、车型版本、口碑标题、用户昵称 |
| 评分 | 综合口碑评分（0–5，两位小数）<br>空间/驾驶感受/续航/外观/内饰/性价比/智能化/油耗/配置 —— 各含 `评分` 与 `评论` 两列 |
| 时间 | 发表时间（YYYY-MM-DD）、购买时间（YYYY-MM） |
| 车况 | 购买地点、购车经销商、行驶里程、裸车购买价、百公里油耗、百公里电耗、夏季/春秋/冬季电耗、夏季/春秋/冬季续航 |
| 正文 | 最满意、最不满意 |
| 互动 | 观看数、评论数、点赞数（见 §6） |
| 其他 | 购车目的、车况信息_其他（JSON）、总口碑数量、在售口碑数量、评论链接、爬取时间 |

「车况信息_其他」存放未在标准列中出现的键值对（纯电/插混车型会多出
「官方续航」「充电时间」等），保证不丢数据又不让表宽失控。

---

## 6. 已知缺口（如实列出）

1. **购车目的 —— 拿不到。** 站点已删除该字段，列表页整页 0 次命中。
   列保留为空，保证历史表结构兼容。若后续页面恢复，`parser.parse_list_page`
   的卡片解析补一条选择器即可。

2. **点赞数 —— 拿不到。** 详情页 `span.option-goods` 在服务端 HTML 里恒为空字符串，
   由前端异步填充，数据源在 `koubeiipv6.app.autohome.com.cn`（robots 全站 Disallow）。
   解析不到时输出空串而不是 0，避免把「未知」误报成「零点赞」。
   **观看数、评论数正常可用**（实测老口碑观看数 1376）。

3. **分页上限 200 页（2000 条）。** 页面顶部写「18168 条口碑」，但
   `index_201.html` 返回空列表。2000 条以后的记录站点不开放。

4. **车型维度的差异。** 燃油车没有「续航」「智能化」维度，这两组列会是空值；
   纯电车没有「油耗」。属于数据本身的缺失，非解析失败。

5. **无登录态。** 当前匿名访问即可拿到全部正文。若站点后续对部分内容
   要求登录，需要在 `browser.py` 中加载持久化用户目录（`--user-data-dir`）。

---

## 7. 验证方式与已验证范围

### 已实际执行

| 项目 | 方法 | 结果 |
|---|---|---|
| 端到端抓取 | `main.py --pages 2` | 20 条全部成功，详情页失败 0，耗时 80s |
| 字段完整性 | 检查 CSV | 50 列；车系ID/口碑ID/标题/昵称/综合评分/发表时间/购买地点/行驶里程/裸车价 均 20/20 非空；口碑ID 无重复 |
| 数值合理性 | 统计分布 | 综合口碑评分 3.71~4.86，观看数 0~51682，评论数 0~83 |
| 分页机制 | 逐个 GET `index_1`~`index_201` | 每页 10 条；`index_201` 为 0 条 → 末页判定可靠 |
| 去重 | 同参数重跑 | `跳过(已存在)=10 详情成功=0`，耗时 41s→7s，无重复写入 |
| 失败隔离 | 对不存在的口碑ID 调 `_fetch_detail` | 返回 `None` 且不抛异常；随后对正常ID 仍返回完整数据 |
| 解析层单测 | `python -m unittest discover -s tests -v` | 19 项全通过，0.075s，无需浏览器与网络 |
| 交互数据 | 抓 2021 年老口碑 | 观看数 1376，确认非零值可正常解析 |
| Web 控制台端到端 | 经 `/api/start` 启动 1 页任务 | 10/10 完成，36s，exit=0，进度 0→100%，日志中文无乱码 |
| 下载接口 | `GET /api/download` | 200，37654 字节，带 UTF-8 BOM，11 行（表头+10） |
| 车型名解析 | `main.py --search 宝马3系` | 返回 66 / 317(进口) / 2963(GT) 三个候选，价格正确 |
| 无效ID 拦截 | `main.py --series 276` | **1 秒内**报错「该ID不是有效车系」，不再白等 60 秒 |
| 车型名端到端 | Web 传 `series=零跑C10` | 解析为 7382 → 10/10 完成，39s，数据确为零跑C10 口碑 |
| 公网鉴权-未登录 | 访问隧道域名 `/` | **401** + 登录表单（不含任何数据） |
| 公网鉴权-口令登录 | `/?k=<token>` | 302 → `Set-Cookie: au_share=...; HttpOnly; SameSite=Lax` |
| 公网鉴权-已登录 | 带 Cookie 访问 `/` | 200，20297 字节，是控制台页面 |
| **公网端到端抓取** | 经隧道启动 `series=零跑C10` | 10/10 完成，36s，exit=0；公网下载 CSV 200 / 28275 字节 / 含表头 |
| **详情页是 SSR** | `requests` 直取详情页 vs 浏览器渲染 | HTML 里 kb-item-msg=10 / star-num=7 / item-info=5，两者一致 → 详情页不需要浏览器 |
| Playwright 驱动系统 Chrome | `launch(executable_path=系统chrome)` | 155.0.8059.80 正常，10 张卡片，161584 字节，**无需 chromedriver** |
| 新架构端到端 | `main.py --series 零跑C10 --pages 1` | 10/10 成功，**31s**（旧 Selenium 版 36–39s），HTTP 详情成功 10 / 失败 0 |
| 内置浏览器生效 | `prepare_browser` 下载 Chrome for Testing 148.0.7778.96 后跑 CLI | `browsers/chrome-win64/chrome.exe` 被选中，27s / 10 条，未触碰系统 Chrome |
| 内置浏览器 + Web | 同上环境走 `/api/start` | 29s / 10 条 / exit=0 |
| **最终验收（公网全链路）** | 隧道 → `/?k=` 换 Cookie → `POST /api/start` → 轮询 → `/api/download` | 302 → 200 → 10/10 用 **20s** 完成 exit=0 → 下载 28276 字节；CSV 50 列、口碑ID 唯一 |

### 自检三问

- **去重是否生效？** 生效。以 `口碑ID`（详情页 URL 中的 `view_xxx`）为唯一键，
  启动时读全量 CSV 建立已抓集合。
- **断点续抓是否可用？** 可用，且刻意**不用页级断点**：口碑列表按时间倒序，
  新口碑插入首页会让 `index_5.html` 内容整体后移，页级断点必然漏数据。
  本实现每次从头扫描、按 ID 跳过，代价是列表页重复渲染（每页 1 次请求，很便宜）。
- **单条失败是否中断整体？** 不中断。详情页重试 3 次仍失败时，用列表页的
  满意/不满意摘要兜底写入，并把该条记入 `output/state/state_<车系ID>.json`
  的 `failures` 数组，便于事后补抓。

### 未覆盖

- 未做 200 页全量压测（按当前速率估算约需 2.5 小时）。
- 未验证滑块/验证码出现时的表现（本次测试全程未触发）。
- 未验证纯电车型的「电耗/续航」字段实际取值（缺纯电车系样本）。

---

## 8. 项目结构

```
autohome_koubei/
├── 启动控制台.bat            # 双击启动本机控制台
├── 分享给他人.bat            # 双击启动控制台 + 公网隧道
├── 准备浏览器.bat            # 双击把 Chrome 下载到项目目录
├── DEPLOY_GITHUB.md         # 零成本方案：部署到 GitHub Actions + Pages
├── .github/workflows/
│   └── crawl.yml            # 云端抓取流水线（表单 / issue / 定时三种触发）
├── docs/                    # GitHub Pages 展示页（发布目录）
│   ├── index.html           # 数据浏览页（搜索/筛选/排序/详情弹窗）
│   └── data/
│       ├── koubei.csv       # 展示用数据副本（每次抓取自动同步）
│       └── meta.json        # 最近一次抓取的元信息
├── web.py                   # 本地 Web 控制台（标准库实现 + 口令鉴权）
├── web_ui.html              # 控制台界面
├── web_login.html           # 公网访问的口令登录页
├── share.py                 # 一键开公网隧道并打印分享链接
├── setup_browser.py         # 下载 Chrome for Testing 到 browsers/（支持 win/linux/mac）
├── main.py                  # CLI 入口
├── config.yaml              # 全部可调参数
├── requirements.txt
├── browsers/                # 项目自带的浏览器（setup_browser.py 生成, 约 150MB, 不进 git）
│   └── chrome-win64/chrome.exe
├── autohome_spider/
│   ├── config.py            # dataclass 配置 + CSV 列定义
│   ├── search.py            # 车型名 -> 车系ID 解析 + ID 有效性校验
│   ├── pw_browser.py        # Playwright 渲染层（列表页）+ 浏览器定位
│   ├── fetcher.py           # 纯 HTTP 抓取层（详情页）
│   ├── parser.py            # HTML -> dict（纯函数，可离线单测）
│   ├── storage.py           # CSV 增量追加 + 去重 + 断点状态
│   ├── crawler.py           # 分页/详情/限速/重试编排
│   └── browser.py           # 【legacy】旧 Selenium 驱动管理，当前链路不再使用
├── tests/
│   ├── test_parser.py       # 19 项离线回归测试
│   └── fixtures/            # 列表页 / 详情页 HTML 快照
├── output/
│   ├── autohome_koubei.csv
│   ├── .share_token         # 公网访问口令（勿外泄给无关的人）
│   └── state/state_770.json # 进度与失败清单
└── logs/autohome_spider.log
```

解析层（`parser.py`）刻意不 import selenium，因此可以直接对离线 HTML 跑单测：

```bash
python -m unittest discover -s tests -v
```

站点改版时，把新的页面快照覆盖到 `tests/fixtures/`，跑一遍测试，
失败的断言会直接指出哪个字段的 XPath 失效。改动通常只需落在 `parser.py`，
不必碰抓取逻辑。
