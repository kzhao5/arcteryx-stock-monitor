# Arc'teryx 补货监控

[![stock watch](https://github.com/kzhao5/arcteryx-stock-monitor/actions/workflows/stock-watch.yml/badge.svg)](https://github.com/kzhao5/arcteryx-stock-monitor/actions/workflows/stock-watch.yml)

盯着 Leutia Pant 白色（colour=17166）**00S** 这一个 SKU，有货就推送通知。

Arc'teryx 的商品页是前端渲染的，库存接口没有公开文档，所以脚本用无头浏览器
把尺码选择器真正点一遍，读它的状态——网站改版时这种方式比猜 API 稳。

## 判断依据

页面上售罄的尺码会被**画叉**，可买的是正常方块。判断以这个状态为准：

1. 找到 `00S` 那一块（页面上是单个方块，"00" 和 "S" 上下两行）
2. 看它的 class 有没有 `no--stock` → 有就是**无货**
3. 没有就点它，并**确认真的变成选中态**
4. 再读购买按钮：`Add to cart` → 有货，`Notify Me` → 无货

第 2 步是主要依据，这是对着真实页面确认过的：售罄的尺码 class 里带
`no--stock`，可买的不带，和页面上画叉的那些完全一一对应。

有两个反直觉的地方，都踩过坑：

- **售罄的尺码照样点得动、点完照样进入选中态。** 它们不是 `disabled`，
  `pointer-events` 也是 `auto`。所以"能不能选中"不是库存信号，class 才是。
- **一个尺码都没选的时候，"Add to cart" 就已经是可点的黑色按钮。**
  所以不能只看按钮文字，否则点击没生效时会误报有货。点了没选中就报
  `UNKNOWN`，宁可不报也不误报。

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

GitHub Actions 上这个状态文件通过 Actions 缓存在多次运行之间传递，所以补货时
你只会收到一封邮件，而不是每 15 分钟一封。

## 通知渠道

配了哪个就发哪个，可以同时配多个，都通过环境变量：

| 渠道 | 环境变量 |
| --- | --- |
| Bark（iOS） | `BARK_URL=https://api.day.app/你的KEY` |
| Telegram | `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` |
| Server 酱 | `SERVERCHAN_KEY` |
| 任意 webhook | `WEBHOOK_URL`（POST JSON） |
| 邮件 | `SMTP_HOST`、`SMTP_PORT`、`SMTP_USER`、`SMTP_PASS`、`SMTP_TO`、`SMTP_FROM` |

一个都没配也能跑，只是把结果打在终端里。

### 用 Gmail 发邮件

`SMTP_TO` 支持多个收件人，用逗号或分号隔开，一封信同时发给所有人。

Gmail 不接受账号密码，必须用**应用专用密码**：

1. 发件的那个 Google 账号先开启两步验证
2. 到 https://myaccount.google.com/apppasswords 生成一个应用专用密码（16 位）
3. 按下面填 Secrets：

| Secret | 值 |
| --- | --- |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `465` |
| `SMTP_USER` | 发件的 Gmail 地址 |
| `SMTP_PASS` | 上一步生成的 16 位应用专用密码（**不是**登录密码） |
| `SMTP_TO` | 收件人，多个用逗号隔开 |
| `SMTP_FROM` | 可留空，默认等于 `SMTP_USER` |

**收件人地址也放 Secrets，不要写进代码**——仓库是公开的，邮箱写进源码会被
爬虫抓去发垃圾邮件。

端口用 465（SSL）或 587（STARTTLS）都行。如果服务器不支持加密而你又配了密码，
脚本会**拒绝发送**并报错，不会把密码明文送出去。

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

配好之后想确认通路是否真的通，不用等补货：在 Actions 页面点
`Run workflow`，把 **Also send a test notification** 勾上，会立刻发一封
标题带 `[测试]` 的邮件。命令行对应 `python check_stock.py --test-notify`。

想更进一步，确认"真有货时会不会发信"这条完整链路，在 **Also check this
size for real** 里填一个当前**确实有货**的尺码（比如 `4R`）。它会真的去查那个
尺码、真的走有货分支、真的发一封信给你。用的是独立的状态文件，不会干扰
00S 的正常监控。

日志里只会出现 `k***@gmail.com` 这种脱敏形式。GitHub 只遮蔽和 secret
完全一致的字符串，`SMTP_TO` 拆分后的单个地址不会被自动遮蔽，所以脚本
自己做了脱敏——否则收件人地址会明文出现在公开仓库的日志里。

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
