# ADR-001：日报推送的传输层

- **Status**: **Superseded（2026-10-25，用户裁决）** —— 下面的「双通道 fan-out」方案**未采纳**
- **相关**: `docs/decisions/OPEN-DECISIONS.md` 第 1 行、`scripts/qqbot_spike.py`、审计 P1-3 / P1-8

---

## Superseded：实际采用的方案

用户的原话是「**删除之前用 QQ 号登录的逻辑**」。所以最终不是 fan-out，而是**单通道**：

- `bot/handler.py`（群内指令）、`bot/access.py`（双白名单）、
  `server/app/routes/bot.py`（`/api/v1/bot/onebot` 回调端点）、`tests/test_bot_handler.py`
  全部删除；
- `bot/report.py` 里的 `_post_with_retry` / `_verify_onebot_response` / `push_to_user`
  与 `report_render.clamp_payload`（OneBot 的 `[{type:text}]` 形状）一并删除；
- 新增 `bot/qqbot_sender.py`：token 缓存（提前 60 秒刷新）+ `/v2/groups/{openid}/messages`
  + `err_code` 判定 + 错误码翻译 + 3 次退避重试；`report.py` / `report_alert.py` /
  `scheduler.py` 都只调它；
- 配置从 `NAPCAT_*` / `BOT_*` / `REPORT_GROUP_ID` 换成
  `QQ_BOT_APPID` / `QQ_BOT_SECRET` / `QQ_BOT_GROUP_OPENID` / `QQ_BOT_API_BASE`；
- **群内「汇总」指令不再提供**：官方平台只投递 @机器人 的消息事件，全量关键词触发做不到。

下面保留原始分析（含调研表），因为"为什么不再自己实现 WebSocket""`group_openid` 是
另一套 id 空间""access_token 有 60 秒窗口"这些结论在实现里全部用上了；被推翻的只有
"保留 NapCat 作主通道"这一条。

---

## Background

现在的日报推送只有一条路：NapCat（OneBot v11），底层是**登录一个真实 QQ 号**的第三方协议端。

三个问题叠在一起：

1. **合规风险**：违反 QQ 用户协议，有风控/封号概率；文档里只能写"建议用小号"。
2. **单点**：`report_alert.py` 的运维告警走的也是被监控的 NapCat 本身——NapCat 一挂，日报和告警同时静默（审计 P1-3 已记录，当时只补了本地 JSONL 留痕，通道仍然是单条）。
3. **凭证风险**：`NAPCAT_TOKEN` 走明文 HTTP，它等于"以那个 QQ 号身份发消息"的能力（P1-8）。

官方 QQ 机器人能一次解决这三条，但**不能全换**：群指令必须 @ 机器人才收得到（现在是全量消息、发「汇总」两字就回），换过去是功能回退。

所以问题不是"换不换"，而是**传输层怎么组织**。

## 调研：开源项目怎么做的

| 项目 | 做法 | 可借鉴的点 |
|---|---|---|
| **NoneBot2** | `driver.register_adapter()` 注册多个适配器；业务插件只处理统一 `Event`。QQ-GitHub-Bot 用同一份代码同时挂 `QQAdapter + OneBotV11Adapter + GitHubAdapter` | **多传输并存是一等公民**，不是二选一的开关 |
| **AstrBot**（4.3 万星） | `Platform` 抽象基类（连接生命周期 + `send_by_session()` 主动发送）与 `AstrMessageEvent`（响应投递 `send()`）**双类分离**；`PlatformManager` 用 `_task_wrapper` 包住每个适配器的 `run()`，崩溃只更新状态不拖垮主进程 | ① **主动发送要独立于入站事件**——日报就是"没有入站事件也要发"；② 适配器崩溃必须隔离 |
| **QQ-GitHub-Bot** | `providers/platform/extractors/{onebot,qq,_base}.py` 把各平台的 user/group id 与权限归一化 | **两套 id 空间要归一**（我们是「群号」vs「group_openid」，正是两套） |
| **qq-botpy**（腾讯官方 SDK） | `botpy.Client` 子类 + `on_group_at_message_create` + `message._api.post_group_message(group_openid=..., msg_type=0, msg_id=...)`；WS/心跳/重连/intents 全内置 | 生产上**别自己写 WebSocket**；`msg_id` 决定被动回复，不带就是主动消息 |
| **生态现状** | go-cqhttp 已停止维护；现役 OneBot 实现是 NapCat / LLOneBot / Lagrange。NapCat 基于 NTQQ 二次封装、有 Docker 与 WebUI、风控相对低 | **NapCat 是混合许可证、要求衍生开发获得授权**——我们只是跑官方镜像没改它代码，风险可接受，但升级前必须读 LICENSE |

## Decision（未采纳）

**做双通道 fan-out，不换掉 NapCat。**

新增一层 `bot/notify.py`（聚合器）+ `bot/senders/{onebot,qqbot}.py`（两个同步实现）：

```
report.py / report_alert.py / scheduler.py
        │  只调 notify.send_group(text, kind="daily")
        ▼
   Notifier（读 BOT_SENDERS，逐个发，聚合结果）
        ├── OneBotSender  → NapCat  HTTP  /send_group_msg      （现有代码搬过来）
        └── QQBotSender   → 官方     REST  /v2/groups/{openid}/messages
```

规则：

- **至少一个成功就算成功**；两个都失败才算失败并告警（这时告警本身也得走另一条路，否则又是同一个坑）。
- `BOT_SENDERS` 默认 `onebot`，**行为与现在完全一致**；填 `onebot,qqbot` 才启用双通道。
- 官方这一侧**只用 REST，不引 `qq-botpy`**：日报是纯主动推送，不需要事件循环。access_token 自己缓存（提前 60 秒刷新，官方文档明确写了这个 60 秒窗口）。
- 事件（学 `group_openid`、将来若要 @ 指令）单独一个可选后台线程，用探针里已验证的 MiniWs 或届时再引 `botpy`。**不进主链路**——主链路继续是同步 `requests`，waitress 那 8 个线程一行都不用改。
- 群标识归一到一处（照抄 QQ-GitHub-Bot 的 extractor 思路）：配置里同时存 `REPORT_GROUP_ID`（OneBot 群号）与 `QQ_BOT_GROUP_OPENID`，缺哪个就只走另一条。

## Consequences

**正面**

- NapCat 挂了日报还能发（双通道）；官方挂了也不影响现在的路。
- 审计 P1-3 的"告警与被监控通道是同一个"有了真正的第二条腿。
- 不引入 asyncio、不引入 `qq-botpy`、不改 `requirements.txt` 的锁死版本；`requests` 已经在用。
- 群指令保持现状（NapCat 全量消息），不做功能回退。
- 迁移是**单向可逆**的：`BOT_SENDERS=qqbot` 就是纯官方通道，随时可切回。

**负面 / 代价**

- 约 1.5 人日（聚合器 + 两个 sender + 用例 + 变异验证）。
- 两条通道的消息格式能力不同（官方不支持 Markdown、URL 可能被拦）：日报是纯文本，不受影响，但将来加图要注意。
- 需要人先在 q.qq.com 完成注册/实名，并打开「机器人主动在群聊内发言」——**这一步不过，代码写了也发不出去**，所以排在 spike 之后。
- 双发会让群里看到两条一样的消息（如果两个通道都健康）。**这是刻意的**：宁可重复也不丢。若要"只在一条失败时才补发第二条"，见下方待定项。

## 待定（不阻塞本次决策）

- 双通道是**并行双发**还是**主备降级**（主失败才发备）？并行更简单、不丢；主备更安静。建议先并行，观察一周后再决定要不要降级成主备。
- 官方侧的群指令（`GROUP_AT_MESSAGE_CREATE`）要不要做？取决于用户是否接受"必须 @ 机器人"。
