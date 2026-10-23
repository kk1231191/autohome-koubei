# 零成本部署：让别人在你关机时也能用

## 结论

不需要买云主机。把抓取搬到 **GitHub Actions**（免费、永久在线），数据存在仓库里，
用 **GitHub Pages**（免费静态托管）做展示页。整条链路 0 元/月，你关机、断网、换电脑都不影响。

代价是**没有实时的网页控制台**：触发一次抓取要等 1–2 分钟出结果，而不是点了按钮就转圈等。
对于「抓一批口碑、拿一份 CSV」这类需求，这个延迟完全可接受。

```
┌─────────────────┐   点按钮/开 issue     ┌──────────────────┐
│  使用者          │ ────────────────────> │ GitHub Actions   │
│  (你 / 别人)     │                       │ (runner, 抓取)   │
└─────────────────┘                       └────────┬─────────┘
                                                   │ git commit
                                                   v
┌─────────────────┐     fetch CSV         ┌──────────────────┐
│ GitHub Pages    │ <──────────────────── │ 仓库里的 CSV      │
│ (公开展示页)     │                       │ docs/data/*.csv  │
└─────────────────┘                       └──────────────────┘
```

---

## 一、准备

- 一个 GitHub 账号（免费）
- 本机装了 Git（`git --version` 能打印版本即可）
- 项目目录：`D:\WorkBuddy\outputs\autohome_koubei`

**关于 public / private 的选择**，这个决定影响成本和使用方式：

| | Public 仓库 | Private 仓库 |
|---|---|---|
| Actions 额度 | **无限**（标准 runner） | 2000 分钟/月 |
| GitHub Pages | 免费可用 | 需要 Pro 账号 |
| 数据是否公开 | 是，任何人能看到 CSV | 否 |
| 别人能否申请抓取 | 能（开 issue） | 不能（无权限） |

**推荐 public**：代码本身没有敏感信息，数据来自公开页面，而且额度无限。如果数据不能公开，
看本文末尾的「方案 B：私有仓库」。

---

## 二、五步部署

### 第 1 步：在 GitHub 建一个空仓库

打开 https://github.com/new

- Repository name：随意，比如 `autohome-koubei`
- 选 **Public**
- **不要**勾选 "Add a README file"（否则推送时会冲突）

### 第 2 步：把项目推上去

在项目目录打开 Git Bash：

```bash
cd /d/WorkBuddy/outputs/autohome_koubei

git init
git add .
git commit -m "init: 汽车之家口碑抓取器"

# 把 YOUR_NAME 换成你的 GitHub 用户名
git remote add origin https://github.com/YOUR_NAME/autohome-koubei.git
git branch -M main
git push -u origin main
```

> `browsers/` 已被 `.gitignore` 排除，不会推那 400MB 的浏览器。
> 实际推送体积约 300KB（主要是那个 196KB 的 CSV）。

### 第 3 步：开启 Actions

推上去后打开仓库页面 → **Actions** 标签页 → 如果弹出提示，点
「I understand my workflows, go ahead and enable them」。

左侧应该能看到「抓取汽车之家口碑」。

### 第 4 步：开启 Pages

**Settings** → 左侧 **Pages** → Source 选 **Deploy from a branch** →
Branch 选 **main**，目录选 **/docs** → **Save**。

等 1–2 分钟，页面顶部会显示你的网址：

```
https://YOUR_NAME.github.io/autohome-koubei/
```

这个链接可以发给任何人，不需要登录、不需要你的电脑开机。

### 第 5 步：跑第一次抓取

**Actions** → 左侧「抓取汽车之家口碑」→ 右侧 **Run workflow** 按钮：

| 输入框 | 填什么 |
|---|---|
| 车型名 | `零跑C10`（多个用逗号：`零跑C10,小米SU7`） |
| 页数 | `3`（每页 10 条，3 页 = 30 条，约 2 分钟） |
| 是否进详情页 | `true`（要完整字段就选 true） |

点绿色 **Run workflow**，等 1–2 分钟。跑完后：

- Actions 页面能看到绿色对勾和「抓取结果」摘要表
- 仓库里 `docs/data/koubei.csv` 会自动更新
- Pages 页面刷新一下就有数据了

---

## 三、三种使用方式

### 1. 你自己用：填表单

Actions 页面 → Run workflow → 填车型名。参数支持：

- **车型名**：`零跑C10`、`宝马3系`、`RAV4`（推荐，代码会自动查车系 ID）
- **多车型**：`零跑C10,小米SU7`（中英文逗号、顿号都行）
- **页数**：1–200，每页 10 条
- **详情页**：true = 50 个字段完整；false = 只抓列表摘要，快约 10 倍，但字段缺很多

### 2. 别人用：开一个 issue

任何有 GitHub 账号的人，在你的仓库点 **Issues** → **New issue**，
标题写成下面任意一种：

```
[抓取] 零跑C10
抓取 小米SU7
申请抓取：宝马3系、RAV4
```

提交后自动触发抓取，跑完会在 issue 里回一条评论，告诉他数据条数和展示页链接。
标题里没有「抓取 / 采集 / 申请」这三个词的 issue 不会触发（避免无关 issue 白占额度）。

展示页右上角有个「申请抓取新车型」按钮，点进去就是预填好的 issue 模板。

### 3. 定时增量：每天自动补新数据

**Settings** → **Secrets and variables** → **Actions** → **Variables** 标签 →
**New repository variable**：

| Name | Value | 说明 |
|---|---|---|
| `CRAWL_SERIES` | `零跑C10,小米SU7` | 关注的车型，逗号分隔 |
| `CRAWL_PAGES` | `2` | 每次抓几页（可选，默认 2） |

配好后每天北京时间 10:17 自动跑一次。

**为什么是增量而不是全量**：口碑列表按发表时间倒序，每次都从第 1 页抓，
已收录的口碑 ID 会被自动跳过（`skip_existing`），只有新口碑会追加进 CSV。
所以每天抓 2 页就够了，跑得也快。

> 没配 `CRAWL_SERIES` 时定时任务会自动跳过，不会白跑。

---

## 四、成本

| 项目 | 用量 | 费用 |
|---|---|---|
| GitHub Actions（public 仓库） | 无限 | **0** |
| GitHub Pages | 每月 100GB 流量、1GB 存储 | **0** |
| 数据存储 | 仓库里 CSV，1000 条约 3MB | **0** |
| 电费 / 服务器 | 无（不用你的机器） | **0** |

单次抓取耗时参考：装依赖 40s + 浏览器缓存命中就绪 5s + 抓 3 页约 60s ≈ **2 分钟**。
2000 分钟/月的 private 额度够跑约 700 次。

---

## 五、必须知道的限制

### 1. GitHub runner 的 IP 是数据中心 IP

这是**唯一没实测过的风险**。汽车之家对机房 IP 可能有额外限流。
好消息是本程序的请求速率极低——1–3 秒随机间隔、单线程串行、每页只发 1 次请求，
抓 30 条要 60 秒，比正常人刷页面还慢。

**第一次跑就能知道答案**：如果 `执行抓取` 步骤报「列表页拿不到数据」或返回 403，
把日志发我，我改代理方案。如果成功，这个风险就排除了。

### 2. 单次任务最长 40 分钟

`timeout-minutes: 40`。按实测速率（每页约 20 秒），40 分钟约能跑 100 页 = 1000 条。
要抓满 200 页（2000 条）需要调大超时，或者分两次抓不同页段。

### 3. 没有实时进度条

点完按钮只能等。想看进度就点进那次运行，日志是实时刷新的。

### 4. 别人只能申请，不能立即拿到

issue 触发后要等 1–2 分钟。这是 Actions 的排队 + 启动开销，绕不过去。

### 5. 公开仓库 = 数据公开

任何人访问你的 Pages 链接都能看到全部口碑数据。数据本身来自公开页面，
但如果你要给客户交付，应该换成私有仓库（见方案 B）。

---

## 六、方案 B：私有仓库（数据不公开）

如果你不想公开数据：

1. 建仓库时选 **Private**
2. 第 4 步的 Pages **跳过**（免费账号的 private 仓库不支持 Pages）
3. 拿数据的方式：
   - Actions 跑完后，页面底部有 **Artifacts** → 下载 `crawl-<id>.zip`（含 CSV，保留 30 天）
   - 或者本地 `git clone` 后直接读 `output/autohome_koubei.csv`
4. 别人用不了（没有仓库权限开不了 issue）

额度：2000 分钟/月，一次抓取约 2 分钟，约够 1000 次/月。

---

## 七、排错

| 现象 | 原因 | 处理 |
|---|---|---|
| Actions 标签页没有 workflow | 仓库没开启 Actions | Actions 页面点「I understand my workflows」 |
| 报错 `没有指定车型` | 表单没填 / issue 标题没带关键词 | 检查输入 |
| 报错 `未找到与「XXX」匹配的车系` | 车型名写错 | 在本地跑 `python main.py --search 车型名` 查正确写法 |
| `执行抓取` 步骤失败，日志显示超时 | 目标站点限流，或车型 ID 无效 | 把日志发我；先试 `--no-detail` / 减少页数 |
| Pages 显示 404 | Pages 没配 / 分支目录选错 | Settings → Pages 确认是 `main` + `/docs` |
| Pages 有页面但没数据 | 还没跑过抓取，或 CSV 没同步 | 先跑一次 workflow；看 `docs/data/` 下有没有 `koubei.csv` |
| `git push` 报 rejected | 远端有你也有的提交（比如 Actions 刚提交过数据） | `git pull --rebase` 再 push |
| 浏览器下载失败但任务成功 | 退回了 runner 自带 Chrome | 正常，不影响；日志里会打印「系统安装」 |

---

## 八、和本地版的关系

这个仓库里其实有两套入口，互不干扰：

| | 本地版 | 云端版（本文） |
|---|---|---|
| 入口 | `web.py` + `share.py` | GitHub Actions |
| 网页 | 功能齐全，有实时进度条、日志流 | 只读展示 + 申请抓取 |
| 需要你的电脑开机 | 是 | 否 |
| 成本 | 0（cloudflared 免费隧道） | 0 |
| 适合 | 自己边调边抓、要看实时日志 | 长期挂着、给别人用 |

抓取核心（`autohome_spider/`）完全共用，改一处两边都生效。
