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
  app/models.py      AdminUser / Record / AuditLog
  app/routes/        records / stats / reports / health
  app/web/           后台页面与样式（无 CDN 依赖，可离线；亮/暗双主题）
client/              Tauri 2 + Vue 3 员工客户端
  sidecar/           Python 本地服务（调 PS、上报、离线队列）
  src/               Vue 界面
  src-tauri/         Rust 外壳
bot/                 日报生成与推送（APScheduler + NapCat）、群内「汇总」指令回调
tests/               183 个用例，覆盖 API、权限、去重、离线补传、路径穿越、CSRF、
                     回收站、分页边界、审计日志、登录限流、图片命名规则、
                     日报推送重试、区间日报、失败告警门控、
                     QQ 机器人指令（日期参数/白名单/冷却/回调鉴权/审计）
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

后台四个页面：

- **仪表盘** — 今日录用/成功/失败/活跃员工四个指标（失败数可直接点进失败明细）、
  近 7 天趋势、今日按员工/模板/合作伙伴分布、最近 8 条记录；
  底部一条「系统状态」：最后备份时间、昨日日报推送结果、图片占用
- **录用记录** — 日期区间 + 员工/合作伙伴/状态 + 关键词筛选、点行展开完整详情
  （含告警与错误原因）、缩略图灯箱、按当前筛选导出 CSV、下方折叠的回收站可一键恢复
- **每日日报** — 历史日报列表（带已推送/未推送徽标）、正文预览、一键重推
- **操作日志** — 删除、恢复、导出、重推、登录与登录失败的审计流水

全局：亮/暗双主题（默认跟随系统，可手动锁定）、全键盘可达（Tab / 回车展开详情 /
Esc 关闭弹窗）、自定义确认弹窗替代原生 `confirm`、所有异步操作有 loading 与 toast 反馈。

### 2. 起客户端（开发）

```bash
cd client
npm install
npm run tauri:dev
```

设置页填：姓名、服务器地址、API Key，并上传 PSD 模板（可一次选多个，中文名保留）。
合作伙伴名单也在设置页，一行一个。然后回「录用」页选模板、填作者/标题、点生成。

客户端界面（参考原版液态玻璃风，按精简版收敛）：

- 亮/暗双主题：顶栏一键切换，偏好随配置走，换机器不丢
- 录用页：草稿自动保存（中途关软件能接着填）、分阶段进度条、生成结果卡片（文件名 /
  同步状态 / 打开文件夹 / 复制路径）、`Ctrl+Enter` 提交、标题字数提示、空闲态快捷键速查
- 页签切换：`keep-alive` 保留各页状态与滚动位置、按标签顺序左右滑动过渡
- 记录页：近 7 天 / 近 30 天 / 全部 三档区间（服务端列表默认只给 7 天，客户端显式声明）、
  搜索过滤、区间条数与今日统计、手动刷新、缩略图懒加载、骨架屏与空态引导
- 录用页记忆：上次用的模板/合作伙伴/作者自动带出，草稿自动保存
- 设置页：PSD 拖拽上传、API Key 显隐、测试连接、`Ctrl+S` 保存、JPG 质量滑块、
  图片命名规则（`作者.jpg` / `作者_标题.jpg`，同名自动加序号不覆盖）、版本与诊断信息
- 全局：toast 通知、确认弹窗（替代原生 alert/confirm）、首启引导横幅、
  配置缺失清单（x/3）、服务器恢复后自动补传离线队列

### 3. 运行测试

```bash
.venv/Scripts/python.exe -m pytest tests -q      # 后端 + sidecar（119 个用例）
```

客户端（前端）：

```bash
cd client
npm test            # vitest：日期分组、toast 去重、草稿存取
npm run typecheck   # vue-tsc 类型检查（npm run build 会先跑一遍）
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
| 同一天重复提交 | 部分唯一索引 `(work_date, employee_name, template_name, author, title) where is_deleted=0` | 路由层"先查后插"挡不住并发；键里带员工与模板，两个人做同一篇稿、或同一个人出两个期刊模板都不会互相挡 |
| 断网怎么办 | 记录先落本地队列，下次自动补传 | 不能让员工白干 |
| 后台要离线可用 | 样式与图表全部本地实现，不引 CDN | NAS 内网可能不通外网 |
| 本地服务被网页调用 | sidecar 写请求校验 `Origin` 白名单（无 Origin 的本机程序放行） | CORS 只挡读响应，挡不住请求被 POST 执行 |
| 员工报障没日志 | sidecar/worker 日志落 `%APPDATA%\psluyong-lite\logs`（2MB×3 轮转），设置页一键复制诊断信息 | 打包后 stdout 被丢进 DEVNULL |
| 成品图命名 | 设置页可选 `作者.jpg` 或 `作者_标题.jpg`，同名自动加 `(2)` 序号 | 带时间戳的名字在文件夹里根本找不到人；但直接同名会覆盖，等于悄悄丢成品 |
| 审计 | 记录软删除、图片文件保留 | 误删可恢复（后台回收站一键恢复） |
| 后台写操作 | 会话 CSRF token（页面 meta + `X-CSRF-Token` 头） | 跨站表单冒用管理员会话；API Key 走自定义头，浏览器预检就拦掉了 |
| SQLite 并发写 | WAL + `busy_timeout=5000` + `synchronous=NORMAL` | 8 线程 waitress 下不设就是偶发 `database is locked`（data 在 NFS/SMB 上要改回 `DELETE`） |
| 操作可追溯 | `audit_logs` 表 + 后台「操作日志」页 | 容器日志会轮转，"这条谁删的"必须查得到 |
| 系统自身状态看不见 | 仪表盘底部条：最后备份 / 昨日日报推送 / 图片占用 | 业务指标再好看，备份挂了、日报没推也白搭 |
| 后台安全响应头 | `nosniff` / `X-Frame-Options` / `Referrer-Policy` / `Permissions-Policy` | 防点击劫持与 MIME 嗅探；CSP 待内联脚本全部外置后再上 |
| 登录被暴力猜 | 同来源 5 分钟内失败 5 次即 429 + 审计留痕 | 内网端口往往整层楼可达，不能裸奔 |

## 部署

见 [docs/DEPLOY.md](docs/DEPLOY.md)：NAS Docker 部署、客户端打包、QQ 机器人配置。

## 已知限制

- **PS 自动化仅 Windows**：非 Windows 环境 `core` 可 import 但不具备生成能力（已优雅降级，便于在 CI 里跑测试）。
- **PS 必须在本机运行**：方案是"调用员工机器上已打开的 PS"。若员工机器装不了 PS，需要改成集中生成服务器（架构改动）。
- **sidecar 无鉴权**：只绑 `127.0.0.1` + CORS 白名单。本机其他程序仍可调用，单机桌面场景可接受，多用户共用电脑需要补 token。
- **SQLite 单写者**：小团队够用；将来并发上千再换 MySQL，届时 `models.py` 里的 `sqlite_where` 部分索引要换成对应语法。
