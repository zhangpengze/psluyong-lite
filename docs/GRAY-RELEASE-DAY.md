# 灰度当天操作清单（3 人灰度 → 全量）

> 配套文档：[DEPLOY.md](DEPLOY.md) §四（完整上线清单）、§五（回滚）、§三（QQ 机器人）
> 机器可判项跑 `scripts/preflight_check.py`（FAIL = 退出码 1）；本清单是它判不了、
> 必须人做+留证据的部分。
> 纪律：每一项勾的都是"做过且有证据"，不是"应该可以了"。

---

## A. 开灰度前必须完成（4 项放行条件）

### A1. 发货代码已过 CI ✅（2026-10-30 已完成）

远程 main = `71d3f2f`，GitHub Actions 三 job（pytest ubuntu / pytest windows / client）全绿。
后续每次上线保持同一顺序：**先 push → CI 绿 → 再上 NAS**。

### A2. 首份备份存在（否则预检 Day-1 就 FAIL）

```bash
docker exec psluyong-lite python -m server.backup
ls server/data/backups/        # 确认有 *.db.gz
```

开发机上从没跑过热备，NAS 上线当天同样没有——`preflight_check.py` 的「备份存在」
当天会 FAIL。先跑这步，再跑预检。

### A3. QQ 机器人三键 + 重推验证

`.env` 填 `QQ_BOT_APPID` / `QQ_BOT_SECRET` / `QQ_BOT_GROUP_OPENID`（缺项不报错，
只是不推送，仪表盘显示灰色「未配置」——"没配"和"推失败"是两种状态，别混），
`docker compose up -d` 后：后台「每日日报」页 → 选任意一天 →「重推此日报」→
**QQ 群收到消息且徽标变「已推送」**。

群开关（「机器人主动在群聊内发言」）与开放平台 IP 白名单的前置步骤见
DEPLOY.md §三，不重复列。

### A4. 图片第二份：配镜像 + 三层验收 + 证据包

1. **配置**：compose 把第二块盘/另一台设备挂到 `/app/mirror`，
   `.env` 设 `IMAGES_MIRROR_DIR=/app/mirror`（留空 = 只有一份，盘坏即丢图）。
2. **三层验收，缺一不可**：
   - [ ] **人工核实不同物理存储池（不可省的最底线）**：UGOS 存储管理里确认镜像卷与
     数据卷属于**不同存储池/盘阵**。同池不同卷在任何用户态探针下都不可判定。
   - [ ] **容器内双视角 st_dev 取证**：
     ```bash
     docker exec psluyong-lite python -c "import os; print(os.stat('/app/data').st_dev, os.stat('/app/mirror').st_dev)"
     ```
     两个数不同 ≠ 异构（同池不同卷 st_dev 也不同），只证明不是同一文件系统。
   - [ ] **目标线**：另一台设备或异地 + 月度恢复演练记录（对应全量阻塞 #2）。
3. **证据包归档**（放行记录引用它，不是预检输出）：

   | 宿主卷 | 所属存储池 | 容器挂载点 | st_dev（宿主视图） | st_dev（容器视图） | 是否同池 |
   |---|---|---|---|---|---|
   | /volume1/… | 存储池 1 | /app/data | | | 否（人工确认） |
   | /volume2/… | 存储池 2 | /app/mirror | | | |

   外加：UGOS 存储管理截图、docker exec 原始输出。
4. **⚠ 预检输出的正确读法**：`preflight_check.py` 的「图片第二份 PASS」只证明
   "配了且可写"——它的同盘判定只比盘符，在 NAS 的 POSIX 路径下恒为 PASS，
   **不能作为异盘证据写进放行记录**。
5. **灰度期暂只有一份的兜底**：书面接受「成品图单份」风险 + 定补齐期限 + 记入放行记录。

### A5. 灰度前预检（探线上）

```bash
.venv/Scripts/python.exe scripts/preflight_check.py --base-url http://NAS:8080
```

期望 FAIL = 0。「需人工」项对照 DEPLOY.md §四逐个人工勾，别当"不通过"跳过，
也别当"必须全绿"卡住——三态语义见 DEPLOY.md §四开头。

---

## B. 灰度期（约 1 周）观察点

- [ ] 3 名员工各用真实模板出 1 张图：后台有记录、输出目录只有 `作者_标题.jpg`
      （无 `.psluyong-tmp` / `.校验未通过` 残留）
- [ ] 「生成完立刻关客户端再重开，后台仍能查到记录」（关窗补报路径，DEPLOY.md §四已有）
- [ ] 断网一次再恢复：离线队列自动补传，后台补到那条记录（客户端底栏有提示）
- [ ] 每天看仪表盘「系统状态」卡：备份时间 / 日报推送 / 图片占用三个组件行
- [ ] `docker ps --filter name=psluyong` 只有一行；启动日志无「检测到另一份服务在运行」
- [ ] 至少 1 次恢复演练跑通并留记录（全量前必须有，见下）

---

## C. 全量前剩余阻塞（5 项，跟踪表）

| # | 阻塞 | 完成标准（证据） | 状态 |
|---|---|---|---|
| 1 | 图片第二份（5.8） | A4 三层验收全过 + 证据包归档 | ☐ |
| 2 | 恢复演练零记录 | NAS 实跑 `docker exec psluyong-lite python -m server.backup --restore <最近一份> --rehearse`，输出「彩排通过」+ 各表行数，归档时间与操作人（DEPLOY.md §5.3） | ☐ |
| 3 | 告警单通道、无外部监控（5.9） | Uptime Kuma（最低成本）或企业微信/邮件 webhook 盯 `/health/ready` + 当天 reports 状态文件，与机器人通道互不依赖——机器人自己挂了告警也静默是现状 | ☐ |
| 4 | NAS 路径 root+chmod 777 | NAS 文件管理把 data 属主改 `1000:1000`，删 deploy/nas compose 的 `user:`/`command:` 两行，回归非 root（两份 Dockerfile 已证明可行） | ☐ |
| 5 | 外网访问形态未定 | 经 UGREENlink 等外网入口：先上 HTTPS 反代 + `FORCE_HTTPS=1` 再放开；纯内网：书面记录「不外网」决定 | ☐ |

---

## D. 全量放行

- [ ] A / B / C 全部有证据归档（放行记录建议落 `docs/releases/`，含镜像 tag、日期、操作人）
- [ ] 员工手册已发到每个人，且知道「设置 → 高级 → 导出诊断包」在哪（DEPLOY.md §四末项）
- [ ] 客户端安装包分发方式已定：**无自动更新、安装包未签名**（SmartScreen 拦截是首批
      装机第一道门槛，内网自签+域分发可解；产品侧拍板见 RELEASE-READINESS 5.2/5.4）
- [ ] 本次镜像 tag 已记录：`docker images psluyong-lite`（回滚要用，DEPLOY.md §5.1）

---

## 附：本清单的来历（2026-10-30 上线就绪审计）

放行条件与阻塞项来自架构/后端/QA/运维四路并行审计（见
[RELEASE-READINESS-2026-10-24.md](RELEASE-READINESS-2026-10-24.md)），其中：

- 「push + CI 三 job 全绿」已于 2026-10-30 闭环（首跑抓到 test_dir_usage 的
  mtime 时间戳粒度竞态，修复后二跑全绿，提交 `71d3f2f`）；
- 「同盘判定在 POSIX 恒 PASS」是审计实测结论，代码修复（st_dev 运行时告警）
  已排期未落地——落地前，镜像验收以本清单 A4 的人工核实为准。
