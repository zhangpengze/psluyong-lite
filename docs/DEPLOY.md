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

以下四类问题原来只写日志（日志一轮转就消失），现在会推到 `REPORT_GROUP_ID`：

| 场景 | 触发 |
|---|---|
| 数据库备份失败 | 夜间任务异常 |
| 图片镜像失败 | 单张复制失败或目录不可写 |
| 图片占用超阈值 | 超过 `DISK_ALERT_GB` |
| 定时日报推送失败 | 配了 NapCat 但推送未成功 |

```bash
# .env
ALERT_ON_OPS_FAILURE=1    # 关掉就只记日志
```

运维告警与"员工录用失败"是两条独立通道，各自的节流互不影响（运维 5 分钟，
失败告警按 `ALERT_MIN_INTERVAL_S`）。

另外建议用 NAS 自带监控或 Uptime Kuma 盯 `http://NAS的IP:8080/health/ready`：
容器整个挂掉时，服务内部的告警也发不出去。

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
- [ ] **图片有第二份**：`IMAGES_MIRROR_DIR` 已配（或在存储空间开了快照）
- [ ] 容器日志已限流（compose 里的 `logging` 段），`docker logs` 不会无限长
- [ ] 运维告警能收到：手动删掉一个备份文件后跑一次 `python -m server.backup` 验证
- [ ] `PRAGMA journal_mode` 输出为 `wal`（data 目录在网络盘上时改为 `DELETE`）
- [ ] 后台「操作日志」页能看到本次登录记录
- [ ] `ENABLE_SCHEDULER=1`，且「每日日报」页能看到当天日报
- [ ] NapCat 已部署，HTTP 服务端 Token 已填进 `NAPCAT_TOKEN`
- [ ] 后台点「重推此日报」，QQ 群收到且徽标变「已推送」
- [ ] 配了群内指令：NapCat 上报地址指向 `/api/v1/bot/onebot`，群里发「汇总」私聊能收到
- [ ] 检查 `/health/ready` 返回 200
- [ ] 上线前已记录本次镜像 tag：`docker images psluyong-lite`（回滚要用，见第五节）
- [ ] **每月一次恢复演练**：`docker exec psluyong-lite python -m server.backup --restore <最近一份备份文件> --rehearse` 输出「彩排通过」且打印各表行数——只校验不落盘，练的是"备份真的能恢复"（步骤见第五节 5.3）

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
| extra_hosts | 无 | `host.docker.internal:host-gateway`（容器内访问宿主机 13000 的 NapCat） | 功能性差异保留；根 compose 的 NapCat 走局域网 IP（见第三节 3） |
| 镜像 tag | `psluyong-lite:${PSLUYONG_IMAGE_TAG:-latest}` | 同左 | **已收敛为同款**（原两处写死 `latest`，即 B7） |
| 日志限流 / 资源限制 | json-file 10m×3 / 2 核 1G | 同左 | 一致，无漂移 |

## 七、密钥轮换（泄露响应）

`.env` 里四个值一旦泄露（截图、误提交、交接不清），按下面顺序换，**换完必须重启生效**。
2026-10-24 的真实事故就是这么处理的：`.env.bak.ci`（复现「CI 没有 .env」时的备份）被
`git add -A` 带进提交并推到公开仓库；即使 force-push 重写历史，旧提交 SHA 在 GitHub 上
仍能被直接访问——**删除历史不等于止血，轮换才是**。

| 键 | 影响面 | 换的代价 |
|----|--------|----------|
| `SECRET_KEY` | 登录 session 签名 | 全员重新登录，无其它影响。最先换这个 |
| `API_KEY` | 员工客户端提交记录 | **每台员工机的客户端配置都要同步改**，否则提交 401 |
| `ADMIN_PASSWORD` | 主管后台登录 | 见下方「注意」：改 `.env` 不会改已有管理员密码，必须走 `reset-password` |
| `NAPCAT_TOKEN` | 机器人推送鉴权 | **必须同时在 NapCat「网络配置 → HTTP 服务端」改成一致的值**，否则推送 401 |

生成新值（任意 Python 3 环境）：

```bash
python -c "import secrets;print(secrets.token_urlsafe(32))"
```

### 服务端（NAS）换法

```bash
# 1. 停服 + 备份当前 .env（备份放项目目录外，别再犯误提交的错）
docker compose down
cp .env /volume1/备份/.env.bak.$(date +%Y%m%d)

# 2. 改 .env 里对应四行（绿联文件管理默认隐藏 . 开头文件，需先开「显示隐藏文件」）
#    SECRET_KEY= / API_KEY= / ADMIN_PASSWORD= / NAPCAT_TOKEN=

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
**API_KEY 要么不换，要换就一次换干净**。短期无法逐台更新时，先只换 `SECRET_KEY`、
`ADMIN_PASSWORD`、`NAPCAT_TOKEN`，把 `API_KEY` 排到下次上门维护。

### 事故后的额外动作

1. GitHub 上的旧对象不会因 force-push 立刻消失（`raw.githubusercontent.com/<owner>/<repo>/<旧SHA>/.env` 仍可访问），
   轮换后可向 GitHub Support 提工单请求清理缓存对象；**把仓库转 Private 是最快的止血**。
2. 泄露过的值一律当「已公开」，任何环境都不再复用。
3. 查审计日志有无异常登录：后台「审计日志」页，或 `app.db` 的 `audit_log` 表。
