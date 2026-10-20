# 期刊录用管理系统（精简版）

PS 模板批量生成 + 工作日志汇总。员工在任意 Windows 上填两组文字一键生成，
记录与成品图落到 NAS，主管用浏览器看板检索汇总，每天定时把日报推到 QQ 群。

> 从 `E:/psluyong` 的做法里提炼而来：保留跑通过的 PS COM 引擎与加固逻辑，
> 去掉账务（金额/收款方式/余额）等与本项目无关的部分。

## 目录

```
core/                PS 自动化引擎（纯 Python，无 UI 依赖）
  ps_connector.py    COM 连接、文本替换、导出
  ps_service.py      服务封装 + COM 错误人话翻译 + 零匹配治理
  runner.py          一次生成的完整编排
server/              Flask 后端 + 主管后台（Jinja2）
  app/models.py      AdminUser / DictItem / Template / Record
  app/routes/        records / templates / dicts / stats / reports / health
  app/web/           后台页面与样式（无 CDN 依赖，可离线）
client/              Tauri 2 + Vue 3 员工客户端
  sidecar/           Python 本地服务（调 PS、上报、离线队列）
  src/               Vue 界面
  src-tauri/         Rust 外壳
bot/                 日报生成与推送（APScheduler + NapCat）
tests/               39 个用例，覆盖 API、权限、去重、离线补传、路径穿越
```

## 快速开始

### 1. 起后端

```bash
cp .env.example .env          # 填 SECRET_KEY / API_KEY / ADMIN_PASSWORD
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m server.run
```

打开 http://127.0.0.1:8080 ，用 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 登录。

首次进入请依次：
1. 把 `API_KEY` 发给员工
2. 员工在自己客户端里上传 PSD 模板、维护合作伙伴名单（模板与字典都不在后台维护）

### 2. 起客户端（开发）

```bash
cd client
npm install
npm run tauri:dev
```

设置页填：姓名、服务器地址、API Key，并上传 PSD 模板（可一次选多个，中文名保留）。
合作伙伴名单也在设置页，一行一个。然后回「录用」页选模板、填作者/标题、点生成。

### 3. 运行测试

```bash
.venv/Scripts/python.exe -m pytest tests -q
```

## 生成链路

```
员工点「生成图片」
   → sidecar 校验参数（模板是本机上传的 PSD，不联网）
   → PS：打开 → 递归遍历文本图层 → 精确替换两个占位符 → 导出 JPG
   → 校验导出文件真实落盘（COM 返回成功 ≠ 文件存在）
   → multipart 上报记录 + 成品图
   → 上报失败则进本地队列，下次生成时自动补传
```

生成日志字段（已去掉账务）：

`work_date`、`employee_name`、`template_name`、`partner_name`、`author`、`title`、
`image_path`、`client_machine`、`ps_version`、`status`、`error_message`、`warnings`、`processing_ms`

## 关键设计

| 问题 | 做法 | 原因 |
|---|---|---|
| PS 报错员工看不懂 | HRESULT 表 + 关键字规则翻译成中文动作 | 员工不需要知道 0x800706BA 是什么 |
| COM 返回成功但没生成 | 导出后校验文件存在且大小 > 0 | 假成功比失败更坑 |
| 没有图层被替换 | 零匹配直接判失败 | 否则导出一张跟模板一样的图，员工以为做完了 |
| 同一天重复提交 | 部分唯一索引 `(work_date, author, title) where is_deleted=0` | 路由层"先查后插"挡不住并发 |
| 断网怎么办 | 记录先落本地队列，下次自动补传 | 不能让员工白干 |
| 后台要离线可用 | 样式与图表全部本地实现，不引 CDN | NAS 内网可能不通外网 |
| 审计 | 记录软删除、图片文件保留 | 误删可恢复 |

## 部署

见 [docs/DEPLOY.md](docs/DEPLOY.md)：NAS Docker 部署、客户端打包、QQ 机器人配置。

## 已知限制

- **PS 自动化仅 Windows**：非 Windows 环境 `core` 可 import 但不具备生成能力（已优雅降级，便于在 CI 里跑测试）。
- **PS 必须在本机运行**：方案是"调用员工机器上已打开的 PS"。若员工机器装不了 PS，需要改成集中生成服务器（架构改动）。
- **sidecar 无鉴权**：只绑 `127.0.0.1` + CORS 白名单。本机其他程序仍可调用，单机桌面场景可接受，多用户共用电脑需要补 token。
- **SQLite 单写者**：小团队够用；将来并发上千再换 MySQL，届时 `models.py` 里的 `sqlite_where` 部分索引要换成对应语法。
