# 主管后台「数据概览」重设计

> 2026-10-24 · 基于与主管的四轮需求确认 + 概念稿对齐后实施完成
> 状态：已实现（251 个测试全绿，含 27 个新增概览用例）

## 1. 需求确认（用户原话归纳）

1. 看板**只给主管一人**使用（现有 viewer/admin 会话体系已够，不新增认证）
2. 核心视角是**合作伙伴**：每个伙伴展示「用了多少个不同的模板」+「每个模板用了几次」
3. **合作伙伴不在后台管理**——名单由员工本机客户端维护，服务器只读；因此不建
   partners/templates 实体表（与 models.py 顶部注释的架构决策一致）
4. 时间筛选五档：**今天 / 昨天 / 近 7 天 / 自定义区间 / 近 30 天**
5. 数据更新：30 秒自动轮询 + 手动刷新 + 「最后更新于 HH:MM:SS」

## 2. 设计决策

### 信息架构（一个屏幕一个数据故事）

| 区域 | 内容 | 回答的问题 |
|---|---|---|
| KPI 行（4 卡） | 合作伙伴（活跃/总数/新增）、模板调用（含环比）、成功率（失败可点进筛选列表）、活跃员工 | 整体怎么样 |
| 趋势卡 | 近 N 天每日调用 SVG 面积图，标题带完整起止日期 + 日均 + 峰值 | 在怎么变 |
| TOP5 卡 | 合作伙伴调用排行横条 | 谁是主力 |
| 明细表 | 合作伙伴 \| 模板数 \| 区间调用 \| 占比 \| 最近活跃 \| 状态；**点行展开各模板用量** | 每个伙伴具体情况 |
| 系统状态条 | 最后备份 / 昨日日报推送 / 图片占用（原设计保留） | 系统自身健康 |

### 关键交互

- **伙伴行展开**：模板明细随首屏数据一起下发（`html.details` 片段按序对齐
  `partners` 数组），展开不发请求；交互与记录页「点行展开详情」一致
  （tabindex=0、回车/空格、aria-expanded），轮询重渲染后自动恢复展开状态
- **下钻到底**：展开行内「查看该伙伴全部记录」→ `/records?partner=…&start=…&end=…`，
  记录页既有筛选直接复用；KPI 的失败数 → `/records?status=failed&…`
- **单日区间**（今天/昨天）：趋势图退化为单根柱子 + 数值，不比一个孤零零的点诚实

### 排版与颜色（专业感来源）

- 数字一律 `tabular-nums` + 千分位 + 右对齐；日期带年份（跨年不认错批次）
- 涨跌用箭头 + 符号，颜色只表意：涨=绿、跌=红、无参照=灰
- 全套仅一个主色（既有 `--primary` 蓝）+ 三个语义色（绿=活跃、琥珀=预警、灰=停用），
  全部走 app.css 既有 CSS 变量，零硬编码色值（P0 规则）
- 图标沿用 base.html 侧栏的 SVG 统一描体系（16px），本页无新增图标需求
- 口径写在明细表底部小字：状态分档规则 + 调用次数定义——主管看到「停用」时
  知道它是怎么算出来的，数据才可信

### 实时更新方案（架构师复核结论）

**轮询 30s，不上 SSE。** 单用户内网看板 + SQLite 单写者 + 无 CDN 约束下，SSE 的
长连接与失效逻辑是纯负担；轮询负载可忽略（见 §4）。细节：

- 页面不可见时跳过轮询；重新可见且超过一个周期立即补一次
- 轮询中不打断操作：只 innerHTML 替换数据区，展开状态保留
- 连续失败 3 次才 toast 提示「显示的是上次成功加载的数据」
- 服务端另有 15 秒缓存 + 记录写入（新增/删除/恢复）主动失效——
  员工一提交，主管页面下一个轮询周期（≤30s）即见新数据

## 3. 数据契约

### `GET /api/v1/stats/overview`（api_or_session，viewer 放行）

参数：`range=today|yesterday|7d|30d|custom`；custom 另带 `start`/`end`（YYYY-MM-DD）；
`limit` 伙伴明细展示条数（默认 20，0 = 全部，上限 500，非法值退回默认）。
非法区间返回 400 + 中文错误（起>止 / 缺日期 / 格式错 / 超 90 天 / 未来日期 / 未知档位）。

响应（结构化字段 + `html` 片段，片段由 panels/ 包生成并转义，Jinja 首屏与 JS
轮询复用同一份标记，避免两套渲染漂移）：

```
range{preset,label,start,end,days}  updated_at(HH:MM:SS)  updated_iso
kpi{calls, calls_prev, calls_delta_pct, success, failed, success_rate,
    partners_active, partners_total, partners_new, partners_inactive,
    templates_active, employees_active, avg_per_day, peak_date, peak_count}
trend[{date,count}]                 # 按天补零
partners[{name, calls, templates_used, templates[{name,calls,share}],
          last_active, status(active|warning|dormant), share, first_seen}]
system{rows{service/database/backup/report/images{status,detail}}, }
meta{calls_definition, business_date, status_rule, tz}
html{kpi, trend, top, table, details[], note, status}
```

> system.rows 每行 = 一个组件：`status`（ok/warn/bad/off）+ `detail`（右侧真实指标）。
> 分档口径在 server/app/status.py 顶部常量：备份 >36h 预警、磁盘 ≥80% 阈值预警、
> ≥阈值异常、日报昨日未推送预警 / 失败异常 / 通道未配置置灰。
> 刻意不做延迟探测（没有网关和集群），不编"23 ms"这种数。

### `GET /api/v1/stats/overview/export`（api_or_session）

概览导出 CSV：汇总 + 伙伴明细 + 各伙伴模板用量三级；UTF-8 BOM（Excel 直开）；
不受 limit 截断影响（导出即全量）；记审计流水（export_csv）。
公式注入防护与记录页导出同规则（`=/+/-/@` 开头前置制表符）。

### 打磨轮增量（2026-10-24 晚）

- 切时间范围 `history.pushState` 同步地址栏 + popstate 监听：可收藏、可分享、可后退
- 趋势图透明热区 + 原生 tooltip：悬浮看任意单日数值（不引图表库）
- 伙伴明细表 `limit` 截断（默认 20 家）+「显示全部」按钮（点后带 limit=0 轮询）
- TOP5 横条可点击 / 键盘定位：滚到明细表对应行、展开、高亮 2 秒
- 侧栏显示上次登录时间（admin_users.last_login_at）
- 系统状态卡重设计（参考云厂商状态页）：一条紧凑状态栏 → 组件行
  （后台服务/数据库/数据库备份/日报推送/图片存储），每行状态点 + 组件·状态 +
  右侧真实指标（记录数/库大小/备份时间/磁盘占用与阈值）；无延迟探测就不编毫秒数；
  操作日志入口收进卡片标题右侧；status.py 重写为结构化 rows，测试 14 个用例覆盖全部分档

### 全面体检增量（2026-10-24 凌晨）

体检修复的 bug：

- `dashboard.js` 轮询并发：原 `inflight` 布尔下并发调用拿到立即 resolve 的
  空 Promise，TOP5 定位的"补拉再定位"会拿到旧表陷入死循环 → 改为共享
  in-flight Promise + 完成后按当前区间自动补拉，跳转补拉最多 2 次封顶
- 切档/后退撞上进行中的请求会丢请求 → 收尾时比对请求时区间与当前区间，
  不一致自动补拉
- 非法区间（>90 天、结束晚于今天）400 后 chip 停在错误档、轮询每 30 秒
  再 400 一次 → 4xx 时回滚到上一次成功区间（replaceState 同步地址栏）并显示原因
- 会话过期后轮询永远报"加载失败" → 401 直接跳 /login
- CSV 导出 Content-Type 重复（`charset=utf-8; charset=utf-8`）→ 概览与记录两处
  导出都改为 mimetype 不带 charset、由 header 单点声明

### 图片对账（曾加入，后按需求整体移除）

系统状态卡曾有第 6 行「图片对账」+ 夜间对账结果落盘展示；主管评估后认为
不实用，已于 2026-10-24 整体移除：状态行、`save_reconcile_status()` 落盘、
夜间任务对账步骤、`backup.check_images()` 与 CLI `--check-images`、相关测试。
`nightly_job` 保留模块级重构（备份/镜像/磁盘三步）。

### 口径（全站唯一定义，在 overview/ 包 __init__ 顶部 + 页面底部双公示）

- 调用次数 = `is_deleted=0` 的**全部**记录，含失败；成功率单独算
- 业务日 = `work_date`（员工填的工作日期），不是 created_at——补传昨天的记录
  按工作日期归属，不因传晚了归到第二天
- 合作伙伴维度只统计 `partner_name` 非空的记录；空伙伴名照常计入总量
- 伙伴状态：今天−最近一次调用 ≤7 天活跃、8–14 天预警、>14 天停用
- 环比 = 前一个**等长**区间（7 天对比前 7 天，单日对比前一天）

## 4. 性能与升级路径

- 直接 GROUP BY 聚合，不建预聚合表：单主管内网看板的量级（records 按天几十条），
  带 `idx_records_partner_date` 的伙伴×模板分组是毫秒级；nightly 聚合表反而要处理
  软删除/恢复后的重修一致性，是用复杂度换不存在的性能问题
- 15 秒服务端缓存（键 = start/end/preset，最多 32 个键）+ 写入主动失效
- **升级触发条件**（写在这里，到时照做）：records 过 10 万行，或本接口 P95
  超过 500ms → 上 `partner_daily_stats` 预聚合表，接口形状不用变

## 5. 文件清单

| 文件 | 改动 |
|---|---|
| `server/app/overview/` | 新增（包）：区间解析、聚合、缓存、口径 |
| `server/app/panels/` | 新增（包）：HTML 片段渲染（escape 后给 Jinja/JS 共用） |
| `server/app/status.py` | 新增：系统状态（从 web/views.py 拆出共享） |
| `server/app/routes/stats.py` | 新增 `GET /overview` |
| `server/app/routes/records.py` | 新增/删除/恢复后失效概览缓存 |
| `server/app/web/views.py` | 仪表盘路由改走 overview；删已搬走的函数 |
| `server/app/web/templates/dashboard.html` | 重写：外壳 + 首屏直出 + 时间筛选 |
| `server/app/web/static/dashboard.js` | 新增：轮询/切范围/展开 |
| `server/app/web/static/app.css` | 新增概览页组件样式（全走既有变量） |
| `tests/test_stats_overview.py` | 当时新增 27 个用例（现全套 383） |

## 6. 导航说明

概念稿中的六项导航（合作伙伴/模板管理/调用记录…）是方向示意；本次只重做仪表盘页，
侧栏维持既有四项——不给没有页面可去的导航入口。
