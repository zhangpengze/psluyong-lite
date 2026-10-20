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
  - `app.db` — SQLite 数据库
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

## 三、QQ 群日报

### 1. 部署 NapCat

在 NAS 上用 Docker 跑 NapCat（或已有 QQ 机器人框架），登录一个 QQ 号，
开启 HTTP 服务端（默认 3000 端口），记下 token。

### 2. 配置

`.env` 里补上：

```bash
NAPCAT_WEBHOOK=http://napcat:3000    # 与 server 同网络时用容器名
NAPCAT_TOKEN=你的token
REPORT_GROUP_ID=123456789            # 目标群号
DAILY_REPORT_HOUR=18
DAILY_REPORT_MINUTE=0
ENABLE_SCHEDULER=1
```

重启容器。每天 18:00 自动推送，日报同时存档到 `server/data/reports/`，
后台「每日日报」页可回看。

### 3. 补跑某天

```bash
curl -X POST http://NAS:8080/api/v1/reports/run \
  -H "Cookie: session=..." \
  -H "Content-Type: application/json" \
  -d '{"date":"2026-10-19","push":true}'
```

或在后台「每日日报」页点开对应日期后手动触发（需管理员账号）。

### 未配置时

`NAPCAT_WEBHOOK` 为空时不会报错：日报照常生成并存档，只是不推送。
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
- [ ] `ENABLE_SCHEDULER=1` 且日报能收到
- [ ] 检查 `/health/ready` 返回 200
