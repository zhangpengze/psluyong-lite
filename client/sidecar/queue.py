"""离线队列

网络断了不应该让员工白干一遍：生成成功的记录先落本地，
下次启动或下次生成成功时按顺序补传。

死信处理：如果队头记录是"永久失败"（服务端每次都 500，比如图片文件
已被手动删掉），重试永远不会成功，还会堵住后面所有正常记录。所以
每条记录带失败计数，累计 5 次转入 dead/ 目录留档，队列继续走。

拒绝码分三类（2026-10-24 修：原来把所有 4xx 一律当"业务拒绝"删掉，
换 API Key / 服务器地址填错时会把整批积压静默删除，界面上还显示成补传成功）：
  - DISCARD_STATUS（400/409/422）：这条数据本身有问题，重发一万次也一样 → 删除
  - BLOCKING_STATUS（401/403/404）：记录没错，是环境/配置问题（Key 轮换没同步、
    地址指向旧版本）→ **保留**并标 blocked，等人修好配置后下次 flush 自动补上
  - 429 与其他未预期的 4xx：保留，本轮停止（服务端在限流/我们不认识的拒绝）
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR, QUEUE_DIR

logger = logging.getLogger(__name__)

DEAD_DIR = DATA_DIR / "dead"
MAX_ATTEMPTS = 5
# 连续遇到这么多条 5xx 就认为服务端整体故障，停止本次补传
CONSECUTIVE_5XX_LIMIT = 3

# 这条数据本身不合法：重试没有意义，删掉（不能留成永远补不成功的毒记录）
DISCARD_STATUS = frozenset({400, 409, 422})
# 环境/配置问题：记录是好的，不能删。标 blocked，修好配置后自动补传
BLOCKING_STATUS = frozenset({401, 403, 404})


def _ensure() -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)


def _dead_dir() -> Path:
    DEAD_DIR.mkdir(parents=True, exist_ok=True)
    return DEAD_DIR


def enqueue(fields: dict, image_path: str | None, blocked_reason: str = "") -> Path:
    """记录入队。blocked_reason 非空表示"环境问题导致暂时传不上去"（如 401），
    界面据此提示员工去检查设置，而不是显示成正常待补传。"""
    _ensure()
    item_id = f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    payload = {"fields": fields, "image_path": image_path, "created_at": datetime.now().isoformat()}
    if blocked_reason:
        payload["blocked"] = True
        payload["blocked_reason"] = blocked_reason
        payload["blocked_at"] = datetime.now().isoformat()
    path = QUEUE_DIR / f"{item_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("记录已进入离线队列: %s%s", path.name, "（受阻）" if blocked_reason else "")
    return path


def pending() -> list[Path]:
    _ensure()
    return sorted(QUEUE_DIR.glob("*.json"))


def blocked_items() -> list[dict]:
    """被环境/配置拦住的积压（401/403/404）。界面必须显示出来——
    否则员工看到"待补传 N 条"永远不减少，却不知道原因在设置里。"""
    out = []
    for path in pending():
        item = load(path)
        if item and item.get("blocked"):
            out.append({"name": path.name, "reason": item.get("blocked_reason", "")})
    return out



def dead_letters() -> list[Path]:
    if not DEAD_DIR.exists():
        return []
    return sorted(DEAD_DIR.glob("*.json"))


def load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.error("离线记录损坏，跳过 %s: %s", path.name, e)
        return None


def remove(path: Path) -> None:
    """成功补传或业务拒绝后删除。损坏文件的留档走 _to_dead。"""
    path.unlink(missing_ok=True)


def _rewrite(path: Path, item: dict) -> None:
    """更新失败计数等元数据。写坏也不至于丢记录：tmp + 原子替换。"""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _to_dead(path: Path, item: dict, reason: str) -> None:
    item["dead_reason"] = reason
    item["dead_at"] = datetime.now().isoformat()
    target = _dead_dir() / path.name
    try:
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)
        path.unlink(missing_ok=True)
        logger.error("离线记录转死信 %s（原因: %s）", path.name, reason)
    except OSError as e:
        logger.error("死信转移失败，保留原记录 %s: %s", path.name, e)


def retry_dead() -> dict:
    """把死信搬回队列重试一次。

    适用场景：服务端修好了、图片又被放回去了、或者只是当时网络抖得厉害。
    搬回时清掉 failures，否则下一次 5xx 会立刻再次打回死信 —— 员工点了
    重试却什么都没发生，比不给按钮更糟。

    损坏文件（只有 corrupt_raw、没有 fields）搬回去也没法提交，直接留在
    死信里，靠人工看文件。
    """
    restored = failed = 0
    for path in dead_letters():
        item = load(path)
        if item is None or not item.get("fields"):
            failed += 1
            continue
        item.pop("failures", None)
        item.pop("dead_reason", None)
        item.pop("dead_at", None)
        try:
            _ensure()
            target = QUEUE_DIR / path.name
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, target)
            path.unlink(missing_ok=True)
            restored += 1
        except OSError as e:
            logger.error("死信恢复失败 %s: %s", path.name, e)
            failed += 1
    if restored:
        logger.info("已把 %d 条死信搬回队列待重试", restored)
    return {"restored": restored, "failed": failed, "dead": len(dead_letters())}


def flush(client) -> dict:
    """按顺序补传。

    - 网络不通（status=0）：立即停，后面的大概率也失败，且记录不该被计数
    - 服务端 5xx：可能是整体故障也可能是单条毒记录——计数 +1，连续多条
      5xx 才停；单条累计 MAX_ATTEMPTS 次转死信
    - 400/409/422（数据本身不合法）：删记录跳过，不值得重试
    - 401/403/404（环境/配置问题）：**保留**并标 blocked，立即停本轮
      （同一个 Key/地址对整批都一样，继续试只会把剩下每条都标一遍）
    - 429 及其他 4xx：保留，停本轮，不计失败次数

    返回值里的 blocked 是**当前**受阻条目数（不是本轮新增），界面直接显示。
    """
    from .api_client import ServerError

    sent = skipped = 0
    consecutive_5xx = 0
    block_reason = ""
    for path in pending():
        item = load(path)
        if item is None:
            # 损坏文件移入死信留档（保留原始内容供排查），而不是悄悄删掉
            raw = path.read_text(encoding="utf-8", errors="replace")[:4000]
            _to_dead(path, {"corrupt_raw": raw}, "文件损坏")
            skipped += 1
            continue
        image_path = Path(item["image_path"]) if item.get("image_path") else None
        try:
            client.submit_record(item["fields"], image_path)
            remove(path)
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
                    _to_dead(path, item, f"连续 {failures} 次服务端错误: {e.message}")
                    continue
                _rewrite(path, item)
                if consecutive_5xx >= CONSECUTIVE_5XX_LIMIT:
                    logger.warning("连续 %d 条 5xx，暂停补传（服务端疑似整体故障）", consecutive_5xx)
                    break
                continue

            if e.status in DISCARD_STATUS:
                logger.info("补传丢弃（这条数据服务端不接受）: %s -> %s", path.name, e.message)
                remove(path)
                skipped += 1
                continue

            if e.status in BLOCKING_STATUS:
                # 关键：不删。换 Key 忘同步、服务器地址指向旧版本时，整批积压都会
                # 401/404；旧版在这里 remove()，员工界面上"待补传 3 条"直接变 0，
                # 看起来像补传成功，实际记录全丢且不可恢复。
                item["blocked"] = True
                item["blocked_reason"] = f"HTTP {e.status}: {e.message}"
                item["blocked_at"] = datetime.now().isoformat()
                _rewrite(path, item)
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
        "remaining": len(pending()),
        # 直接数以目录为准：dead 计数器含本轮新转入的，再加目录数会算两遍
        "dead": len(dead_letters()),
        "blocked": len(blocked_items()),
        "block_reason": block_reason,
    }
