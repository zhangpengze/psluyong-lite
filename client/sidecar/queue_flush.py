"""离线队列的补传执行

从 queue.py 拆出来的原因有两个：

1. **并发**。补传有四个触发点：主窗启动、60 秒轮询恢复、员工点"立即补传"、
   上报成功后的顺手补传。以前没有任何互斥，两个请求同时进来会各自读到同一份
   积压，然后**各自提交一遍**：
     - 成功记录：服务端唯一索引挡住（409），白跑一趟，还算"跳过"
     - 5xx 的记录：failures 被两边各 +1，MAX_ATTEMPTS 被提前用完，本该还能
       重试的记录被提前丢进死信（真丢数据）
     - 两个请求同时 rewrite 同一个 json：后写的覆盖前写的
   现在一把非阻塞锁：正在补传就直接返回空结果，调用方拿到的是"当前积压数"，
   不会误以为已经清空。

2. queue.py 已经到 300 行闸门了，塞不下更多东西；补传策略（什么码该删、
   什么码该留）本来也是独立的一块知识。

常量和 store 层（入队/删记录/死信）仍在 queue.py，这里只做"按顺序试一遍"。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from . import image_buffer

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
# 连续遇到这么多条 5xx 就认为服务端整体故障，停止本次补传
CONSECUTIVE_5XX_LIMIT = 3

# 这条数据本身不合法：重试没有意义，删掉（不能留成永远补不成功的毒记录）
DISCARD_STATUS = frozenset({400, 409, 422})
# 环境/配置问题：记录是好的，不能删。标 blocked，修好配置后自动补传
BLOCKING_STATUS = frozenset({401, 403, 404})

# 同一时刻只许一轮补传（见模块说明）
_FLUSH_LOCK = threading.Lock()


def flush(client) -> dict:
    """按顺序补传。已有补传在进行时立刻返回当前积压，不排队、不重复提交。"""
    from . import queue

    if not _FLUSH_LOCK.acquire(blocking=False):
        logger.info("补传已在进行中，本次跳过（多窗口/按钮与定时器会同时触发）")
        return queue.empty_result()
    try:
        return _flush_locked(client, queue)
    finally:
        _FLUSH_LOCK.release()


def _flush_locked(client, queue) -> dict:
    """真正的补传循环。

    - 网络不通（status=0）：立即停，后面的大概率也失败，且记录不该被计数
    - 服务端 5xx：可能是整体故障也可能是单条毒记录——计数 +1，连续多条
      5xx 才停；单条累计 MAX_ATTEMPTS 次转死信
    - 400/409/422（数据本身不合法）：删记录跳过，不值得重试
    - 401/403/404（环境/配置问题）：**保留**并标 blocked，立即停本轮
      （同一个 Key/地址对整批都一样，继续试只会把剩下每条都标一遍）
    - 429 及其他 4xx：保留，停本轮，不计失败次数
    - 成品图两边都找不到：转死信留档，**不提交**（旧版会不带图建一条无图记录）

    返回值里的 blocked 是**当前**受阻条目数（不是本轮新增），界面直接显示；
    lost 是本轮因为缺图转死信的条数。
    """
    from .api_client import ServerError

    pruned = image_buffer.prune(queue.QUEUE_DIR, queue.DEAD_DIR)
    if pruned:
        logger.info("清理了 %d 个过期的补传图副本", pruned)

    sent = skipped = lost = 0
    consecutive_5xx = 0
    block_reason = ""
    for path in queue.pending():
        item = queue.load(path)
        if item is None:
            # 损坏文件移入死信留档（保留原始内容供排查），而不是悄悄删掉
            raw = path.read_text(encoding="utf-8", errors="replace")[:4000]
            queue.to_dead(path, {"corrupt_raw": raw}, "文件损坏")
            skipped += 1
            continue
        image_path, image_lost = image_buffer.locate(item)
        if image_lost:
            # 声明过有图却哪都找不到：宁可留档也不能建成一条无图记录
            queue.to_dead(path, item, "成品图已不在（缓冲和原目录都找不到），不能当作无图记录提交")
            lost += 1
            continue
        try:
            client.submit_record(item["fields"], image_path)
            queue.remove(path, item)
            sent += 1
            consecutive_5xx = 0
        except ServerError as e:
            if e.status == 0:
                logger.warning("补传中断（服务端不可用）: %s", e.message)
                break
            if e.status >= 500:
                failures = int(item.get("failures", 0)) + 1
                consecutive_5xx += 1
                # 先写进 item 再判断，否则死信里留下的是上一轮的旧计数
                item["failures"] = failures
                if failures >= MAX_ATTEMPTS:
                    queue.to_dead(path, item, f"连续 {failures} 次服务端错误: {e.message}")
                    continue
                queue.rewrite(path, item)
                if consecutive_5xx >= CONSECUTIVE_5XX_LIMIT:
                    logger.warning("连续 %d 条 5xx，暂停补传（服务端疑似整体故障）", consecutive_5xx)
                    break
                continue

            if e.status in DISCARD_STATUS:
                logger.info("补传丢弃（这条数据服务端不接受）: %s -> %s", path.name, e.message)
                queue.remove(path, item)
                skipped += 1
                continue

            if e.status in BLOCKING_STATUS:
                # 关键：不删。换 Key 忘同步、服务器地址指向旧版本时，整批积压都会
                # 401/404；旧版在这里 remove()，员工界面上"待补传 3 条"直接变 0，
                # 看起来像补传成功，实际记录全丢且不可恢复。
                item["blocked"] = True
                item["blocked_reason"] = f"HTTP {e.status}: {e.message}"
                item["blocked_at"] = datetime.now().isoformat()
                queue.rewrite(path, item)
                block_reason = item["blocked_reason"]
                logger.error("补传受阻（配置问题，已保留）: %s -> %s", path.name, item["blocked_reason"])
                break

            # 429 限流与其他 4xx：保留、停本轮，不计失败次数（不是这条数据的问题）
            logger.warning("补传暂停（服务端拒绝 %s）: %s", e.status, e.message)
            block_reason = f"HTTP {e.status}: {e.message}"
            break

    return {
        "sent": sent,
        "skipped": skipped,
        "lost": lost,
        "remaining": len(queue.pending()),
        # 直接数以目录为准：dead 计数器含本轮新转入的，再加目录数会算两遍
        "dead": len(queue.dead_letters()),
        "blocked": len(queue.blocked_items()),
        "block_reason": block_reason,
    }
