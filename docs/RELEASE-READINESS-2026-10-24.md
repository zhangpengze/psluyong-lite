# 上线就绪评估 · 深度代码审计（2026-10-24）

> 审计方式：架构 / 后端 / QA / 运维 / 客户端 五方并行，**逐文件读源码 + 运行时取证**（不是读 README）。
> 基线：`main @ 80fdabf`，工作区干净。上一次审计（docs/AUDIT-2026-10-24.md）已闭环 7 项 blocking，本轮**不重复**那些结论。
>
> ## 总判定：**不能按现状对外全量上线；可以内网 3 人灰度。**
> 宏观结构是健康的（测试、分层、备份、回滚、密钥治理都在线），但**"数据会不会悄悄丢"这一层还有 7 个洞**——它们的共同特征是：**出事了界面上显示"成功"**。
> 建议先修 P0（约 6~7 人日），再灰度 1 周，然后全量。

---

## 一、维度评定

| 维度 | 现状 | 评定 |
|---|---|---|
| 测试与回归 | 557 用例全绿（P0-1/2/3/4/5 修复后复测，0 failed / 0 skipped）；前端 vue-tsc + 39 例通过；CI ubuntu+windows+client 三 job；覆盖率 89% | 达标 |
| 代码结构 | 五棵源码树 0 文件 >300 行；routes 零 DB 访问；service 层 10 模块 | 达标 |
| 安全基线 | 无硬编码密钥；`.env` 不入库 + CI 门禁；non-root；CSRF（含登录表单）/POST logout/限流/路径穿越/通配符转义 | 达标 |
| 数据可靠性 | WAL + 部分唯一索引、软删除、热备+彩排恢复、图片镜像 | **有洞（P0-4/5/6/7）** |
| 推送可靠性 | 响应体判定 + 幂等闸门 + 次日终版 + 缺口巡检 | P0-1/2 已修；仍缺「机器人失联主动探测」 |
| 客户端健壮性 | 配置已原子写 + 备份自愈；上报已与前端轮询解耦并有启动对账；进程树无治理 | **主体已修（P0-4/5/6 ✅，仅剩进程树治理）** |
| 可运维性 | 状态卡、审计流水、健康探针、回滚章节 | 基本可用，缺客观监控 |
| 可交付性 | 无自动更新、无安装包签名、无员工手册、无诊断包 | **缺件** |

---

## 二、已经过硬的部分（可以放心）

1. **测试基线是真的**：本轮 `pytest tests -q` → 494 passed、0 skipped；`--cov` 实测 **4146 stmts / 437 miss = 89%**（与交接文档一致，未虚报），唯一 0% 的是 `server/run.py`、`server/wsgi.py` 两个纯装配入口（可豁免）。`test_rotate_secrets`（11 例）等新增用例覆盖了运维脚本。
2. **CI 是真跑起来的**：`.github/workflows/tests.yml` 三个 job（ubuntu/windows/client），带密钥文件门禁、300 行门禁、失败注解，可直接给外部排障。
3. **写路径的并发兜底是对的**：`models.py:124-133` 部分唯一索引 `(work_date, employee_name, template_name, author, title) WHERE is_deleted=0 AND status='success'`，路由层先查后插 + 数据库兜底 + `IntegrityError` 清理孤儿图，这条链路设计正确。
4. **假成功治理在 PS 导出处有做**：导出后不是只信 COM 返回值（审计时口径只有 `exists() && size>0`，见 P0-7；现已补成金标准校验）。
5. **审计与可追溯**：`audit_logs` 覆盖增删改导出推送登录；容器日志轮转限流（10m×3）；sidecar 日志 2MB×3。
6. **迁移有防线**：`migrations.py` 补列 + 回填默认值 + 删废弃索引（专门处理"代码改了线上还是 409"这个坑）。
7. **密钥泄露响应已机械化**：`scripts/rotate_secrets.py` + DEPLOY 第七章，能一键轮换四键并拒绝把备份写进仓库。

---

## 三、P0 阻断（7 项，全部本轮实证）

> 共同点：**失败被显示成成功**。这类 bug 不会告警、不会丢单报错，只会让主管看到一张漂亮的假报表。

**修复进度**（2026-10-24 当日落地，用例 494 → 604，全绿；前端 vue-tsc + 39 例通过）：

| # | 状态 | 落地要点 | 钉死的回归用例 |
|---|---|---|---|
| P0-1 推送假成功 | ✅ 已修 | `_verify_onebot_response` 解析 `status`/`retcode`；4xx 与 body 失败均不重试；徽标只在真成功时置 `pushed`；手动「只生成不推送」不再覆盖历史徽标 | `test_push_ok_http_but_failed_body_is_failure` 等 5 例（变异验证：改回旧逻辑 3 红） |
| P0-4 队列静默丢记录 | ✅ 已修 | 拒绝码分级：`DISCARD_STATUS=400/409/422` 删、`BLOCKING_STATUS=401/403/404` 保留并标 blocked、429 保留停本轮；`/status` 暴露 `pending_blocked` + 原因；主窗底栏显示「其中 N 条因配置受阻」 | `test_auth_or_address_error_keeps_records[401/403/404]` 等 10 例（变异验证：改回"所有 4xx 一律删" 6 红） |
| P0-3 机器人回调全开放 | ✅ 已修 | 密钥缺省 fail-closed（`BOT_ALLOW_INSECURE=1` 才放行）+ QQ/群双白名单（留空=谁都不能用）+ 前置过滤（心跳/通知/私聊不进线程）+ 60s/120 次限流 + 启动告警；顺带修掉"私聊失败把报表贴到日报群"的跨群泄露 | `test_handle_event_whitelist_empty_denies_everyone`、`test_onebot_callback_empty_secret_denied_by_default` 等 7 例（变异验证 4 红） |
| P0-2 日报漏推/18 点后记录不可见 | ✅ 已修 | 18:00 快报 + 次日 08:30 终版巡检（`bot/report_patrol.py`，只补昨天、只在数据变了时发）；幂等闸门用 `stats_fingerprint` 比**数据**而非正文（正文末尾有生成时间，比正文等于永远「变了」）；更早的缺口只发一条告警列日期，不刷群；仪表盘改按推送时点算「该推哪天」（原来固定看昨天，18:00 失败要等 24 小时才变黄）；删掉「重启一小时内仍补跑」的错误注释 | `test_report_patrol.py` 13 例 + `test_scheduler.py` 4 例 + `test_system_status.py` 6 例（变异验证：去闸门 7 红／指纹与数据无关 7 红／巡检不补推 9 红／缺口放宽 9 红／仪表盘固定昨天 5 红／不注册巡检 job 6 红） |
| P0-5 客户端配置损坏即丢配置 | ✅ 已修 | 写入改 `tmp + os.replace`（含 Windows 共享冲突重试）；保存前留 `config.json.bak`，且**只有当前文件能解析时才刷新**；读坏文件先改名 `config.broken-<ts>.json` 留档→试 `.bak`→最后才回默认值并把这件事报给界面；逐字段纠偏（坏字段只影响自己）；读-改-写进同一把 `RLock`；「暂时读不到」与「真损坏」分开处理（前者绝不回写）；前端位置记忆写完回填本地快照，不再每 10 秒重写一遍 | `test_client_config.py` 17 例 + `test_sidecar.py` 3 例 + 前端 3 例（变异验证：6 处逻辑改回旧行为，分别 2/1/1/2/2/2 条转红） |
| P0-6 上报依附前端轮询、无对账 | ✅ 已修 | 结算从 `GET /export/status` 下沉到 `client/sidecar/settler.py`：三条路（前端轮询 / 后台每 15 秒 / 下次启动对账）都调同一幂等函数，以 `result.json` 为「已结算」标记；`unsettled = status.json 有 + result.json 无`；服务器不可用时记录进离线队列也算结算（图片路径一起留下）；队列也写不进去才不标结算并回「图片已生成、记录没存住」；409 `duplicate` 按「早已上报成功」收尾（进程被杀在写标记前）；`/status` 暴露 `unsynced`，主窗底栏显示「待补报 N 次」；顺带修 `state=error`（worker 抛错）以前完全不上报 | `test_settler.py` 15 例 + `test_sidecar.py` 2 例（变异验证：关扫描 3 红／去幂等 2 红／取消也上报 1 红／409 不算已报 1 红／队列失败也标结算 1 红／不下发 unsynced 1 红／main 不启动结算 1 红） |
| P0-7 导出只验"存在+非空" | ✅ 已修 | 新增 `core/export_check.py`（大小下限 / 能否解码 / 尺寸对模板 / 空白极值 / 色彩模式）+ `core/ps_preflight.py`（导出前读 `Width/Height` 当基准、CMYK→RGB、16bit→8bit，失败降级成 warning 不拦流程）；`runner.run_export` 改为**先导出到 `X.psluyong-tmp.jpg`、校验通过才 `os.replace` 成交付名**——半截文件再也拿不到合规文件名；「像是成品但不对」（全白/尺寸不符/CMYK）另存 `X.校验未通过.jpg` 留证据，「根本不成立」（<2KB / 解码失败）直接删；取消或崩溃留下的临时文件按 1 小时阈值清理 | `test_export_check.py` 14 例 + `test_run_export.py` 8 例 + `test_ps_service.py` 8 例（变异验证：去掉校验 4 红／直接写交付名 1 红／空白阈值失效 1 红／解码失败算通过 2 红／CMYK 放行 1 红／不比对尺寸 1 红／不清理残留 2 红／不转 RGB 1 红） |

**⚠️ 升级注意**：P0-3 是行为反转。已部署环境若没配 `BOT_CALLBACK_SECRET` / `BOT_ADMIN_QQ_IDS`，升级后群内指令会"突然不理人"——按 `docs/DEPLOY.md` 三.5 补两个键再重启（排障命令也写在那节）。

### P0-1 推送只认 HTTP 200，不看 OneBot 响应体 → 日报"假成功"，且"未推送"告警结构上永不触发
- **证据**：`bot/report.py:133-148`（`if resp.status_code == 200: return True, "推送成功"`，从不解析 body）；`bot/scheduler.py:85-86`（兜底告警的判断条件是 `not result["pushed"]`，而它恒为 True）；`bot/report.py:185-191` 把 `pushed: true` 写进 `reports/<day>.status.json`；`server/app/status.py:162-163` 仪表盘据此显示"已推送"。
- **实证**（本轮）：注入 `HTTP 200 + {"status":"failed","retcode":100,"message":"GROUP_NOT_FOUND"}` → `push_to_group()` 返回 `ok=True "推送成功"`。
- **后果**：群号填错、机器人被踢、Token 失效、消息超长被风控——**全部显示成功**。群里一条消息都没有，后台绿点，告警永不响。
- **修法**：解析 `resp.json()`，要求 `status=="ok"` 或 `retcode in (0,1)`，否则带 `retcode/message` 判失败；`save_push_status` 只在 body 校验通过后写 `pushed=True`；4xx 不重试。**工作量 0.5 人日**（含 2 条用例：`200+failed`、`200+retcode≠0`，现有 `_FakeResp` 没有 `.json()`，所以这个 bug 测不出来）。

### P0-2 日报漏推既发现不了也补不回来；18:00 之后录入的记录永远进不了任何一次群报
- **证据**：`bot/scheduler.py:91-108` 用默认 `MemoryJobStore`，注释写"服务重启错过时间点，一小时内仍补跑"——**错误**。`misfire_grace_time` 只对"进程活着但被阻塞"有意义；进程重启后 job 不存在，按 `trigger.get_next_fire_time(None, now)` 算的是**下一个未来时刻**。
- **实证**：`CronTrigger(hour=18)` 在 `2026-10-24 18:05 +08:00` 求下一次 fire → **`2026-10-25 18:00`**（当天不补）。
- **叠加缺陷**：① `status.py:149-168` 只检查**昨天**的状态文件，今天没推要等 24 小时才变黄点；② `run_daily` 只统计 `work_date == day`，而客户端 `routes_export.py:133` 用本机当天日期 → 18:00 后加班补录的记录**不落在任何一次已推送的日报里**，网页日报也是 18:00 生成的那版，同样不含。
- **修法**：① DB 或状态文件做幂等闸门 `report_runs(day, state, attempts, content_hash)`，job 先查后推；② 加"次日 08:30 巡检 + 缺失自动补推"job；③ 群指令加「补推 YYYY-MM-DD」；④ 日报口径改为 18:00–次日 09:00 滚动或加一份"终版"；⑤ 把误导注释删掉。**工作量 1.5 人日**。
- **落地（2026-10-24）**：状态文件扩成 `{pushed, pushed_at, fingerprint, attempts, last_error, final, final_at}`；闸门判据是「已推成功 + 同一天 + **数据**指纹一致」才跳过；巡检在 `bot/report_patrol.py`（拆出 `bot/report_render.py`，让 `report.py` 留在 277 行）。**三个实现层面的判断值得记下来**：① 指纹必须算数据、不能算正文——正文末尾有「生成时间：」，比正文等于每天早上都「内容变了」，终版会天天重复推；② 缺口判据必须是「生成过日报 or 库里有记录」，否则新部署那天往前看 7 天空白会被当成 7 天事故；③ 只自动补昨天、更早的只告警——宕机一周时群里不该突然冒出 7 份旧日报（这条是加「回看 N 天」时才暴露的取舍，原修法里没有）。群指令「补推」未做：后台「重推此日报」已支持任意日期，够用。

### P0-3 QQ 机器人回调默认全开放：无凭据可触发 + 任意群 + 空白名单放行 + 每条上报起线程
- **证据**：`routes/bot.py:26-38`（`secret` 为空 → `return True`）；`bot/handler.py:147-149`（`return not whitelist or user_id in whitelist`，空白名单=谁都能用）；`handler.py:206-232` 全程**没有校验 `group_id` 是否等于 `REPORT_GROUP_ID`**；`routes/bot.py:75` 在**任何过滤之前**先 `threading.Thread().start()`；蓝图无条件注册（`routes/__init__.py:15`），所以每次部署都暴露该端点。
- **实证**：空 secret + 空白名单 + `group_id=999999`（非日报群）+ 无任何请求头 → `POST /api/v1/bot/onebot` 返回 200，handler 照做并私聊回复了该 QQ（日志 `群内指令触发: user_id=888888 group_id=999999`）。
- **后果**：机器人被拉进任何一个群，随便谁发「汇总」就能拿到**含员工姓名、作者、标题、失败原因**的完整报表；每条群消息（含心跳）都开一个线程，可被刷到线程耗尽。
- **修法**：secret 为空时 fail-closed（启动 warning + 端点 403）；空白名单=谁都不能用；加 `BOT_ALLOWED_GROUP_IDS`；把 `post_type/message_type` 前置过滤放到 `_spawn` 之前；该路由接 `ratelimit.hit`。**工作量 0.5 人日**。

### P0-4 离线队列把 401/403/404/429 一律当"业务拒绝"删除 → 换 Key / 地址填错的瞬间静默丢光积压
- **证据**：`client/sidecar/queue.py:172-174`（非 5xx、非 `status==0` → `logger.info(...)` + `remove(path)`）。注释把 4xx 一律解释成"服务端明确不要这一条"。
- **实证**：队列各放 2 条，让 `submit_record` 分别抛 403 / 401 / 429 / 409 / 400 → **每次队列都 2 → 0，`dead: 0`**，只有一行 `logger.info`。
- **后果**：API Key 轮换后忘了同步某台机器（本项目 2026-10-23 刚发生过密钥泄露轮换）、员工手抖改错 Key、服务器地址指向旧版本没有该接口（404）——**只要此时队列里有积压，一条不剩**。界面上"待补传 3 条"直接变 0，看起来像补传成功。
- **附带**：`retry_dead()`（`queue.py:104-126`）把死信搬回 pending 后立即 flush（`routes_shell.py:73-81`），在鉴权失效场景下实测会**删掉唯一的副本**。
- **修法**：只有 400/409/422 删；401/403/404/429 保留并标 `blocked`，`/status` 暴露 `pending_blocked`，底栏提示"因鉴权失败 N 条积压未上传"；`retry_dead` 改为先复制、成功后才删。**工作量 0.5 人日**。

### P0-5 客户端 `config.json` 非原子写 + 损坏即回退默认值并立刻写回 → 员工配置永久丢失；一种形态下全接口 500
- **证据**：`client/sidecar/config.py:277-285`（`CONFIG_PATH.write_text(...)`，先 truncate 再写；同一项目 `worker.py:34-39` 已经会 `tmp + os.replace`，配置这里**反而没有**）；`config.py:262-266`（解析失败只 `logger.error` 然后 `return ClientConfig()`——空 api_key、默认服务器地址、空员工名，**不备份不提示**）；`config.py:269`（`ClientConfig(**raw)` 无类型校验）；写者不止一个：`routes_status.py:80-98` 的 read-modify-write 无锁，而前端每个窗口都会跑 `startAfterReady`（`App.vue:107-142`）+ 每窗 10s 一次位置记忆（`useWindowControls.ts:142`）。
- **实证**：写一个截断的 config → `load_config()` 返回 `api_key='' server_url='http://127.0.0.1:8080' employee=''`；随后 `save_config()` 把默认值**写回磁盘**（真实 Key 与 NAS 地址丢失）；写 `{"output_dir": 123}` → `AttributeError: 'int' object has no attribute 'startswith'`，该形态下 `/status`、`/config`、`/export` 全部 500，界面只剩"本地服务连接失败，请重启"，**重启无效、界面里修不回来**，只能手删 `%APPDATA%\psluyong-lite\config.json`。
- **修法**：`tmp + os.replace` + 进程内锁；load 时整段 try 包住并逐字段校验；坏文件改名 `config.broken-<ts>.json` 并保留 `config.json.bak` 自动回滚；位置记忆改到关窗时写一次。**工作量 1 人日**（这是唯一"发到 100 台后只能逐台远程"的缺陷，且当前**没有自动更新兜底**）。
- **落地（2026-10-24）**：按 `config_schema.py`（字段/默认值）/ `config_validate.py`（提交校验 + 读取纠偏）/ `config.py`（原子写 + 备份 + 自愈）三层拆开，各自 <300 行。自愈链路：坏文件 → 改名留档 → `.bak` 能解析就用它并写回 → 否则默认值 + `/status.config_recovered` 让界面弹一次"请在设置里核对地址和 Key"。**额外发现并修掉两个本报告没写到的形态**：① `os.replace` 撞上并发读会抛 `PermissionError`（Windows 共享冲突）→ 换名重试 6 次、读重试 4 次；② 若把这种**瞬时**读失败当成"文件坏了"，一次并发读就会把好文件留档并回默认值写盘——所以"暂时读不到"单独一个分支，只撑内存、绝不写盘，`/config` 保存直接返回 503「稍后重试」。前端位置记忆写完回填本地快照（实测改回旧写法：70 秒内同一窗口写 7 次完全相同的坐标）。

### P0-6 记录上报挂在"前端轮询"这根线上，且启动时无对账 → 图在硬盘里、记录永久没有
- **证据**：上报是实现为 `GET /export/status` 的**副作用**（`client/sidecar/routes_export.py:157-199`），唯一的轮询方是前端 `useExportTask.ts:70-88`；轮询一抛错就 `reject` 并停表（`:82-84` → `:139-145`），**worker 仍在跑**；全仓 grep 确认 `executions/` 只在 `executor.py`/`routes_export.py` 出现，**没有任何"已完成但未上报"的扫描或补偿**。
- **后果**：生成结束瞬间 sidecar 抖一下、休眠唤醒、员工点"仍然退出"或客户端崩溃 → PS 已把图写进输出目录、`status.json` 已是 done，但后台**没有这条记录**：主管看到的是"这个员工今天没干活"，图却躺在文件夹里，且**永远补不回来**。
- **修法**：上报下沉到 sidecar（worker 写完 `status.json` 自己提交，失败进队列），`/export/status` 只读；启动对账扫 `executions/*/status.json`（`done && !result.json` → 补报或列成"待补报"清单）。**工作量 1.5 人日**（可与 P0-4 合并做）。

**✅ 落地（2026-10-24）**：新增 `client/sidecar/settler.py`（结算编排）+ `reporter.py`（字段组装与上报）。
`GET /export/status` 不再承担上报副作用，只负责「结算一次或读缓存」；sidecar 启动时 `main()` 拉起
`start_settler()`——先做一次**启动对账**（后台线程，避免连不上服务器时把启动卡死几十秒），
之后每 15 秒兜底扫一遍 `executions/`。判定口径：`status.json` 存在且 `result.json` 不存在 = 待结算；
「服务器已收下」或「进了离线队列（含图片路径）」都算结算成功，两者都失败才不写标记、下一轮重试。
上报与 `_client` 晚绑定同样走 `_client_lazy`，测试 monkeypatch 仍然生效。
运行时验证（真实 Flask + 真文件协议）：杀进程后重启 → 1 条补报进服务器、`unsynced` 1→0、轮询不再重复提交；
断网 → 队列 1 条且图片路径保留、`result.json.queued=true`、再结算无新增；跑到一半（无 `status.json`）→ 不结算也不报假记录；
取消 → 一条失败记录都不产生；二次启动 → 新增提交 0。

### P0-7 导出校验只有"存在 + 大小 > 0"：尺寸错、全白、半截图、CMYK 偏色都能过
- **证据**：`core/ps_service.py:261-269`（唯一的产出校验）；`ps_service.py:245-257`（SaveAs 前不读 `document.Mode` / `BitsPerChannel`，不设 `EmbedColorProfile`）；`executor.py:181-185`（取消直接 `popen.kill()`）。
- **后果**：① 1×1 图、全白图、被裁掉一半的图都记成 `success`，计入"今日出图"与成功率；② **半截文件有确定成因**：员工点「取消生成」而 worker 正在 SaveAs → 输出目录里留下一个文件名完全合规的残缺 `作者_标题.jpg`，重试会生成 `(2)`，交付时按名字拿错；③ 非 RGB/16bit 模板静默产出偏色 JPG。
- **修法**：导出后读回 `document.Width/Height` 与模板比对；用 Pillow 判空白（直方图/极值）+ 文件大小区间；导出前校验 `Mode=="RGB"` 与 `BitsPerChannel==8`，需要时 `ConvertProfile`；先写 `.part` 再 rename；取消时删除本次未完成产物。**工作量 1 人日**（配合"金标准图"可自动化，见 5.4）。

**✅ 落地（2026-10-24）**：临时名用 `X.psluyong-tmp.jpg` 而不是 `.part`——PS 是按**扩展名 + SaveOptions** 决定存什么格式的，
改成 `.part` 它会存成无格式裸文件或直接报错。校验分两层，判据按"有没有证据价值"分：
**留证据的**（`keep=True`：尺寸对不上、几乎是纯色、色彩模式可疑）另存 `X.校验未通过.jpg`，人可以打开核对；
**没证据价值的**（<2KB、解码失败、全是垃圾字节）直接删——留着只会占地方并让人误以为是好的。
导出前体检（`ps_preflight`）读 `document_info()` 拿到真实尺寸当基准，**这才是"模板尺寸"的可靠来源**——
拿模板文件名或配置里的尺寸去比，一旦用户换过模板就会天天误报。CMYK/16bit 自动转 RGB/8bit 并带一条 warning
（转完颜色会有细微差异，得让人知道），转换失败不拦流程（会由后面的校验兜住，而不是让员工看到一条看不懂的 COM 错误）。
`expect_size` 允许宽高互换：模板横竖版切换是合法操作，不该被当成尺寸错。
残留清理（`sweep_stale_temps`）只删 1 小时前的临时文件、只删名字里带 `.psluyong-tmp` 的——正在跑的导出不能被顺手删掉。

运行时验证（真 Pillow 渲染走 `run_export`，6 个场景）：正常 → 只有交付名、临时文件消失；全白 → 失败且留 `X.校验未通过.jpg`；
半截 JPEG → 失败且**输出目录为空**；尺寸 400×300 对 800×600 → 失败并留证据；CMYK 模板 → 成功且 `Mode` 变 3 + warning 文案；
残留临时文件 → 被清理后正常出交付图。

**P0 合计约 6.5 人日**（含用例与回归）。

---

## 四、P1 重要（灰度期就要修，别等全量）

### 服务端
1. **状态卡一行异常打挂整张卡**：`status.py:112-123` `_database_row` 直接 `Record.query...count()`，**没有 try/except**；同文件 `_dir_usage`/`_backup_row` 都做了降级。DB 一锁（WAL 下高峰期可能）→ `/api/v1/stats/overview` 与仪表盘一起 500。
2. **`/health/ready` 只做 `SELECT 1`**（`health_service.py:14-16`）：磁盘满 / data 卷只读时写入全 500，readiness 依然 200；也不覆盖调度器存活。而 compose healthcheck 用的正是它——`restart: unless-stopped` **不会**因 unhealthy 重启，探针失败只是"显示不健康"。建议加 `DATA_DIR` 可写探测 + `scheduler.get_jobs()` + 最近 job 心跳。
3. **告警是单通道且互相吞**：`report_alert.py:77-112` 全局单哨兵 + 300s 窗口，`scheduler.py:35-62` 三个运维告警连发时**只有第一条能出去**，后两条连日志都没有；而通道就是被监控的 NapCat 本身——NapCat 一挂，日报与告警**同时全静默**。建议节流键改 `(kind,title)`、被抑制的落日志、加本地告警文件 + 启动补偿推送。
4. **`POST /api/v1/records`（64MB 上传）与 bot 回调无限流**（`ratelimit.py` 只被登录使用）——已知取舍，但 bot 路由既然要开，就该接上。
5. **多进程/多副本会重复推日报**：`wsgi.py:5-6` 在 import 时启动调度器；APScheduler 无分布式锁。当前 compose 单进程跑没事，一旦改成 `--scale` 或 gunicorn 多 worker 就是 N 条重复日报。用 P0-2 的幂等闸门 + `SCHEDULER_OWNER` 环境变量兜住。
6. **正文长度无上限**（`bot/report.py:46-98`）：伙伴数 × 模板名（200 字符）可轻易过万字符，超 QQ 单条上限即整条推送失败（且因 P0-1 被误报成功）。渲染后硬截断并提示"完整版见后台"。
7. **`status.py` 每 30s `rglob("*")` 全量遍历 `IMAGES_DIR`**，几万张图时仪表盘明显卡；`_CACHE` 全局 dict 多线程无锁。
8. **Token 明文过 HTTP**（`report.py:128-135`，本机 `.env` 实测是 `http://222.222.0.111:13000`）：拿到即可冒充机器人往任何群发消息。至少改内网 HTTPS 或来源 IP 白名单。

### 客户端
9. **PS 冷启动重试预算约 6 秒**（`ps_connector.py:68-73,90-126`：3 次循环 + `sleep(2)`），而 `Dispatch` 在 PS 未运行时会静默拉起 PS，冷启 15~40s → **员工第一次点生成必然失败**，还会入库一条 failed 并触发失败告警。`ps_path` 参数（`ps_service.py:47-61`）赋值后**从未使用**，是装饰品。
10. **单占位符没命中仍然 success**（`ps_service.py:189-203` 只在"全部规则都没命中"时判失败）：模板里 `{{标题}}` 被误删 → 成品图没标题，照样 success 交付。
11. **单实例开关是 toggle**（`lib.rs:746-753`）：主窗已显示时双击桌面图标 → 窗口被**隐藏**，员工以为没启动。建议第二个实例只 `show + focus`。
12. **僵尸 sidecar / 端口占用**：`lib.rs:873-885` 只 kill 一级子进程，worker 是孙进程；sidecar 无父进程看门狗；8765 被占时新 sidecar 静默死在 DEVNULL，前端仍连上僵尸 → `busy` 可能永远 true、按钮永远 409。建议 Job Object（`KILL_ON_JOB_CLOSE`）+ 父 PID 看门狗 + 端口失败可见化。
13. **生成成功后 `saveConfig` 抛异常会把结果翻成"生成失败"**（`useExportTask.ts:119-140` 成功分支与 catch 在同一个 try）：图已生成、记录已上报，员工看到失败必然再点一次 → 撞 409，白跑一轮 PS。
14. **三个窗口各跑一遍 `startAfterReady`**（`App.vue:107-142`）→ 启动瞬间对同一批 pending 并发 flush；成功记录被 409 兜住，但**失败记录不参与判重**（唯一索引只约束 `status='success'`），会生成 2~3 条重复 failed，污染"失败数"。只让主窗跑 flush/轮询。
15. **"测试连接"测的是已保存的旧地址**（`AccountTab.vue:22-39` 调 `/status`，后者用 `load_config()` 的磁盘值），toast 却打印表单里的新地址 → 改了地址提示"连接成功"，保存后连不上。
16. **PS 无硬超时看门狗**：`executor.py:116-125` 进程活着就一直返回 running，`heartbeat_stale` 只用来显示；`close_document` 失败被 `logger.debug` 吞掉并置 None（`ps_service.py:96-104`）→ PS 里文档越叠越多，最终"暂存盘不足"。
17. **队列补传时图没了仍建"无图记录"**（`api_client.py:61-75` 只在 `exists()` 时才带 files），UI 还显示"已同步"；`queue` 只存路径字符串不复制文件 → 员工整理过输出目录就永久丢图。
18. **本机写操作面**：`/open-folder` 接受任意 `path`（`routes_shell.py:26-33`）；`task_id` 未做格式校验就拼路径（`routes_export.py:160` + `worker.py:36-37` 会 `mkdir(parents=True)`）。单机桌面场景危害有限，但一条正则就能堵。

---

## 五、你没想到、但上线必须有的 12 项（能力缺口，不是 bug）

> 这一节是"能跑"和"能交付给 20 个员工天天用"之间的差距。

### 5.1 员工身份与"逐人吊销"（**最该先决定的产品问题**）
现状全公司共用一把 `API_KEY`，`employee_name` 是客户端自由填写的字符串。
- 离职员工 / 换机器，**无法单独吊销**，只能全公司轮换一把 Key（要挨个通知）。
- 员工改个名字或打错字，历史统计直接断成两个人；两个人同名则被判重（去重键含 employee_name）。
- 建议：记录表加 `employee_id`（或至少 `machine_id`），服务端维护一张员工表，客户端首次注册领一把**独立 Key**；`employee_name` 从服务端下发而不是手填。

### 5.2 客户端自动更新（当前完全没有）
`tauri.conf.json` 无 `plugins.updater`、无 `bundle.createUpdaterArtifacts`，`Cargo.toml` 也没有 updater 依赖。
→ P0-5 这类配置损坏一旦发出去，你只能逐台远程删文件。内网可以用最简单的一套：静态目录放 `latest.json` + 安装包，客户端启动比对版本、提示"有新版本，点此更新"。

### 5.3 客户端版本上报 + 最低版本门禁
全仓 grep 无 `client_version`/`app_version`（记录表只有 `ps_version` 与 `client_machine`），版本号在 `client/sidecar/__init__.py`、`package.json`、`tauri.conf.json` 三处手工同步，必然漂移。
→ 记录表加 `client_version`；`/status` 返回服务端要求的 `min_version`；不达标时客户端顶部提示升级。否则你永远不知道"这个 bug 是不是老版本才有的"。

### 5.4 安装包签名 + 金标准图
- **NSIS 未签名**：`tauri.conf.json` 的 bundle 没有任何签名配置，员工首次运行会被 SmartScreen 拦成"未知发布者，已阻止"，很多人到这一步就放弃了。内网也建议自签证书 + 域内分发。
- **金标准图**：每个模板首次使用时用固定假数据生成一张基准图，记录尺寸/DPI/色模/大小区间，之后每次导出都比对（这是把 P0-7 变成可回归的唯一办法）。

### 5.5 一键诊断包
现在只有"复制诊断信息"7 行文本（`AdvancedTab.vue:63-75`）。P0-5 那种"配置全空、界面全 500"的场景，员工没法告诉你任何有用信息。
→ 一键导出 zip：脱敏 config（去掉 api_key）、`sidecar.log`/`worker.log`/近期 `crash-*.log`、最近 5 个 `executions/*/{params,status}.json`、pending/dead 计数、`/health/ready`、PS 版本与系统信息。

### 5.6 给员工的一页纸（现在没有）
`docs/` 只有 DEPLOY（给运维）、AUDIT（给开发）、PLAN_DELTA、design。
→ 员工第一次装完要知道：PS 必须先打开、模板 PSD 放哪里、两处占位符是什么、断网会怎样、卡住了点哪里、出问题找谁（附"复制诊断信息"按钮位置）。**这一页纸能砍掉一半客服量。**

### 5.7 数据留存与归档策略
软删除记录与图片**永久保留**，磁盘只报警不归档（`DISK_ALERT_GB` 默认 20GB，超了只推一次群）。按每天 50 张、每张 3MB 估算，一年 ≈ 55GB。
→ 定策略：软删记录 90 天自动清理（含图片）、按年/月归档冷数据、镜像目录按月滚动。

### 5.8 备份的"异地"这一环
现状：每日 gzip 热备 + 保留 10 份 + 可选 `IMAGES_MIRROR_DIR` 镜像。但如果镜像挂在**同一台 NAS 的另一块盘**（compose 注释里的示例就是），勒索/误删/整机故障仍然一起没。
→ 至少做到：镜像目标 = 另一台设备或 NAS 快照 + 每月一次 `--restore --rehearse` 演练（清单里已有，缺的是"谁在什么时候做过"的记录）。

### 5.9 客观监控（不要只靠自己写的状态卡）
状态卡是"服务自己说自己好不好"，NapCat 挂了它不一定知道（P0-1）。
→ 最小成本：内网一台常开机器 curl `/health/ready` + 检查当天 `reports/*.status.json`，异常发第二个通道（企业微信/邮件）；或直接用 NAS 自带的容器监控告警。

### 5.10 账号管理
没有改密界面（只有 `python -m flask create-user/reset-password` CLI），没有密码强度策略，没有"停用账号"的 UI（模型有 `is_active` 但**没有维护入口**），`viewer` 角色建出来也只能改库。
→ 至少给 admin 一个"账号"页：改密、停用、看最近登录。

### 5.11 时间一致性与"两天前/一天后"的口径
`work_date` 来自**客户端本机时钟**（`routes_export.py:133`），服务端只做 ±1 天 / 近一年校验（`records.py:68`）；同时服务端有 `date.today()` 20+ 处（`grep` 实测），而 `config.py:20-24` 在 Windows 上**主动 pop 掉 TZ**。
→ 容器里有 `TZ=Asia/Shanghai` 时是自洽的，但一旦 .env 的 TZ 被改、或某台机器时钟慢一天，"今日录用"指数就会错。
→ 建议：统一 `today() = datetime.now(ZoneInfo(Config.TZ)).date()` 一处实现；启动日志打印 `TZ / 系统时间 / 生效 today`；客户端上报时带 `client_time`，服务端偏差 >5 分钟就在响应里提示。

### 5.12 上线后的"运营三件事"（流程，不是代码）
1. **首周每天看三个数**：今日出图数、失败率、待补传总数（客户端底栏）。失败率 >5% 或补传堆积 >0 就该查。
2. **每周抽查一张成品图**：从后台点开原图，和模板比对（金标准校验已经拦掉尺寸错/空白/半截，但"内容对不对"——占位符填对没、图层有没有漏——机器判不了，这仍是唯一人工兜底）。
3. **变更记录**：这次审计发现的每个 P0 修完要有一条回归用例钉住，否则下次重构会悄悄退回去（本项目已有 3 次"改了代码线上还是旧行为"的先例：废弃索引、静态缓存头、告警哨兵）。

---

## 六、上线节奏建议

| 阶段 | 内容 | 出口条件 |
|---|---|---|
| **第 1 周** | P0-1…P0-7 全部 ✅ 已完成（604 全绿，前端 39 例 + vue-tsc 通过） | 每条 P0 有回归用例且做过变异验证 |
| **第 2 周** | 打第一版带版本号的安装包；3 人灰度（含 1 台老机器/1 个非 RGB 模板） | 灰度 5 个工作日：0 丢图、0 无记录、日报每日到达 |
| **第 3 周** | 修 P1 服务端项 + 客户端 9/12/13/14；补员工手册、诊断包 | 灰度反馈清零 |
| **第 4 周** | 全量分发（签名安装包 + 自动更新通道）；上线监控与备份演练 | 上线检查清单（DEPLOY 第四节）全打勾 + 演练记录归档 |

**不建议**跳过灰度直接全量：P0-5 与 P0-6 的表现都是"客户端看着正常、数据没了"，全量后才发现就要挨台排查。

---

## 七、本次取证方法（可复现）

```bash
# 1) 全量测试（七个 P0 全部修完后实测 604 passed, 0 skipped）
.venv/Scripts/python.exe -m pytest tests -q

# 2) 七个 P0 的运行时取证（脚本已跑，输出见下）
.venv/Scripts/python.exe <temp>/ps_probe.py
```

关键输出（原文）：

```
[1] OneBot 推送：HTTP 200 + body failed
  push_to_group -> ok=True msg=推送成功              ← P0-1 假成功

[2] 离线队列：服务端 4xx
  HTTP 403: 队列 2 -> 0 {'sent': 0, 'skipped': 2, 'dead': 0}
  HTTP 401: 队列 2 -> 0       HTTP 429: 队列 2 -> 0      ← P0-4 静默丢积压

[3] config.json
  截断文件 -> api_key='' server_url='http://127.0.0.1:8080' employee=''
  保存后磁盘内容: {"server_url": "http://127.0.0.1:8080", "api_key": "", ...}   ← P0-5 配置丢失
  {'output_dir': 123} -> AttributeError: 'int' object has no attribute 'startswith'

[4] 机器人回调
  POST 无任何凭据 -> HTTP 200 body={'status': 'ok'}
  handler 是否照做: {'uid': 888888}
  日志: 群内指令触发: user_id=888888 group_id=999999    ← P0-3 任意群任意人

[5] 调度器重启语义
  触发时刻=2026-10-24 18:05 下一次 fire=2026-10-25 18:00  ← P0-2 当天不补推
```

审计范围（逐文件读过）：`server/app/**`（含 routes/services/panels/overview/web/backup）、`core/*.py`、`bot/*.py`、`client/sidecar/**`、`client/src/**`、`client/src-tauri/src/*.rs`、`tests/**`、`Dockerfile` + 两份 compose + `scripts/package_nas.py` + `.github/workflows/tests.yml`。

---

*本报告与 `docs/AUDIT-2026-10-24.md` 互补：那份是"结构整改清单"（已闭环），这份是"上线风险清单"（待决策）。*
