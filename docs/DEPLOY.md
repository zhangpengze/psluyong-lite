# 部署手册

## 一、服务端（NAS Docker）

### 1. 准备目录

在绿联 NAS 的共享文件夹里建一个目录，例如 `/volume1/docker/psluyong-lite`，
上传整个项目（或只上传 `Dockerfile`、`docker-compose.yml`、`requirements.txt`、`server/`、`bot/`、`core/`）。

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

### 2. 启动

```bash
docker compose up -d --build
docker compose logs -f server
```

看到 `服务启动 http://0.0.0.0:8080` 即成功。浏览器打开 `http://NAS地址:8080`。

### 3. 资源与备份

- 容器限制：2 核 / 1G 内存，实测空闲约 80MB。
- 数据全部在 `./server/data`：
  - `app.db` — SQLite 数据库
  - `images/年/月/日/` — 成品图与缩略图
  - `templates/` — PSD 模板
  - `reports/` — 每日日报 Markdown
- 备份：直接打包 `server/data` 目录即可，无需导库。

### 4. 反向代理（可选）

用绿联自带反向代理指向 `127.0.0.1:8080`，并加 HTTPS。若启用 HTTPS，
在 `.env` 里设 `FORCE_HTTPS=1`，否则登录 Cookie 不会带 `Secure` 标记。

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
- [ ] 已用管理员登录，改掉默认密码
- [ ] 至少上传 1 个模板，并在客户端成功出过 1 张图
- [ ] 员工机器的 PS 版本已确认（不同版本 COM 行为略有差异）
- [ ] `server/data` 已纳入 NAS 定时备份
- [ ] `ENABLE_SCHEDULER=1` 且日报能收到
- [ ] 检查 `/health/ready` 返回 200
