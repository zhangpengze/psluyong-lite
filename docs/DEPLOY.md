# 部署手册

## 一、服务端（绿联 NAS · UGOS Pro Docker）

> 流程按绿联官方「Docker 应用 → 项目」Compose 部署方式编写
> （官方教程：support.ugnas.com），与 UGOS Pro 社区成功案例一致。
> 其他 NAS 思路相同，只是建项目的入口不同。

### 0. 先配镜像加速（否则首次拉镜像可能卡住）

【Docker】应用 → 【镜像】页 → 右上角【设置】→ 找到 DockerHub 项 →
「加速器配置」，添加国内源并保存，然后重启 Docker 应用（或重启 NAS）生效：

    https://docker.1ms.run
    https://docker.xuanyuan.me
    https://dockerproxy.com

配一个就够，多配几个会自动 fallback。配完随便拉个 nginx 验证速度。

### 1. 准备目录

「文件管理」→ 共享文件夹 → 在 `docker` 下新建 `psluyong-lite`
（完整路径 `/volume1/docker/psluyong-lite`）。上传这些内容：

    Dockerfile  docker-compose.yml  requirements.txt  server/  bot/  core/

只要源码，**不要**把开发机上的 `server/data` 拷进去（见第 4 节）。
然后准备 `.env`：

```bash
cd /volume1/docker/psluyong-lite
cp .env.example .env
```

编辑 `.env`，**必须**填这三项：

```bash
python -c "import secrets;print(secrets.token_urlsafe(48))"   # SECRET_KEY
python -c "import secrets;print(secrets.token_urlsafe(32))"   # API_KEY
# ADMIN_PASSWORD 自己起一个强密码
```

> 绿联文件管理默认隐藏以 `.` 开头的文件，`cp` 之后看不到 `.env` 是正常的：
> 文件管理 → 设置 → 常规设置 → 取消勾选「隐藏以 . 开头的文件」。

### 2. 启动（项目 Compose，官方推荐方式）

【Docker】应用 → 【项目】→ 【创建】→ 项目目录选
`/volume1/docker/psluyong-lite` → 粘贴 `docker-compose.yml` 的内容
（或从本机导入该文件）→「立即部署」，系统会自动构建并启动容器。

习惯命令行的可以开 SSH（控制面板 → 终端 → SSH）后操作：

```bash
cd /volume1/docker/psluyong-lite
docker compose up -d --build
docker compose logs -f server
```

看到 `服务启动 http://0.0.0.0:8080` 即成功。浏览器打开 `http://NAS的IP:8080`
（IP 在 控制面板 → 网络设置 → 网络连接 查看）。

### 3. 资源与备份

- 容器限制：2 核 / 1G 内存，实测空闲约 80MB。
- 数据全部在 `./server/data`：
  - `app.db` — SQLite 数据库（运行时旁边还会有 `app.db-wal` / `app.db-shm`，
    这是 WAL 模式的正常产物，**打包备份时一并带上或先停容器**）
  - `images/年/月/日/` — 成品图与缩略图
  - `backups/` — 每天 03:30 自动热备（gzip，保留最近 10 份）
  - `reports/` — 每日日报 Markdown
- 备份验证（在 NAS 上执行，能列出文件说明 Linux 文件锁正常）：

```bash
docker exec psluyong-lite python -m server.backup --list
```

- 兜底：直接打包 `server/data` 目录即可，无需导库。

#### 3.1 图片必须有第二份（重要）

数据库每天有热备，但成品图只在 `server/data/images` 这一块盘上。盘坏、误删目录、
误格式化，图就全没了——而图才是员工一天的工作成果，库里的只是一串路径。

做法：在 NAS 上建一个备份共享目录（最好在另一块盘），挂进容器，然后配两项：

```yaml
# docker-compose.yml
volumes:
  - ./data:/app/data
  - /volume2/backup/psluyong-images:/app/mirror   # 第二块盘
environment:
  IMAGES_MIRROR_DIR: /app/mirror
```

```bash
# .env
IMAGES_MIRROR_DIR=/app/mirror
DISK_ALERT_GB=20          # 图片占用超过这个值推群提醒
```

每天 03:30 的夜间任务会做增量镜像：只拷新增/变化的文件（按大小比对，不靠时间戳），
10 个人一天几十张图是秒级完成。手动执行：

```bash
docker exec psluyong-lite python -m server.backup --mirror-images
```

未配置时启动日志会打一条 warning——不是错误，但意味着"盘坏即全丢"。

输出会列出缺失记录（record id / 日期 / 员工 / 期望路径）和前 50 个孤儿文件；
缺失原图时退出码为 1，方便挂到外部监控上。

#### 3.2 容器日志限流

compose 里已经写好 `logging: max-size 10m / max-file 3`。缺了这段，json-file 驱动
会无限增长，NAS 系统盘被写满会连带其它容器一起挂，而且没有任何提示。

#### 3.3 运维告警

以下四类问题原来只写日志（日志一轮转就消失），现在会推到日报群：

| 场景 | 触发 |
|---|---|
| 数据库备份失败 | 夜间任务异常 |
| 图片镜像失败 | 单张复制失败或目录不可写 |
| 图片占用超阈值 | 超过 `DISK_ALERT_GB` |
| 定时日报推送失败 | 配了机器人但推送未成功 |

```bash
# .env
ALERT_ON_OPS_FAILURE=1    # 关掉就只记日志
```

运维告警与"员工录用失败"是两条独立通道，各自的节流互不影响（运维 5 分钟，
失败告警按 `ALERT_MIN_INTERVAL_S`）。

另外建议用 NAS 自带监控或 Uptime Kuma 盯 `http://NAS的IP:8080/health/ready`：
容器整个挂掉时，服务内部的告警也发不出去。

#### 3.4 必须单进程运行（重要）

**这套服务只能开一个进程。** 不要 `docker compose up --scale server=2`，不要在两台
机器上挂同一个 `data` 目录跑两份，也不要前面挂个多副本的反代让两台各自跑调度器。

原因很具体——日报的"今天推过没有"是「读标记 → 发消息 → 写标记」三步，中间没有锁
（`bot/report_store.py`）。两个进程同时走到 18:00，都会读到 `pushed=false`，于是群里
收到两条一模一样的日报；夜间备份同理，会往 `backups/` 写两批 `.gz` 再互相挤掉，
图片镜像也会两个进程抢同一批文件。所有状态都在本地 SQLite + 数据目录里，没有
任何跨进程协调机制。

**服务会自己发现这件事**（2026-10-25 起）：

- 启动时在 `data/run/instance.lock` 上加一把文件锁，拿不到就在日志里打一条
  `WARNING`：「检测到另一份服务在运行……本服务必须单进程运行」；
- 仪表盘底部「后台服务」那一行会从绿点变黄点，文案带上同样一句；
- 拿不到锁**不会**阻止启动，也不会让它不推日报——多推一条群消息远好过服务起不来。
  判定只是提醒，不改变任何行为。

看到这条警告怎么办：

```bash
# 1. 看是谁占着（cat 得到持有者的 pid / 主机名 / 时间）
cat /volume1/docker/psluyong-lite/data/run/instance.lock
# 2. 确认是不是自己多开了
docker ps --filter name=psluyong    # 有没有第二个容器
# 3. 只该留一个；确认后把多出来的停掉
```

两点例外，都不会影响功能：两份副本不共享 `data` 目录时根本碰不上（也就不会重复
推送）；`data` 落在 NFS/SMB 上时网络文件系统的锁语义不保证，可能探不出来、也可能
每次都警告——后者按上面三条确认一遍即可，不影响服务。

> 以后真要多副本：先加锁选主（谁拿到锁谁跑调度器，其余只服务 HTTP），再做。当前
> 规模（一台 NAS、一个容器）没有这个收益，所以只做检测 + 文档约束。

### 4. 从 Windows 开发机迁移数据（可选）

全新部署跳过本节。如果开发机上已有记录和图片要带到 NAS：
库里 `image_path` / `thumb_path` 存的是**绝对路径**，跨机器拷贝后不会
自动跟随，需要在 NAS 上做一次路径重写：

1. 停掉两边服务，把整个 `server/data` 目录原样拷到 NAS 的项目目录；
2. 在 NAS 上执行（SSH 进 NAS 后）：

```bash
docker exec -i psluyong-lite python - <<'PY'
import sqlite3
conn = sqlite3.connect('/app/data/app.db')
old = 'E:\\psluyong-lite\\server\\data'  # 开发机上的路径前缀，按实际改
new = '/app/data'
for col in ('image_path', 'thumb_path'):
    conn.execute(
        f"UPDATE records SET {col} = REPLACE(REPLACE({col}, ?, ?), '\\', '/')"
        f" WHERE {col} LIKE ?",
        (old, new, old + '%'),
    )
conn.commit()
print('路径重写完成')
PY
```

3. 重启容器，在后台抽查几条记录的缩略图和原图能正常打开。

### 5. 反向代理（可选）

用绿联自带反向代理指向 `127.0.0.1:8080`，并加 HTTPS。若启用 HTTPS，
在 `.env` 里设 `FORCE_HTTPS=1`，否则登录 Cookie 不会带 `Secure` 标记。
外网访问可用自带的 UGREENlink，不必自己折腾 DDNS。

### 6. 数据库并发（WAL 与网络盘）

默认 `SQLITE_JOURNAL_MODE=WAL`：读写不互斥，多线程下不会再偶发
`database is locked`，是 8 线程 waitress + 多人同时提交的正确配置。

但它有一个前提——**数据库必须在本地磁盘上**。WAL 依赖共享内存文件
`app.db-shm`，在 NFS / SMB 网络共享上行为不可靠（可能出现读不到最新数据）。
`./server/data` 是 NAS 本地卷，默认没问题；如果你把 data 目录改成了挂载
的网络共享，请在 `.env` 里改成：

```bash
SQLITE_JOURNAL_MODE=DELETE
```

判断当前生效的模式：

```bash
docker exec psluyong-lite python -c "
from server.app import create_app
from server.app.db import db
from sqlalchemy import text
app = create_app()
with app.app_context():
    print(db.session.execute(text('PRAGMA journal_mode')).scalar())
"
```

输出 `wal` 即正常。

---

## 二、员工客户端（Windows）

### 开发调试

```bash
cd client
npm install
npm run tauri:dev
```

Rust 外壳会自动拉起 Python sidecar（`python -m client.sidecar.server`）。
若本机 python 不在 PATH，设置：

```bash
set PSLUYONG_PYTHON=C:\path\to\python.exe
set PSLUYONG_REPO_ROOT=E:\psluyong-lite
```

依赖注意：员工机需要 pywin32（COM 调 Photoshop 用），已写在
`requirements.txt` 里（Windows marker），在员工机执行一次
`pip install -r requirements.txt` 即可，Linux 上会自动跳过。

### 打包 sidecar

```powershell
# 需要先装：pip install pyinstaller
powershell -ExecutionPolicy Bypass -File scripts\build_sidecar.ps1
```

产物 `client/src-tauri/sidecar-dist/psluyong-sidecar.exe`，会被 Tauri 一并打进安装包。

### 打包安装包

```bash
cd client
npm run tauri:build
```

产物在 `client/src-tauri/target/release/bundle/nsis/`，双击安装即可。

### 员工机器前置条件

| 项 | 要求 |
|---|---|
| 系统 | Windows 10 / 11（64 位） |
| Photoshop | 已安装且能正常启动；建议生成前先手动打开 |
| 字体 | 模板用到的字体必须已安装，否则生成会告警"字体不可用" |
| 网络 | 能访问 NAS 的 8080 端口 |

高 DPI：WebView2 会跟随系统缩放，1080P / 2K / 4K 与 125% / 150% / 200% 都无需额外配置。

### 配置文件在哪、坏了怎么办

员工的所有设置（服务器地址、API Key、姓名、模板目录、窗口位置）都在
`%APPDATA%\psluyong-lite\config.json`。这个文件**不需要手工编辑**，只需要知道它出事时会怎样：

| 现象 | 实际发生了什么 | 员工要做什么 |
|---|---|---|
| 弹一次「配置文件损坏…」提示，其他一切正常 | 文件被写坏（断电/杀软/手工改），客户端已从 `config.json.bak` 自动恢复 | 不用管，关掉提示即可；保险起见去「设置 → 账号」看一眼地址和 Key 还在不在 |
| 弹「…没有可用备份，已重置为默认值」，且底栏一直显示"服务器离线" | 文件坏了且备份也没有（多半是第一次就坏了），只能用默认值启动 | **重新填服务器地址和 API Key**（设置 → 账号），保存后底栏应恢复"在线" |
| 保存设置时提示「配置文件暂时被占用，本次不写入，请稍后重试」 | 杀毒软件/备份软件正占着这个文件；客户端**故意不写**，避免用默认值覆盖真配置 | 等几秒再点保存；反复出现就把 `%APPDATA%\psluyong-lite` 加进杀软白名单 |

出事的原文件会以 `config.broken-<日期>-<时间>.json` 留在同一目录，不要删——报障时连同它一起发给运维，能直接看出里面还留着哪些值。同目录的 `config.json.bak` 是上一版配置，恢复时会自动读取。

### 生成完了但后台查不到这条记录，怎么查

客户端不会让记录掉在"没人看"的空档里：生成的终态一落盘，就由 sidecar 自己去结算
（报给服务器，报不上去就进离线队列），前端轮询、后台每 15 秒扫一遍、下次启动对账
走的是同一段逻辑。所以正常情况下列表里不会少记录。真少了，按顺序看：

| 现象 | 实际发生了什么 | 怎么办 |
|---|---|---|
| 主窗底栏显示「待补报 N 次」 | 结算连续失败（磁盘满 / 本地服务异常），图在输出目录、记录还没落地 | 客户端会继续重试；先看输出目录磁盘是否满，再把 `%APPDATA%\psluyong-lite\logs\sidecar.log` 发给运维 |
| 底栏显示「待补传 N 条」 | 记录已在本地队列里，只是服务器没连上（或 Key/地址错） | 修好网络/配置后自动补传，也可点「立即补传」 |
| 底栏显示「其中 N 条因配置受阻」 | 服务器 401/403/404：Key 轮换没同步或地址填错 | 去「设置 → 账号」核对，改对后自动补传 |
| 连底栏都没有，但输出目录里有图 | 这条任务在结算前就被删了（正常情况下不会：看 `logs\sidecar.log` 有没有「任务结算失败」） | 把该目录下的图、`logs\` 一起打包给运维补录 |

排障时可以直读现场：`%APPDATA%\psluyong-lite\executions\<任务id>\` 下
`status.json` 是 worker 的终态、`result.json` 是"已结算"标记（有它就说明服务器或队列
已经收下了）。有 `status.json` 却没有 `result.json`，就是下一次启动会被自动补报的那一条。

---

### 生成失败，提示「成品几乎是纯色 / 尺寸与模板不一致 / 文件不完整」，怎么查

客户端不在"PS 说成功"就交差：图先写到临时名，再拿 Pillow 真读一遍，四类假成功都拦下来：

| 现象（员工看到的） | 实际发生了什么 | 怎么办 |
|---|---|---|
| 「成品尺寸 400×300 与模板 800×600 不一致」 | 模板 PSD 被改过（裁切/改画布）或导出设置被改；留了证据图 `X.校验未通过.jpg` | 打开那张证据图看：内容对但尺寸小 → 模板画布被改过，重新上传模板；内容也不对 → 让员工把模板 PSD 发给你 |
| 「成品几乎是纯色（文字或图层没渲染出来）」 | 文字层被隐藏/锁住、字体缺失导致渲染空、或导出的画布选错了 | 同上，打开 `X.校验未通过.jpg` 核对；若模板本身没问题，查 PS 里字体是否装齐 |
| 「导出文件不完整或不是有效图片（PS 在保存过程中被中断）」 | 员工点了「取消生成」正好撞上 SaveAs，或 PS 自己崩了。这类文件已**直接删除**，输出目录里不会留 | 重试一次即可；同一台机器反复出现就查 PS 暂存盘 |
| 「模板色彩模式不是 RGB（4），已转成 RGB 导出…」 | 不是失败，是 warning：CMYK 模板被转成 RGB 了，颜色会有细微差异 | 要严格还原颜色就让设计把模板存成 RGB/8bit 再上传 |
| 输出目录里出现 `X.psluyong-tmp.jpg` | 导出中途进程被杀（客户端崩溃/任务管理器结束），临时文件没来得及改名 | 不用管：一小时后下次生成会自动清理；急着交付就手动删掉，那不是成品 |

顺带说明为什么临时文件叫 `.psluyong-tmp.jpg` 而不是 `.part`：PS 是按扩展名决定存什么格式的，
去掉 `.jpg` 它会存成裸文件或直接报错。

---

## 三、QQ 群日报（官方机器人）

### 0. 为什么是官方机器人，不是"登录一个 QQ 号"

老版本用 NapCat（第三方协议端，登录一个真实 QQ 号当机器人）。2026-10 整体换成
QQ 开放平台的官方机器人，原因不是技术偏好：

- **授权**：NapCat 的 README 明确禁止未经作者授权的衍生分发，而我们把它打进
  NAS 安装包＝在分发，这件事没有授权依据；
- **风险**：真号登录态存在容器里（`./QQ` 目录），WebUI 的 6099 端口是真号的操作台，
  账号被风控/被封的成本是"一个真 QQ 号"；
- **能力**：官方通道对群主动消息的额度是**已认证 60 条/分钟、1000 条/天/群**，
  我们一天十条都不到，完全够用。

代价是**没有了"群里发指令回日报"**（第 5 节）：官方平台只投递 @机器人 的消息事件，
做不到"监听全群关键词"，这个功能已随 NapCat 一起删除。

### 1. 在开放平台创建机器人

1. 浏览器打开 <https://q.qq.com>，手机 QQ 扫码登录 → 选「个人」主体 → 完成实名认证；
2. 「创建机器人」，填名称/简介（头像可跳过）；
3. 进「开发设置」→「开发配置」，复制 **AppID** 和 **AppSecret**。
   ⚠ AppSecret **只显示一次**，丢了只能重置（旧值立刻失效）；
4. **IP 白名单**：填 NAS 的**出口公网 IP**（在 NAS 上 `curl ifconfig.me` 查）。
   不填会一直报「该机器人去火星了，稍后再试吧」；
5. 把机器人**拉进目标群**（个人认证的机器人可以加进你是群主/管理员的群）；
6. **手机 QQ → 该群 → 右上角设置 → 机器人 → 打开「机器人主动在群聊内发言」**。
   这个开关不打开，日报永远发不出去（服务端回 `40034102 主动消息失败, 无权限`）。

### 2. 取群的 `group_openid`（不是群号）

官方接口发群消息用的是 `group_openid`——一串只有从**事件**里才能拿到的字符串，
把群号 `123456789` 填进去只会拿到 `40054002`。

两种拿法，任选一种：

- **手动**：机器人进群后，在群里 @ 它说一句话，服务端日志里
  `GROUP_AT_MESSAGE_CREATE` 事件带的那串 `group_openid` 就是它；
- **自动**：`python scripts/qqbot_spike.py`（脚本会连 WebSocket 抓第一条群事件，
  并把建议写进 `.env` 的那一行直接打出来）。

### 3. 配置 .env

```bash
QQ_BOT_APPID=<第 1 步的 AppID>
QQ_BOT_SECRET=<第 1 步的 AppSecret>
QQ_BOT_GROUP_OPENID=<第 2 步拿到的 openid>
DAILY_REPORT_HOUR=18
DAILY_REPORT_MINUTE=0
ENABLE_SCHEDULER=1
```

三项缺任何一项都不会报错：日报照常生成存档，只是不推送，仪表盘那一行显示
灰色「未配置（缺少 XXX）」——"没配"和"配了推失败"在页面是两种颜色，别混。

改完重启容器：

```bash
docker compose up -d
```

> 从老版本升级：把 `.env` 里的 `NAPCAT_WEBHOOK` / `NAPCAT_TOKEN` / `REPORT_GROUP_ID`
> 三行删掉，**并停掉 napcat 容器**——它存着一个真 QQ 的登录态，留着就是风险点。

### 4. 验证与补推

1. 管理员登录后台 →「每日日报」页。每条日报左侧有推送徽标：
   **已推送**（绿）/ **未推送**（红）/ **已推送·终版**（绿）。
2. 选中任意一天，点右上角「**重推此日报**」立即补推，徽标实时更新——
   联调就用这个按钮，不用等 18:00。
3. 定时任务每天 `DAILY_REPORT_HOUR:MINUTE` 自动生成并推送。

**18:00 的快报和次日的终版是两件事**，别把两条消息当成重复推送：

| 时点 | 内容 | 什么时候发 |
|---|---|---|
| 当天 18:00（`DAILY_REPORT_HOUR`） | 当日快报 | 每天固定发 |
| 次日 08:30（`REPORT_FINAL_HOUR`） | 昨天**终版**，含 18:00 之后补录的记录 | **只在数据变了时**才发；没变一条消息都没有 |

终版这一趟顺带兜住"18:00 服务没起来/推送失败"——那种情况定时任务补不回来
（APScheduler 的重启补跑只对"进程还活着"有效）。巡检还会往前看
`REPORT_MISS_LOOKBACK_DAYS` 天，发现"该有日报却没推成功"的日子就发一条运维告警
（列出日期），**不会**自动把旧日报刷进群里；补推走上面第 2 步。

排障对照：

| 现象 | 原因 | 处置 |
|---|---|---|
| 群里只有 18:00 那条，没有终版 | 数据没变化（正常） | 不用管 |
| 第二天中午了，既没终版也没告警 | 容器在 08:30 之前被重启过、或 `ENABLE_SCHEDULER=0` | 看容器日志有无「日报终版巡检」；手动点「重推此日报」 |
| 收到「有 N 天日报没推送成功」告警 | 那几天推送不通 | 按告警里的日期逐个重推 |
| 仪表盘日报一行 18:00 后立刻变黄 | 当天该推的还没推成功 | 查第四节排障表 |

### 5. 群内指令：已移除

老版本支持"群里发「汇总」→ 机器人私聊回当天日报"。官方平台**只投递 @机器人
的消息事件**，监听全群关键词在架构上做不到，所以这个功能连同 `/api/v1/bot/onebot`
回调端点、QQ/群白名单、回调密钥一起删除了。

要看报表：后台「每日日报」页可以直接选任意日期查看/重推；或者用系统通知里的
那几条运维告警。

### 6. 失败即时告警

录用失败**立刻**推到日报群，不等当天日报——失败意味着员工白跑一趟 PS，
越早止损越好。

```bash
ALERT_ON_FAILURE=1       # 1=开 0=关
ALERT_MIN_INTERVAL_S=5   # 告警最小间隔：PS 故障批量失败不会把群刷爆
```

告警内容：员工/模板/稿件/失败原因/时间。推送失败不影响记录本身
（记录已落库，只是通知不到，本地 `data/logs/ops-alerts.jsonl` 仍有流水）。

### 7. 失败行为与排障

- **自动重试**：网络抖动 / 5xx 会重试 3 次（间隔 5s / 15s），仍失败则徽标
  显示「未推送」，并把原因写进当天的 `status.json`。日报正文始终存档不丢。
  **4xx 和业务错误码不重试**——重发同样失败，只会让人晚 20 秒才知道。
- **access_token**：2 小时有效，进程内缓存并在过期前 60 秒自动换新；
  收到 401 会立刻丢缓存重取一次。

错误码对照表（都在 `data/reports/<日期>.status.json` 的 `message` 里）：

| 报错 | 含义 | 处置 |
|---|---|---|
| `40034102 主动消息失败, 无权限` | 群里没开「机器人主动在群聊内发言」 | 手机 QQ 群设置 → 机器人 → 打开该开关 |
| `40013` / HTTP 401 | AppID/AppSecret 错或已重置 | 到 q.qq.com 重新复制，改 `.env` 后重启 |
| `40054002` / HTTP 404 | `QQ_BOT_GROUP_OPENID` 填成了群号或已失效 | 按第 2 节重新取 openid |
| `11244` / HTTP 429 | 触发频率限制（60 条/分钟、1000 条/天/群） | 等一分钟；超了通常是有人在刷「重推」 |
| 「该机器人去火星了」 | 出口 IP 不在白名单 | q.qq.com 后台补 IP 白名单 |
| 「连不上」/超时 | NAS 出不了网或 DNS 有问题 | 容器内 `curl -I https://api.bot.qq.com` |
| 完全没配机器人 | 三项没填齐 | 正常的「只生成不推送」，仪表盘灰点 |

## 四、上线检查清单

**先跑脚本，再勾这个清单。** 下面这份是给"人"看的版本；其中凡是机器能判的，都做成了
`scripts/preflight_check.py`，有 FAIL 就是退出码 1，可以直接挂在 CI 或部署前置闸门上：

```bash
.venv/Scripts/python.exe scripts/preflight_check.py                              # 表格
.venv/Scripts/python.exe scripts/preflight_check.py --base-url http://NAS:8080    # 顺带探就绪探针
.venv/Scripts/python.exe scripts/preflight_check.py --json | jq '.failed'         # 给 CI
```

三态，含义别混：

| 结果 | 含义 | 怎么办 |
|---|---|---|
| `通过` | 机器验过，确实如此 | — |
| `不通过` | 机器验过，确实不行；**退出码 1** | 按 `→` 后面的提示修，别绕过 |
| `需人工` | 机器判不了（要么服务没起，要么本来就靠人看） | 就是下面清单里剩下的那些，逐个勾 |

> 把 `需人工` 当 `不通过` 会让闸门天天响，人就学会"跑不过也照上"——那闸门就白做了。
> 反过来，把 `不通过` 当 `需人工` 放过去，就等于没这道闸门。

脚本判不了、必须人看的：

- [ ] 镜像加速已配（Docker → 镜像 → 设置 → 加速器配置）
- [ ] 【Docker】→【容器】里 psluyong-lite 状态为「运行中」
- [ ] 已用管理员登录，改掉默认密码
- [ ] 至少上传 1 个模板，并在客户端成功出过 1 张图
- [ ] **生成完立刻关掉客户端再重开**，后台仍能查到那条记录（验的是"关窗也补报"这条路）
- [ ] 用本机真实模板出过 1 张图，输出目录里**只有** `作者_标题.jpg`（不该有 `.psluyong-tmp` 或 `.校验未通过`）
- [ ] 员工机器的 PS 版本已确认（不同版本 COM 行为略有差异）
- [ ] `server/data` 已纳入 NAS 定时备份
- [ ] 容器日志已限流（compose 里的 `logging` 段），`docker logs` 不会无限长
- [ ] 运维告警能收到：手动删掉一个备份文件后跑一次 `python -m server.backup` 验证
- [ ] 后台「操作日志」页能看到本次登录记录
- [ ] `ENABLE_SCHEDULER=1`，且「每日日报」页能看到当天日报
- [ ] **只有一个容器在跑**：`docker ps --filter name=psluyong` 只有一行，且启动日志里
      没有「检测到另一份服务在运行」（多副本会重复推日报，见 §3.4）
- [ ] 机器人已创建，`QQ_BOT_APPID` / `QQ_BOT_SECRET` / `QQ_BOT_GROUP_OPENID` 三项已填
- [ ] 群设置里已打开「机器人主动在群聊内发言」（否则 40034102）
- [ ] 开放平台 IP 白名单已填 NAS 的出口公网 IP（否则「该机器人去火星了」）
- [ ] 后台点「重推此日报」，QQ 群收到且徽标变「已推送」
- [ ] 上线前已记录本次镜像 tag：`docker images psluyong-lite`（回滚要用，见第五节）
- [ ] **每月一次恢复演练**：`docker exec psluyong-lite python -m server.backup --restore <最近一份备份文件> --rehearse` 输出「彩排通过」且打印各表行数——只校验不落盘，练的是"备份真的能恢复"（步骤见第五节 5.3）
- [ ] **员工手册已发到每个人手上**（[员工手册.md](员工手册.md)），并确认他们知道「设置 → 高级 → 导出诊断包」在哪

### 4.1 员工报障：先要诊断包

员工说"传不上去 / 出不了图"时，第一句就要求他导诊断包，不要隔空问日志：

> 设置（右上齿轮）→ **高级** → **「导出诊断包」**

sidecar 会打一个 zip 存到 `%APPDATA%\psluyong-lite\diagnostics\` 并打开所在文件夹。里面有：

| 文件 | 内容 |
|---|---|
| `README.txt` | 这个包怎么读、里面有什么、哪些字段已脱敏 |
| `meta.json` | 客户端版本、机器名、导出时间、路径、Python/PS 版本 |
| `config.sanitized.json` | 完整配置，**`api_key` 已替换成 `***已隐藏（原长度 N）***`** |
| `server_health.json` | 服务端 `/health/ready` 的原始返回；探不到时记 `error` 而不是抛异常 |
| `logs/*.log` | 运行时日志（每份只取**尾部 300 行**） |
| `logs/crash-*.txt` | 最近 3 份崩溃转储 |
| `executions/*` | 最近 **5 次**生成的 `params` / `status` / `result` |

任一项取不到都只降级、不中断出包（配置坏了也要能出包，那正是最需要它的时候）。
整个包上限 8MB。**可以直接让员工发群里**——密钥已经抹掉了。

几个常见读法：

- `server_health.json` 里是 `"status_code": 502` 加一段英文 `raw` → 是**代理**回的，不是服务端。看 `client/sidecar/net.py` 的注释，内网请求已强制绕开环境代理；若仍出现，查那台机器上是否有人改回了裸 `requests`
- `config.sanitized.json` 里 `server_url` 指向一个已经不存在的 IP → 换服务器后没同步
- `executions/` 里最后一条 `result.json` 是 `failed` 且 `error` 提到占位符 → 模板被人改过，不是客户端问题

脚本判不了的还有一条：`需人工` 里的「就绪探针」如果不加 `--base-url` 会一直是 `需人工`——那是故意的，脚本不替服务端的健康负责。

---

## 五、回滚

> 前提：镜像按 git 短 SHA 打了 tag（见 5.1）。回滚分两条链路：**应用**回退镜像 tag，
> **数据**回退备份文件，两条互不绑定——镜像回退不动数据，数据回退不动镜像。

### 5.1 镜像版本化（打 tag 规范）

两份 compose（根 `docker-compose.yml`、`deploy/nas/docker-compose.yml`）的镜像名统一为参数化写法：

```yaml
image: psluyong-lite:${PSLUYONG_IMAGE_TAG:-latest}
```

- **构建时**用 git 短 SHA 作 tag（一个 tag 对应一个可复现的提交）：

```bash
PSLUYONG_IMAGE_TAG=$(git rev-parse --short HEAD) docker compose up -d --build
```

- UGOS 图形界面传不了命令行变量时，在项目目录的 `.env` 里加一行即可——compose
  自动读同目录 `.env` 做变量替换，界面部署同样生效：

```bash
# .env
PSLUYONG_IMAGE_TAG=a1b2c3d
```

- 不设 `PSLUYONG_IMAGE_TAG` 时退化为 `latest`，与旧部署完全兼容（不改 `.env` 也不会挂）。
- 构建后确认 tag：`docker images psluyong-lite`。**每次上线前记下当前可用 tag**，
  正常状态下应能看到新旧 tag 并存（回滚就是靠旧 tag）。

### 5.2 应用回滚（回退镜像）

1. 先定目标 tag：`docker images psluyong-lite --format '{{.Tag}}'`（取上一个可用的短 SHA）。
2. 本地还留着旧镜像时，**直接指定旧 tag 重新起容器**（不重新构建，秒级回退）：

```bash
PSLUYONG_IMAGE_TAG=<旧短SHA> docker compose up -d
```

UGOS 图形界面：把 `.env` 里的 `PSLUYONG_IMAGE_TAG` 改成旧短 SHA → 项目 → 重启。

3. 旧镜像已被清理时，检出旧提交重新构建：

```bash
git checkout <上一个可用提交>          # 或 git revert <坏提交>
PSLUYONG_IMAGE_TAG=$(git rev-parse --short HEAD) docker compose up -d --build
git checkout main                      # 回到原分支
```

4. 验证：`/health/ready` 返回 200、管理员能登录、「每日日报」页正常。
   仍不对就再往前退一个 tag——**不要在现场改代码**。

> 镜像回滚只换代码不换数据：`server/data`（NAS 包为 `data/`）是独立数据卷，
> 换 tag 不影响库和图片。若坏版本已经写坏了数据，走 5.3。

### 5.3 数据库回滚（恢复备份）

能力都在 `python -m server.backup`（参数以 `python -m server.backup --help` 为准：
`--list` / `--restore FILE` / `--rehearse` / `--keep N` / `--mirror-images`）：

```bash
# 1. 看有哪些备份（文件在 /app/data/backups/，每天 03:30 自动一份）
docker exec psluyong-lite python -m server.backup --list

# 2. 彩排：解压 + PRAGMA integrity_check + 核心表校验 + 打印各表行数，
#    全过才打印「彩排通过」——不写任何文件，随时可跑
docker exec psluyong-lite python -m server.backup \
    --restore /app/data/backups/<文件名>.db.gz --rehearse

# 3. 上一步通过后才做真实恢复
docker exec psluyong-lite python -m server.backup \
    --restore /app/data/backups/<文件名>.db.gz

# 4. 重启容器让服务重新打开库（恢复执行时旧进程仍握着旧库的文件句柄）
docker compose restart server
# UGOS 图形界面：Docker → 项目 → psluyong-lite → 重启
```

- 恢复前会自动把当前库留档为 `app.db.pre-restore-<8位>`；恢复出错就停容器、
  把留档文件改回 `app.db`，即回到恢复前状态。
- 彩排（`--rehearse`）与真实恢复用同一条命令，只差最后一个参数——每月演练
  （上线检查清单最后一项）跑的就是它。
- **兜底**：整库恢复不了时，停容器后直接还原 `server/data`（NAS 包为 `data/`）
  整个目录，`app.db-wal` / `app.db-shm` 一并删掉，再起容器。

---

## 六、两份 Dockerfile / 两份 compose 配置差异对照（漂移收敛记录）

四份部署配置 = 根 `Dockerfile` + `deploy/nas/Dockerfile.lean` + 根 `docker-compose.yml` +
`deploy/nas/docker-compose.yml`。**改任何一份之前先查本表**，防止只改一半又造新漂移。

| 维度 | 根（仓库直部） | deploy/nas（package_nas 打包） | 处置 |
|---|---|---|---|
| 构建上下文 | 仓库根，`build: .`，Dockerfile/源码齐全 | **本目录无 Dockerfile 与源码，直接 `up --build` 必失败**；须在仓库根 `python scripts/package_nas.py` 打包，zip 根目录才是完整上下文 | 保留差异 + 两边都写明（compose 头注释、包内 `0-操作步骤.txt` 第九节） |
| pip 源 | 官方 pypi.org | 仅清华源（国内实测 pypi 仅 13.8 kB/s，SQLAlchemy 直接超时） | 功能性差异，保留 |
| apt 依赖 | 装 `curl` + libjpeg/zlib/tzdata | 不跑 apt（Pillow manylinux wheel 自带 libjpeg/zlib，tzdata 由 pip 包提供） | 功能性差异，保留 |
| healthcheck 探针 | `curl -fsS /health/ready` | `python -c urllib`（精简镜像里没有 curl） | 功能性差异，与上一行绑定，保留 |
| 运行身份 | `USER appuser`（1000），最小权限 | `user: "0:0"` + 启动时 `chmod -R 777 /app/data` | **功能性差异保留**——NAS 挂载目录属主不一定是 1000，root+777 是挂载属主权宜。**风险**：容器内任何进程可改写全部数据，文件权限形同虚设。**改进路径**：NAS 文件管理把 `data` 目录属主改为 1000:1000 后，删掉 NAS compose 的 `user:` 与 `command:` 两行即回到最小权限 |
| 数据卷 | `./server/data:/app/data` | `./data:/app/data` | 功能性差异保留（zip 根目录即项目目录，目录结构不同） |
| ENABLE_SCHEDULER | `${ENABLE_SCHEDULER:-1}`（environment 显式声明） | 原先缺失 → **已补齐同款一行** | **真漂移已修复**。核实结论：`Config.ENABLE_SCHEDULER` 缺省为 1、打包出的 `.env` 也带 `ENABLE_SCHEDULER=1`，所以此前 NAS 日报并不会不跑——但缺键只是巧合兜底，两份 compose 必须同款显式声明，改默认值时才不会只影响一边 |
| extra_hosts | 无 | 无 | **已收敛**：NapCat 桥接删除后两边都不再需要访问宿主机 |
| 镜像 tag | `psluyong-lite:${PSLUYONG_IMAGE_TAG:-latest}` | 同左 | **已收敛为同款**（原两处写死 `latest`，即 B7） |
| 日志限流 / 资源限制 | json-file 10m×3 / 2 核 1G | 同左 | 一致，无漂移 |

## 七、密钥轮换（泄露响应）

`.env` 里这几个值一旦泄露（截图、误提交、交接不清），按下面顺序换，**换完必须重启生效**。
2026-10-24 的真实事故就是这么处理的：`.env.bak.ci`（复现「CI 没有 .env」时的备份）被
`git add -A` 带进提交并推到公开仓库；即使 force-push 重写历史，旧提交 SHA 在 GitHub 上
仍能被直接访问——**删除历史不等于止血，轮换才是**。

| 键 | 影响面 | 换的代价 |
|----|--------|----------|
| `SECRET_KEY` | 登录 session 签名 | 全员重新登录，无其它影响。最先换这个 |
| `API_KEY` | 员工客户端提交记录 | **每台员工机的客户端配置都要同步改**，否则提交 401 |
| `ADMIN_PASSWORD` | 主管后台登录 | 见下方「注意」：改 `.env` 不会改已有管理员密码，必须走 `reset-password` |
| `QQ_BOT_SECRET` | 官方机器人鉴权（唯一出口） | **不能本地生成**：去 q.qq.com 后台重置 AppSecret，粘回 `.env` 再重启。旧值立刻失效 |

生成新值（任意 Python 3 环境）：

```bash
python -c "import secrets;print(secrets.token_urlsafe(32))"
```

### 一键轮换（推荐）

仓库自带 `scripts/rotate_secrets.py`，本地和容器里都能跑（镜像里已通过
`COPY scripts/rotate_secrets.py` 带上，发布 zip 也会打进 `scripts/`）。它做的事：
备份到**仓库外** → 只改目标行 → 原子写回 `.env` → 重置数据库里管理员的密码
（这步手工最容易漏）→ 打印新值掩码与"还要同步谁"的清单。备份目录只要指到仓库里，
它会直接拒绝执行。

```bash
# 先看计划（不落盘，顺便校验备份目录）
python scripts/rotate_secrets.py --dry-run

# 本地轮换四项
python scripts/rotate_secrets.py --backup-dir D:/psluyong-env-backups

# NAS 上（容器内跑，然后重启让 server 进程重新读 .env）
docker compose exec web python scripts/rotate_secrets.py --yes --backup-dir /tmp/env-backups
docker compose up -d
```

只换其中几项、暂时不动 `API_KEY`（避免逐台跑员工机）：

```bash
python scripts/rotate_secrets.py --keys SECRET_KEY,ADMIN_PASSWORD
```

跑完按屏幕最后那段清单去改员工客户端即可。**脚本不会替你重启服务、也不会替你
改客户端的配置**——这两件仍然要人手做。`QQ_BOT_SECRET` 不在脚本管辖范围内
（见上表：它只能去平台重置）。

### 手工换法（脚本不可用时）

```bash
# 1. 停服 + 备份当前 .env（备份放项目目录外，别再犯误提交的错）
docker compose down
cp .env /volume1/备份/.env.bak.$(date +%Y%m%d)

# 2. 改 .env 里对应四行（绿联文件管理默认隐藏 . 开头文件，需先开「显示隐藏文件」）
#    SECRET_KEY= / API_KEY= / ADMIN_PASSWORD=

# 3. 起服
docker compose up -d

# 4. 管理员密码必须单独重置（init_seed 只在「查无此用户」时建号）
docker compose exec web flask reset-password --username admin --password '<新密码>'

# 5. 验证
curl -fsS http://127.0.0.1:8000/health/ready
```

> **注意**：`server/app/models.py::init_seed` 只在管理员不存在时写密码，所以**只改 `.env`
> 里的 `ADMIN_PASSWORD` 对已部署的库无效**，登录仍是旧密码——第 4 步不能省。

### 员工客户端换法

改了 `API_KEY` 后，每台员工机：客户端「设置 → 服务端」的 API Key 填新值 → 保存 → 提交一条
测试记录确认不再 401。漏改的机器会持续提交失败并堆在客户端离线队列里，补传时依旧 401——
**API_KEY 要么不换，要换就一次换干净**。短期无法逐台更新时，先只换 `SECRET_KEY` 与
`ADMIN_PASSWORD`，把 `API_KEY` 排到下次上门维护。

### 事故后的额外动作

1. GitHub 上的旧对象不会因 force-push 立刻消失（`raw.githubusercontent.com/<owner>/<repo>/<旧SHA>/.env` 仍可访问），
   轮换后可向 GitHub Support 提工单请求清理缓存对象；**把仓库转 Private 是最快的止血**。
2. 泄露过的值一律当「已公开」，任何环境都不再复用。
3. 查审计日志有无异常登录：后台「审计日志」页，或 `app.db` 的 `audit_log` 表。
