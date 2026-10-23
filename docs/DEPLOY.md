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

---

## 三、QQ 群日报（NapCat）

### 1. 部署 NapCat（UGOS Pro 项目 Compose）

镜像用社区维护的 `mlikiowa/napcat-docker`。端口：3000=OneBot HTTP、
6099=WebUI。宿主机 3000 容易被其他服务占用，HTTP 映射到 **13000**。

「文件管理」在 docker 共享文件夹下新建 `napcat/`，【Docker】应用 →
【项目】→【创建】，项目目录选该文件夹，粘贴：

```yaml
services:
  napcat:
    image: mlikiowa/napcat-docker:latest
    container_name: napcat
    restart: always
    ports:
      - "13000:3000"   # OneBot HTTP（勿占宿主机 3000）
      - "6099:6099"    # WebUI 管理界面
    volumes:
      - ./QQ:/app/.config/QQ          # QQ 登录态，删了要重扫
      - ./config:/app/napcat/config   # NapCat 配置（OneBot 开关在这里）
    environment:
      - NAPCAT_UID=0
      - NAPCAT_GID=0
      - TZ=Asia/Shanghai
```

拉镜像慢的话先配第一节的镜像加速。

### 2. 登录 QQ 并开启 HTTP 服务端

1. 容器起来后执行 `docker logs napcat`：日志最顶部有 WebUI 的 Token
   （在登录二维码上方）。浏览器开 `http://NAS的IP:6099`，用这个 Token 进 WebUI。
2. 手机 QQ 扫码登录。**建议用小号**——电脑端 QQ 会被挤下线。
3. **关键一步**：WebUI →「网络配置」→ 新建/启用 **HTTP 服务端**，
   端口 3000，并设一个鉴权 Token。这个 Token 就是下面 `.env` 里的
   `NAPCAT_TOKEN`——注意它和 WebUI Token 是两套，可以设成不一样。
4. 把该 QQ 号拉进目标群。

### 3. 对接 psluyong-lite

`.env` 里补上（用 NAS 的局域网 IP——UGOS 的 Docker 项目之间用容器名
解析不到，别写 `http://napcat:3000`）：

```bash
NAPCAT_WEBHOOK=http://192.168.x.x:13000   # NAS 的 IP + 第一步映射的端口
NAPCAT_TOKEN=HTTP 服务端的 Token
REPORT_GROUP_ID=123456789                 # 目标 QQ 群号
DAILY_REPORT_HOUR=18
DAILY_REPORT_MINUTE=0
ENABLE_SCHEDULER=1
```

IP 在 控制面板 → 网络设置 → 网络连接 查看。改完重启容器：

```bash
docker compose up -d
```

### 4. 验证与补推

1. 管理员登录后台 →「每日日报」页。每条日报左侧有推送徽标：
   **已推送**（绿）/ **未推送**（红）。
2. 选中任意一天，点右上角「**重推此日报**」立即补推，徽标实时更新——
   联调 NapCat 就用这个按钮，不用敲命令。
3. 定时任务每天 `DAILY_REPORT_HOUR:MINUTE` 自动生成并推送。

### 5. 群内指令（可选）：群里发指令自动回日报

不想等每天定时推送，可以在群里发指令，机器人**私聊**回复触发者
（不推群、不刷屏；私聊失败才 fallback 发回群里）。

1. NapCat WebUI →「网络配置」→ 新建/启用 **HTTP 上报**（反向 post）：
   - 上报地址 `http://<NAS局域网IP>:8080/api/v1/bot/onebot`
   - 勾选**群消息**上报，心跳/通知类不用开
   - 请求头加 `X-Bot-Secret: <你的密钥>`（配了 `BOT_CALLBACK_SECRET` 才需要）
2. `.env` 补上：

```bash
# 允许触发指令的 QQ 号，逗号分隔；留空 = 群里任何人都能触发
BOT_ADMIN_QQ_IDS=10001,10002
# 回调校验密钥，与 NapCat 上报请求头 X-Bot-Secret 保持一致；留空 = 不校验
BOT_CALLBACK_SECRET=
# 同一人触发冷却秒数，防连点
BOT_COOLDOWN_S=10
```

3. 重启 `docker compose up -d`，在群里发指令即可。

指令集（精确匹配，多说一个字都不触发，避免正常聊天被误触发）：

| 指令 | 回复 |
|---|---|
| `汇总` / `日报` / `数据汇总` | 当天日报 |
| `汇总 昨天` / `前天` / `今天` | 指定某天日报 |
| `汇总 MM-DD` / `汇总 YYYY-MM-DD` | 指定日期日报 |
| `汇总 本周` / `本月` / `上周` | 区间汇总（带「按日」拆分） |
| `帮助` / `指令` | 指令列表 |

日期参数无法识别时（如 `汇总 乱码`）也回帮助文案。每次触发在后台
「操作日志」留一条 `群内指令` 流水（actor=QQ 号，可追溯谁触发过）。

### 6. 失败即时告警

录用失败**立刻**推到 `REPORT_GROUP_ID`，不等当天日报——失败意味着员工
白跑一趟 PS，越早止损越好。

```bash
ALERT_ON_FAILURE=1       # 1=开 0=关
ALERT_MIN_INTERVAL_S=5   # 告警最小间隔：PS 故障批量失败不会把群刷爆
```

告警内容：员工/模板/稿件/失败原因/时间。推送失败不影响记录本身
（记录已落库，只是通知不到）。

### 7. 失败行为与排障

- **自动重试**：推送失败会重试 3 次（间隔 5s / 15s），仍失败则徽标
  显示「未推送」。日报正文始终存档不丢，随时可以重推。
- **徽标一直「未推送」但手动能推成功** → 到点那一刻 NapCat 没在跑。
  确认 napcat 容器 `restart: always` 且 NAS 已开机自启。
- **推送返回 HTTP 401/403** → `NAPCAT_TOKEN` 填错了，必须是 HTTP
   服务端的 Token，不是 WebUI 的。
- **接口通但群收不到** → 机器人不在群里，或 `REPORT_GROUP_ID` 填错。
- **完全没配 NapCat 时**：不报错，日报照常生成存档，只是不推送——
  可以先跑通主链路，机器人事后再接。

---

## 四、上线检查清单

- [ ] `.env` 里三个密钥都不是示例值
- [ ] 镜像加速已配（Docker → 镜像 → 设置 → 加速器配置）
- [ ] 【Docker】→【容器】里 psluyong-lite 状态为「运行中」
- [ ] 已用管理员登录，改掉默认密码
- [ ] `docker exec psluyong-lite python -m server.backup --list` 能列出备份
- [ ] 至少上传 1 个模板，并在客户端成功出过 1 张图
- [ ] 员工机器的 PS 版本已确认（不同版本 COM 行为略有差异）
- [ ] `server/data` 已纳入 NAS 定时备份
- [ ] `PRAGMA journal_mode` 输出为 `wal`（data 目录在网络盘上时改为 `DELETE`）
- [ ] 后台「操作日志」页能看到本次登录记录
- [ ] `ENABLE_SCHEDULER=1`，且「每日日报」页能看到当天日报
- [ ] NapCat 已部署，HTTP 服务端 Token 已填进 `NAPCAT_TOKEN`
- [ ] 后台点「重推此日报」，QQ 群收到且徽标变「已推送」
- [ ] 配了群内指令：NapCat 上报地址指向 `/api/v1/bot/onebot`，群里发「汇总」私聊能收到
- [ ] 检查 `/health/ready` 返回 200
