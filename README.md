# Arc'teryx 补货监控

[![stock watch](https://github.com/kzhao5/arcteryx-stock-monitor/actions/workflows/stock-watch.yml/badge.svg)](https://github.com/kzhao5/arcteryx-stock-monitor/actions/workflows/stock-watch.yml)

盯着 Leutia Pant 白色（colour=17166）**00S** 这一个 SKU，有货就推送通知。

Arc'teryx 的商品页是前端渲染的，库存接口没有公开文档，所以脚本用无头浏览器
把尺码选择器真正点一遍，读它的状态——网站改版时这种方式比猜 API 稳。

## 判断依据

页面上售罄的尺码会被**画叉**，可买的是正常方块。判断以这个状态为准：

1. 找到 `00S` 那一块（页面上是单个方块，"00" 和 "S" 上下两行）
2. 它如果是 disabled / pointer-events:none / 内部 input 被禁用 → **无货**
3. 能点就点，并且**确认它真的变成选中态**
4. 选中之后再读购买按钮：`Add to cart` → 有货，`Notify Me` → 无货

第 3 步不能省。这个页面**没选任何尺码时 "Add to cart" 也是可点的黑色按钮**，
所以只看按钮文字会把"点击没生效"误判成有货。点了没选中就报 `UNKNOWN`，
宁可不报也不误报。

## 安装

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## 校准

尺码控件的形态已经对着真实页面确认过了：单个 `00S` 方块，不是「尺码 + 长度」
两个控件，所以默认参数就能用。

网站改版后如果结果变成 `UNKNOWN`，带截图跑一次看发生了什么：

```bash
python3 check_stock.py --headful --screenshot shot.png
```

结果是 `UNKNOWN` 时，日志里会自动列出页面上所有像尺码的元素及其状态
（tag、disabled、pointer-events、class），照着那个列表调就行。
`size '00S' not found on page` 说明标签写法变了，可以显式指定：

```bash
python3 check_stock.py --size 00 --length Short
```

## 日常运行

跑一次就退出（配合 cron 用）：

```bash
python3 check_stock.py
```

自己常驻循环，每 10 分钟一次：

```bash
python3 check_stock.py --interval 600
```

退出码：`0` 有货 / `1` 无货 / `2` 没判断出来（页面变了或网络问题）。

状态存在 `stock_state.json`，只有**从无货变有货**时才推送，不会反复轰炸。
想每次有货都推，加 `--notify-always`。

## 通知渠道

配了哪个就发哪个，可以同时配多个，都通过环境变量：

| 渠道 | 环境变量 |
| --- | --- |
| Bark（iOS） | `BARK_URL=https://api.day.app/你的KEY` |
| Telegram | `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` |
| Server 酱 | `SERVERCHAN_KEY` |
| 任意 webhook | `WEBHOOK_URL`（POST JSON） |
| 邮件 | `SMTP_HOST`、`SMTP_PORT`、`SMTP_USER`、`SMTP_PASS`、`SMTP_TO` |

一个都没配也能跑，只是把结果打在终端里。

## 定时

在自己的机器上用 **cron**（macOS / Linux，每 10 分钟）：

```cron
*/10 * * * * cd /path/to/arcteryx-stock-monitor && .venv/bin/python check_stock.py >> monitor.log 2>&1
```

注意 cron 不继承你 shell 里的环境变量，通知相关的变量要写在 crontab 顶部，
或者在命令前 `source .env`。

不想自己管机器就用 GitHub Actions，见下一节。

## 跑在哪里

先跑一次体检，它会把决定性的几项查清楚（只读，不装任何东西）：

```bash
bash preflight.sh
```

它检查四件事：这是不是共享集群、能不能连外网、chromium 能不能起来、有没有办法做定时。

### 推荐：GitHub Actions

这个仓库是 public 的，所以 Actions 分钟数免费不限量，`.github/workflows/stock-watch.yml`
已经在仓库里了。电脑关机也照跑，不用维护任何服务器。

两个必须知道的点：

1. **定时任务只在默认分支上生效。** 工作流已经在 `main` 上了，所以没问题；
   以后改它的时候记得改完要合回 `main` 才生效。
2. **仓库 60 天没有任何提交，GitHub 会自动停掉 schedule。** 到时候随便推一个
   提交就能恢复。

定时已经开好了（每 15 分钟）。你只需要到
Settings → Secrets and variables → Actions 里加通知用的 secret。
仓库是公开的，**不要**把 Bark key 之类直接写进 yml，一定走 secrets。

没配 secret 也能跑，只是结果只出现在 Actions 日志里，不会推到你手机。

### 学校 / 实验室服务器

技术上能跑，但通常不合适，`preflight.sh` 会告诉你卡在哪一条：

- **使用政策**：科研计算资源一般不允许拿来跑私人用途的长期进程，登录节点更是
  明确只给编译和提交作业用。跑之前先看你们的 acceptable use 条款。
- **出网**：计算节点常常没有直连外网，只有登录节点有，或者要走代理。
  preflight 里 arcteryx.com 那行会直接告诉你。
- **chromium 依赖**：`playwright install chromium` 装到 `~/.cache` 不需要 root，
  但缺系统库（libnss3、libatk 之类）就得管理员装。preflight 会用 `ldd`
  把缺的库列出来。
- **定时**：共享集群常常禁掉用户 crontab；SLURM 作业有时限，也不适合挂长任务。

如果只是一台自己有 root 的实验室机器，那没问题，按上面「安装」走，
再用 crontab 或 systemd --user 定时。

### 自己的笔记本

最省事，但只有开机联网时才在监控。macOS 用 launchd 或 cron，合盖就停。
适合先跑几天看看效果，不适合长期蹲守。

## 想要更轻量的版本

不想每次开浏览器的话，先把真实接口挖出来：

```bash
python3 check_stock.py --discover dump/
```

它会把页面加载时所有 JSON 响应存进 `dump/`，并指出哪些响应里出现了 `00S`。
找到那个接口后，用 `requests` 直接打它就行，几百毫秒一次。

## 注意

- 别把间隔调得太短。5–15 分钟足够了，一分钟一次只会更快被风控。
- 脚本只读页面，不会加购、不会下单。
- 补货高峰期热门尺码可能几分钟就没，通知到了要手动尽快去下单。
