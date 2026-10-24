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

> **2026-10-24 第二批已修 6 项**（1、2、3、9、10、15），每项都是「代码 + 用例 + 变异验证」：
> 把逻辑改回旧行为，新用例必须变红，改回来必须变绿。变异数写在各项标题里。
> **第三批已修 3 项**（11、13、18 的 open-folder 部分），同样的交付标准。
> **第四批已修 2 项**（17、14）：都是"静默丢数据 / 污染统计"一类，同样每条都有变异验证。
> **第五批已修 2 项**（16、12）：都是"软件看着在跑、实际什么都没干"一类。
> 16 是 PS 卡死时员工只能关软件、PS 里文档越叠越多；12 是 8765 被占时新 sidecar 静默死在
> DEVNULL、前端连上僵尸进程。这一批变异 15/15 全红（含 Rust 那条——注意 `cargo test`
> 失败返回 **101** 不是 1，按 `rc!=0` 判会把"变异把代码写坏了"也算成"抓到了"）。
> 另外补了 `tests/test_rust_source_guards.py`：CI 里没有 cargo 步骤，`lib.rs` 的
> `include_str!` 守卫在流水线中等同于不存在，所以这三件事在 Python 侧再钉一遍。
> **第六批已修 3 项**（4、6、7）：都是"一个人就能把服务打挂 / 让最该发的那条发不出去"。
> 4 是上传接口可以无限次调用（一条 64MB）；6 是最忙那天的日报超长、整条被群拒发；
> 7 是仪表盘每 30 秒全量遍历图片目录。这一批变异 13/13 全红。
> **第七批已修 2 项**（18 的 `task_id` 校验、12 的 Job Object 兜底）：这两条原本排灰度期，
> 都是"一条正则 / 一层内核兜底"就能收掉的口子，顺手补了。变异 9/9 全红。
> 这一批真正的收获是**发现了一条假绿的用例**（`wait_exit` 少了 `SYNCHRONIZE`，永远返回
> "已退出"，把 `LimitFlags` 改成 0 也照样过）——守卫本身失效比缺守卫更危险；
> 也因此把变异判据从"控制台文本里找 ` failed`"换成读 `--junitxml`：中文 Windows 上
> pytest 输出是 cp936，`text=True` 按 utf-8 解会把 `out` 变成 `None`，真被抓到的变异
> 会被报成"未抓到"（假阴性让验证本身失真）。
> 剩下的 5、8 不是纯代码问题：5 要先定部署形态（单进程 or 多副本），
> 8 要 IT 决定（内网 HTTPS 还是来源 IP 白名单）。

### 服务端
1. ✅ **已修**（变异 2/2 全红）**状态卡一行异常打挂整张卡**：`status.py:112-123` `_database_row` 直接 `Record.query...count()`，**没有 try/except**；同文件 `_dir_usage`/`_backup_row` 都做了降级。DB 一锁（WAL 下高峰期可能）→ `/api/v1/stats/overview` 与仪表盘一起 500。
   → 现在五行全部经 `_safe_row` 取数，单行异常降级成「取数失败 · 异常类型」（带 `error` 原文与堆栈日志），整卡照常渲染；`_CACHE` 的读写补了 `threading.Lock`——8 线程 waitress 下并发读写一个 dict 本来也没保证。用例 `tests/test_system_status.py`（对五个行名参数化 + 概览接口仍 200 + 仪表盘仍出「取数失败」）。
2. ✅ **已修**（变异 4/4 全红）**`/health/ready` 只做 `SELECT 1`**（`health_service.py:14-16`）：磁盘满 / data 卷只读时写入全 500，readiness 依然 200；也不覆盖调度器存活。而 compose healthcheck 用的正是它——`restart: unless-stopped` **不会**因 unhealthy 重启，探针失败只是"显示不健康"。建议加 `DATA_DIR` 可写探测 + `scheduler.get_jobs()` + 最近 job 心跳。
   → 改成真写一次（`open/write/flush/fsync` 后 `unlink`，`finally` 里清理），`CRITICAL = ("db", "data_dir")` 任一失败即 503；调度器只作信息项（`off`/`unknown` 永不判失败——多进程部署下本进程没有调度器是正常的）。**没用 `os.access`**：它只查权限位，只读挂载、配额耗尽、inode 用尽照样返回 True。返回体保留旧 `ok`/`status`/`db` 三个键，失败细节只进日志不外泄。用例 `tests/test_health_ready.py`（14 条，含探针失败也不留垃圾文件、数据库不可用时不许把连接串回给调用方）。
3. ✅ **已修**（变异 4/4 全红）**告警是单通道且互相吞**：`report_alert.py:77-112` 全局单哨兵 + 300s 窗口，`scheduler.py:35-62` 三个运维告警连发时**只有第一条能出去**，后两条连日志都没有；而通道就是被监控的 NapCat 本身——NapCat 一挂，日报与告警**同时全静默**。
   → 节流键改为 `(kind, title)`，9 个调用点全部补上 `kind`；被抑制的那条**照样写 `logger.warning` 并追加到 `<DATA_DIR>/logs/ops-alerts.jsonl`**（记 `suppressed#N`），"没告警"和"告警被吞"从此能区分；成功发送后计数器归零。本地写失败只记日志，不影响告警本身出去。用例 `tests/test_ops_alert.py`（7 条）。**通道仍是 NapCat 这一点没变**——本地 JSONL 只是让静默留痕，真正的双通道（比如邮件/企业微信）仍是待办。
4. ✅ **已修**（变异 4/4 全红）**`POST /api/v1/records`（64MB 上传）无限流**（`ratelimit.py` 只被登录使用；bot 回调已在第二批接上）。
   → 新增 `server/app/upload_guard.py`：**每来源 30 次/分 + 全站 300 次/分**两道闸，超限 429 + `Retry-After`。三个关键点——
   ① 做成**装饰器**而不是"视图函数第一行"：请求体最大 64MB，`request.form` 一被访问就整块读进内存/临时盘，"先限流后解析"必须是结构性保证。用例用对照组证明这条顺序：额度够时 2KB body 走 413（`test_body_over_limit_is_413_when_quota_is_available`），额度用光后同样的请求给 429。
   ② 返回 **429 而不是 400**：sidecar 队列只删 400/409/422，429 会保留并停本轮、不计失败次数；写成 400 等于"被限流一次丢一条记录"。首次上报那条路（`reporter.submit_fields`）也验证过 429 → 入队 + 落 `blocked_reason`（新用例 `test_429_on_settle_keeps_the_record_in_the_offline_queue`，这条路径以前完全没测）。
   ③ **单来源之外还要全站闸**：只按来源分，10 台机器同时发疯照样打满磁盘。被来源闸拦住时不消耗全站额度（否则一个人拖垮所有人）。
   → 用例 `tests/test_upload_limit.py`（6 条，含"列表接口不受上传限流牵连"）+ `tests/test_ratelimit.py`（+4 条闸门顺序）+ `tests/test_settler.py`（+1）。
   → 顺带在 `tests/conftest.py` 加了 autouse 的限流计数清空：整个测试会话共用一个 app、同一来源 127.0.0.1，不清就会在跑到几十条提交用例后整片 429，而失败信息看起来像业务接口坏了。
5. **多进程/多副本会重复推日报**：`wsgi.py:5-6` 在 import 时启动调度器；APScheduler 无分布式锁。当前 compose 单进程跑没事，一旦改成 `--scale` 或 gunicorn 多 worker 就是 N 条重复日报。用 P0-2 的幂等闸门 + `SCHEDULER_OWNER` 环境变量兜住。
6. ✅ **已修**（变异 4/4 全红）**正文长度无上限**（`bot/report.py:46-98`）：伙伴数 × 模板名（200 字符）可轻易过万字符，超 QQ 单条上限即整条推送失败（且因 P0-1 被误报成功）。
   → `report_render.clamp_for_message / clamp_payload`：发送前按**字节**（`MESSAGE_MAX_BYTES = 4000`，中文一字 3 字节，按字符数判断会以为还很空）按**行**截断（切半行像乱码，而每行都是一条独立信息），尾部注明"已省略 N 行：单条消息有长度上限，完整版见后台「每日日报」"。
   → 截的是**发送副本**：`run_daily` 落盘的 `.md` 与接口返回的 `content` 都是全文，所以"完整版见后台"这句承诺是真的——用例 `test_run_daily_pushes_clamped_but_archives_full` 同时钉住三件事（存档 == 全文、发出去的更短且不超限、返回给后台页面的仍是全文）。
   → 截断点放在 `_post_with_retry`（`push_to_group` / `push_to_user` / 群内指令回复全部经过它），而不是各调用点各截一次。
   → 用例 `tests/test_report.py`（+6：短文本不动、按字节不按字符、只丢整行、真的发的是截断版、存档是全文、非文本 payload 原样放行）。
7. ✅ **已修**（变异 5/5 全红）**`status.py` 每 30s `rglob("*")` 全量遍历 `IMAGES_DIR`**，几万张图时仪表盘明显卡。
   → 新增 `server/app/dir_usage.py`：按**目录层**缓存（本层文件数/字节数/子目录列表），键失效条件是"该目录 mtime 变了"，外加 5 分钟 TTL 与容量上限（4096 条，超了整体清空）。
   → **只缓存分层结果、每次重新加总**：缓存"子树合计"的话，孙目录里删一个文件，上层会一直报旧值直到 TTL 过期——这是这类缓存最容易写错的地方，专门有一条用例（`test_change_deep_down_updates_the_parent_total`）钉它。
   → 一次遍历从"每张图一次 stat"降到"每个目录一次 stat"（一年 365 个日目录）。
   → 用例 `tests/test_dir_usage.py`（8 条：计数、缺目录、复用不重扫、新图立刻可见、深层变更、TTL 过期、坏目录不拖挂整行、容量上限）。`_CACHE` 的锁在第 1 项已补。
8. **Token 明文过 HTTP**（`report.py:128-135`，本机 `.env` 实测是 `http://222.222.0.111:13000`）：拿到即可冒充机器人往任何群发消息。至少改内网 HTTPS 或来源 IP 白名单。

### 客户端
9. ✅ **已修**（变异 5/5 全红）**PS 冷启动重试预算约 6 秒**（`ps_connector.py:68-73,90-126`：3 次循环 + `sleep(2)`），而 `Dispatch` 在 PS 未运行时会静默拉起 PS，冷启 15~40s → **员工第一次点生成必然失败**，还会入库一条 failed 并触发失败告警。`ps_path` 参数（`ps_service.py:47-61`）赋值后**从未使用**，是装饰品。
   → 新增 `core/ps_retry.py`：冷启 60s / 热连 10s 两套预算、指数退避（0.5s 起、上限 2.5s、最多 40 次）、连之前用 `tasklist` 判 PS 是否在跑、冷启时用 `ps_path` **主动拉起**（`ps_path` 从此有用了）。致命错（组件未注册 / PS 已崩溃）不重试，立刻带根因失败——等 60 秒只会折磨员工。顺带修了一个隐藏 bug：**pywin32 给的 HRESULT 是有符号负数**（`-2147023174`），而所有码表都按无符号写（`0x800706BA`），不 `& 0xFFFFFFFF` 就一条都对不上。用例 `tests/test_ps_retry.py`（30 条）+ `tests/test_ps_connector.py`（11 条）。
10. ✅ **已修**（变异 4/4 全红）**单占位符没命中仍然 success**（`ps_service.py:189-203` 只在"全部规则都没命中"时判失败）：模板里 `{{标题}}` 被误删 → 成品图没标题，照样 success 交付。
    → 两个表单必填字段对应的替换规则标 `required=True`，未命中即判失败并**点名是哪个占位符**；可选规则仍只做提醒（文案保持原样）。同时把"正则规则无效"从"模板里找不到占位符"里摘出来单独报——否则员工会跑去改模板，而真正错的是配置。流程抽到 `core/ps_rules.py` + `core/ps_replace_flow.py`（`ps_service.py` 因此回到 211 行，守住 300 行闸门）。用例 `tests/test_ps_rules.py` + `tests/test_ps_service.py`。
11. ✅ **已修**（变异 2/2 全红）**单实例开关是 toggle**（`lib.rs:746-753`）：主窗已显示时双击桌面图标 → 窗口被**隐藏**，员工以为没启动。建议第二个实例只 `show + focus`。
    → 新增 `show_main_window`，步骤抽成纯函数 `show_steps(minimized, visible)`：`minimized` 才还原、`!visible` 才 `show`、**最后一定聚焦**，任何分支都不隐藏。托盘菜单的 toggle 语义保持不变（那个菜单本来就叫「显示/隐藏主窗」）。窗口操作要真窗口才能测，所以沿用项目里 `nearest_side` 的老办法——把决策抽成纯函数验，另加一条守卫：`include_str!("lib.rs")` 断言第二个实例**没有**调到 `toggle_main_window`（这类"用错函数"光看逻辑测不出来）。用例 `lib.rs::instance_tests`（6 条）。
12. ✅ **已修**（变异 4/4 全红）**僵尸 sidecar / 端口占用**：`lib.rs:873-885` 只 kill 一级子进程，worker 是孙进程；sidecar 无父进程看门狗；8765 被占时新 sidecar 静默死在 DEVNULL，前端仍连上僵尸 → `busy` 可能永远 true、按钮永远 409。
    → **两层看门狗**：Tauri 启动 sidecar 时写 `PSLUYONG_PARENT_PID`（`lib.rs` 的 `tag_parent_pid` 包在 `resolve_sidecar` 包装层里，所有返回路径都过它）；`sidecar/parent_watch.py` 每 2 秒探一次，爹没了就 `os._exit`（COM 卡死时 atexit/logging 都可能跟着挂住，所以不走优雅退出）；worker 子进程同样起一个（`_spawn_worker` 里**覆盖**继承来的 Tauri pid，否则它盯的是"爷爷"）。
    → **Windows 上不能 `os.kill(pid, 0)`**：除 `CTRL_C_EVENT`/`CTRL_BREAK_EVENT` 外任何信号都会**杀掉**目标进程——判活变谋杀，用例 `test_alive_does_not_kill_the_target` 就是拿真子进程盯这一条。改用 `OpenProcess(SYNCHRONIZE|QUERY_LIMITED)` + `WaitForSingleObject(h, 0) == WAIT_TIMEOUT`。
    → **端口占用可见化**：启动前先裸 `bind` 一次（`server.probe_port`）。不能在 `SO_REUSEADDR` 的套接字上探——Windows 下那样连被占的端口都能绑上（werkzeug 正是这么设的），两个 sidecar 会同时活着；裸 bind 则老老实实报 10048。被占时写清"很可能是上一次的 sidecar 还活着"并 `SystemExit(1)`，`app.run` 自己抛的 OSError 也兜住再退。
    → 用例 `tests/test_parent_watch.py`（20 条）+ `tests/test_sidecar_startup.py`（8 条）+ `lib.rs::parent_pid_tests`（3 条）。
    → ✅ **Job Object 兜底已补**（变异 2/2 全红，另加 Python 镜像守卫 1/1）：`client/src-tauri/src/win_job.rs`。sidecar **先入 job、后按需起 worker**，所以 worker 天生在 job 里；`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 打开后，Tauri 被 `TerminateProcess` 时句柄由 OS 关闭，内核把整棵树收走——`TerminateProcess` 下 Python 一行都执行不到，看门狗那 2 秒窗口并不存在。job 句柄**故意永不关闭**（关掉就等于立刻杀掉 sidecar），它的生命周期正是"Tauri 进程还活着"。入 job 失败只 `eprintln!` 不拦启动：兜底不该让客户端起不来。
    → 已知边界写进文件头注释：只能管"入 job 之后"生出来的子进程（顺序不能反）；Java/某些安装器会创建 breakaway job，但本进程树里没有。
    → ⚠️ **这一批最值得记的不是功能，是那条假绿的用例**：`关掉最后一个句柄会带走子进程` 一直"通过"，把 `LimitFlags` 改成 0 也照样通过。原因是 `wait_exit` 里 `OpenProcess` 只申请了 `PROCESS_QUERY_LIMITED_INFORMATION`，**没有 `SYNCHRONIZE`**，`WaitForSingleObject` 每次返回 `WAIT_FAILED`，被 `rc != WAIT_TIMEOUT` 判成"已退出"。现在改成三态返回（已退出/仍在跑/**不知道**），每次关句柄前先断言"子进程还活着"，再补一条 `LimitFlags=0` 的**对照组**必须活下来——对照组证明"杀进程"确实是这个标志位干的，而不是进程碰巧自己退了。
13. ✅ **已修**（变异 2/2 全红）**生成成功后 `saveConfig` 抛异常会把结果翻成"生成失败"**（`useExportTask.ts:119-140` 成功分支与 catch 在同一个 try）：图已生成、记录已上报，员工看到失败必然再点一次 → 撞 409，白跑一轮 PS。
    → 「记住上次用的模板/作者」抽成 `rememberThisCombo()`，自带 try/catch，失败只 `console.warn`，**不弹 toast**（告诉员工"没记住"除了焦虑没别的用）。清空表单、提示成功、结果条滚动都在它之后，因此都不再被这一步拖累。用例 `client/src/composables/useExportTask.spec.ts`（7 条，其中 4 条专门盯"saveConfig 炸了也必须报成功"）。
14. ✅ **已修**（变异 2/2 全红）**三个窗口各跑一遍 `startAfterReady`**（`App.vue:107-142`）→ 启动瞬间对同一批 pending 并发 flush；成功记录有唯一索引兜底（409），但 **5xx 的记录会被并发两三轮各记一次 `failures`**——`MAX_ATTEMPTS=5` 提前用完，本该还能重试的记录被提前丢进死信（真丢数据）；两个请求还会同时 `_rewrite` 同一个 json，后写的覆盖前写的。
    → 两层：前端把"该谁补传"抽成纯函数 `utils/windowRole.ownsQueue(route)`，只有主窗跑 `flushQueue`/`refreshToday`，辅助窗只读 `/status`（界面数字照样实时）；sidecar 侧把补传主体拆到 `queue_flush.py`，加一把**非阻塞**锁——正在补传时第二个请求立刻返回当前积压（不排队、不重复提交，如实告诉调用方还剩多少）。用例 `client/src/utils/windowRole.spec.ts`（3 条）+ `useSidecarStatus.spec.ts`（7 条，含"轮询发现服务器恢复时辅助窗也不补传"）+ `tests/test_queue.py`（并发互斥、`empty_result` 与 `flush` 字段形状一致）。
    → 顺带把 `reporter.py` / `routes_shell.py` 里手抄两份的"空补传结果"换成 `queue.empty_result()`：字段少一个是 `undefined`，比报错难查得多。
15. ✅ **已修**（变异 4/4 全红）**"测试连接"测的是已保存的旧地址**（`AccountTab.vue:22-39` 调 `/status`，后者用 `load_config()` 的磁盘值），toast 却打印表单里的新地址 → 改了地址提示"连接成功"，保存后连不上。
    → 新增 `client/sidecar/conn_test.py` + `POST /config/test`：**用表单里此刻填的地址与 Key** 真发请求。两步探针——先 `/health` 判地址，再打一个需要鉴权的接口判 Key（只看 `/health` 的话，Key 是上一任的也照样 200）。结论分四档 `invalid` / `unreachable` / `key_bad` / `server_error`，前端如实转述；任何异常都不许抛出（按钮不能永远停在"测试中…"）。用例 `tests/test_conn_test.py`（12 条）+ `vue-tsc` 通过。
16. ✅ **已修**（变异 8/8 全红 + Rust 3/3 全红）**PS 无硬超时看门狗**：`executor.py:116-125` 进程活着就一直返回 running，`heartbeat_stale` 只用来显示；`close_document` 失败被 `logger.debug` 吞掉并置 None（`ps_service.py:96-104`）→ PS 里文档越叠越多，最终"暂存盘不足"。
    → **硬超时 300 秒**（拆到 `sidecar/timeout.py`，`executor.py` 又从 300 行门禁边上退回来）：`status()` 轮询路径和后台 reaper（`_sweep_once`）用**同一个判据**——只写在一处，两处各写一份早晚有一处先漂。判据**只看总耗时、不看心跳**：worker 卡在 COM 里时 pywin32 会释放 GIL，心跳照常每 2 秒跳，看起来一切正常。
    → 中止顺序是**先写终态再 kill**（和 `cancel()` 同一套理由）：反过来的话，被杀的 worker 有可能先把"进程没了但终态没写"写进磁盘，于是被判成 `uncertain`——员工看到"结果未知"，其实是我们自己掐的。
    → 终态 `success=false` 且 `state="timeout"`；结算层（`settler.py`）把它映射成 `uncertain: true, timeout: true` 上报，**不生成 failed 记录**——图有可能已经导出来了，只是卡在收尾，报失败既让员工白重做一张，也污染失败率。前端 `ExportFail` 加 `timeout?: boolean`，与 `uncertain` 同类处理。
    → **关文档失败不再不可见**：`close_document` 返回原因串（`""` 表示成功），runner 把它挂到本次结果的 `warnings` 上——"它不影响这张图，但请手动关掉 PS 里那个模板，否则文档会越叠越多"。`disconnect()` 里的同类吞掉也一并改成 warning。
    → 用例 `tests/test_executor.py`（+6：判据纯函数/终态绝不报成功/轮询路径中止/**没人轮询时 reaper 也会中止**/新任务不被误杀/worker 盯的是 sidecar 的 pid）+ `tests/test_run_export.py`（+3）+ `tests/test_settler.py`（timeout 参数化）+ `tests/test_ps_service.py`（2 条改写）。
17. ✅ **已修**（变异 5/5 全红）**队列补传时图没了仍建"无图记录"**（`api_client.py:61-75` 只在 `exists()` 时才带 files），UI 还显示"已同步"；`queue` 只存路径字符串不复制文件 → 员工整理过输出目录就永久丢图。
    → 新增 `image_buffer.py`：入队时把成品图**复制**进 `%APPDATA%\psluyong-lite\pending_images\{item_id}.jpg`（与队列 json 同名，保住对应关系），补传优先用副本；复制失败（磁盘满/杀软锁文件）只记日志、记录照常入队并退回用原路径。缓冲和原目录**都**找不到时**不提交**、整条转死信（图跟着进 `dead/`，人工点「重试死信」时连图一起搬回缓冲），界面显示"死信 N 条"。副本有保留期：成功落地/业务拒绝时立刻删，超过 7 天且队列与死信里都没有对应 json 的孤儿按天清掉。首次上报那条路（`reporter` 直调 `api_client`）保留"无图也发"（失败记录本来就没图），但补了一条 warning 日志。
    → `api_client` 的 `exists()` 顺手改成 `is_file()`：目录也能通过 `exists()`，`open()` 会抛。
    → 用例 `tests/test_queue.py`（10 条：复制成功/清理输出目录后仍带图补传/两边都没有转死信且**一次都没提交**/复制失败退回原路径/成功后删副本/业务拒绝后删副本/本来无图的记录不判缺图/死信带走图且重试搬回/过期孤儿清理/优先用副本）。
18. ✅ **已修**（`/open-folder` 变异 3/3 全红；`task_id` 变异 5/5 全红）**本机写操作面**：`/open-folder` 接受任意 `path`（`routes_shell.py:26-33`）；`task_id` 未做格式校验就拼路径（`routes_export.py:160` + `worker.py:36-37` 会 `mkdir(parents=True)`）。单机桌面场景危害有限，但一条正则就能堵。
    → 打开目录加白名单：只许**输出目录 / 模板目录 / 本机数据目录**，路径先 `resolve()` 再按 `os.sep` 边界比对（`base + os.sep` 是必须的——裸 `startswith` 会让 `期刊录用2` 被当成 `期刊录用` 的子目录）；Windows 大小写用 `normcase` 归一，`..\..\Windows` 这类串在规范化后绕不出去。前端两处调用（记录页打开输出目录 / 结果条打开成品所在目录）都落在白名单内，行为不变。用例 `tests/test_sidecar.py`（11 条）。
    → **`task_id` 白名单**（`client/sidecar/task_paths.py`）：字母数字开头、只许 `-` 和 `_`、最长 64 位；另挡 Windows 保留设备名（`CON`/`PRN`/`AUX`/`NUL`/`COM1-9`/`LPT1-9`）——`CON` 是个合法文件名，但 `open("executions/CON/status.json")` 在 Windows 上打到的是控制台设备，不报错也不落盘。三个入口（`routes_export` 的 status/cancel、`executor.status` 的 fallback、`settler.settle_task`）共用一个 `task_dir()`，各写一份早晚有一处先漂。
    → **顺带查出一个真 bug**：`executor.status()` 对没见过的 id 会返回 `{"state":"unknown","finished":True}`，路由把它当终态送去结算，于是后台落一条**幽灵失败记录**——成功率被扣，而那条"失败"根本没人做过。修法是把"目录不存在"和"id 非法"都当"没这个任务"：结算直接 `return None`。用例 `test_unknown_task_id_creates_no_phantom_failure_record` 断言 `submitted == []`。
    → 用例 `tests/test_task_paths.py`（35 条：真实 id 形状、`..`/`../x`/`a/b`/`C:\Windows`/绝对路径/空格/点号/`$`/超长/`None`/int/dict/保留设备名/`task_dir` 不出 `executions/`/不创建目录）+ `tests/test_sidecar.py`（+3）+ `tests/test_settler.py`（+2）+ `tests/test_executor.py`（+2）。

### 本轮新增的一条（不在原报告里）

19. ✅ **已修**（变异 6/6 全红）**内网请求会走系统代理**：`api_client.py` / `conn_test.py` / `diagnostics.py` 全部用模块级 `requests.get`，而 `requests` 默认读 `HTTP_PROXY` / `HTTPS_PROXY`。公司电脑装了 VPN 客户端或 IT 推了系统代理时，发给内网服务器的请求会被送去代理，代理回 **502**——员工侧永远"连接失败"，运维在自己机器上（没配代理）复现不出来。**这条是验证过程中被绊出来的**：本机就设着 `http_proxy`，诊断包的就绪探针返回 `502 upstream connect failed`，而服务好好跑着。
   → 新增 `client/sidecar/net.py`：对内网的请求统一走 `net.get` / `net.request`，显式带 `proxies={"http": None, "https": None}`（requests 把 `None` 当"这个 scheme 不走代理"，且不会再用环境变量补回来）。默认值而非硬规定：调用方显式给了代理仍以调用方为准。外部 webhook（`bot/report.py`）**有意保留走代理**。`scripts/preflight_check.py` 的就绪探针同样处理——运维笔记本上最可能有代理。用例 `tests/test_net_no_proxy.py`（6 条，含一条"反证"：不传 proxies 时 requests 确实会把环境代理补上）。

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

### 5.5 一键诊断包 ✅ **已完成（2026-10-24）**
现在只有"复制诊断信息"7 行文本（`AdvancedTab.vue:63-75`）。P0-5 那种"配置全空、界面全 500"的场景，员工没法告诉你任何有用信息。
→ 一键导出 zip：脱敏 config（去掉 api_key）、`sidecar.log`/`worker.log`/近期 `crash-*.log`、最近 5 个 `executions/*/{params,status}.json`、pending/dead 计数、`/health/ready`、PS 版本与系统信息。
→ **实现**：`client/sidecar/diagnostics.py` + `GET /diagnostics`（给 curl 远程取）+ `POST /diagnostics/save`（存到 `%APPDATA%\psluyong-lite\diagnostics\` 并打开文件夹）。设置 → 高级 →「导出诊断包」。
　`api_key` 只抹值不抹键（保留 `***已隐藏（原长度 N）***`，能看出"配了但长度不对"）；日志每份只取**尾部 300 行**；最近 **5 次**生成；整包上限 8MB。任一项取不到**只降级不中断**——配置坏了正是最需要出包的时候，所以 `build_bundle` 允许 `client=None`。
　不用 `<a download>`：Tauri 窗口在部分 Windows 版本上点了没反应，一个静默无效的按钮比没有更糟（见 `routes_shell.py` 注释）。用例 `tests/test_diagnostics.py`（变异 5/5 全红）。

### 5.6 给员工的一页纸 ✅ **已完成（2026-10-24）**
`docs/` 只有 DEPLOY（给运维）、AUDIT（给开发）、PLAN_DELTA、design。
→ 员工第一次装完要知道：PS 必须先打开、模板 PSD 放哪里、两处占位符是什么、断网会怎样、卡住了点哪里、出问题找谁（附"复制诊断信息"按钮位置）。**这一页纸能砍掉一半客服量。**
→ **实现**：[员工手册.md](员工手册.md)，53 行非空、一页纸。结构就是「先开 PS → 四步出图 → 断网会怎样 → 卡住查表 → 导诊断包找谁 → 五件别做的事」。
　写的时候真踩到两个错：主按钮记成「生成」（实际「生成录用图」）、默认输出目录记成 `D:\期刊录用`（实际是用户「文档」下的 `期刊录用`，`D:` 只是设置页的输入示例）。所以补了 `tests/test_employee_handbook.py`：**手册里指向界面的每一句，都对着真实 Vue 源码校一遍**——按钮名、离线横幅原文、四句阶段文案、90 秒取消阈值、60 秒冷启预算、模板缺失文案、设置页五个分组名、默认目录。顺手也校出 `ps_connector.py` 里一句「再点一次『生成图片』」是同一个错，一起改了。变异 10/10 全红。


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

> 2026-10-24 补充：上线检查清单本身也做成了可执行的——`scripts/preflight_check.py`
> 把 DEPLOY 第四节里机器能判的九项跑一遍，三态输出（通过 / 不通过=退出码 1 / 需人工），
> 可直接当 CI 或部署前置闸门。**关键是别把「需人工」当「不通过」**：那会让闸门天天响，
> 人就学会"跑不过也照上"，闸门等于没有。

---

## 六、上线节奏建议

| 阶段 | 内容 | 出口条件 |
|---|---|---|
| **第 1 周** | P0-1…P0-7 全部 ✅ 已完成（916 全绿，前端 56 例 + vue-tsc 通过，Rust 18 例） | 每条 P0 有回归用例且做过变异验证 |
| **第 2 周** | 打第一版带版本号的安装包；3 人灰度（含 1 台老机器/1 个非 RGB 模板） | 灰度 5 个工作日：0 丢图、0 无记录、日报每日到达 |
| **第 3 周** | P1 服务端 1/2/3/4/6/7 ✅；客户端 9/10/11/12/13/14/15/16/17 ✅；18 全项 ✅（含 `task_id` 白名单）；12 的 Job Object 兜底 ✅；员工手册、诊断包、预检脚本 ✅（2026-10-24 第二~七批）。仅剩服务端 5（多副本重复推日报）、8（Token 明文过 HTTP）待口径 | 灰度反馈清零 |
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
