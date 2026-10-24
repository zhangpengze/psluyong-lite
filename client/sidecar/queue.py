"""离线队列（落地 + 死信），补传执行在 queue_flush

网络断了不应该让员工白干一遍：生成成功的记录先落本地，
下次启动或下次生成成功时按顺序补传。

死信处理：如果队头记录是"永久失败"（服务端每次都 500，比如图片文件
已被手动删掉），重试永远不会成功，还会堵住后面所有正常记录。所以
每条记录带失败计数，累计 5 次转入 dead/ 目录留档，队列继续走。

成品图不再只记路径：入队时复制一份到 image_buffer（见该模块说明）。补传时
优先用副本，两边都找不到就**不提交**、整条转死信——旧版会让 api_client 不带
文件提交，服务端落一条没图的记录，界面还显示已同步。

拒绝码分三类（2026-10-24 修：原来把所有 4xx 一律当"业务拒绝"删掉，
换 API Key / 服务器地址填错时会把整批积压静默删除，界面上还显示成补传成功）：
  - DISCARD_STATUS（400/409/422）：这条数据本身有问题，重发一万次也一样 → 删除
  - BLOCKING_STATUS（401/403/404）：记录没错，是环境/配置问题（Key 轮换没同步、
    地址指向旧版本）→ **保留**并标 blocked，等人修好配置后下次 flush 自动补上
  - 429 与其他未预期的 4xx：保留，本轮停止（服务端在限流/我们不认识的拒绝）
三个集合的定义和用法都在 queue_flush，这里只是按老习惯转出来给调用方用。
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

from . import image_buffer
from .config import DATA_DIR, QUEUE_DIR
# 补传实现拆到 queue_flush（原因见那个模块的说明）；常量照旧从这里取，
# reporter / 测试一直在用 queue.DISCARD_STATUS 这种写法。
from .queue_flush import (  # noqa: F401
    BLOCKING_STATUS,
    CONSECUTIVE_5XX_LIMIT,
    DISCARD_STATUS,
    MAX_ATTEMPTS,
    flush,
)

logger = logging.getLogger(__name__)

DEAD_DIR = DATA_DIR / "dead"


def _ensure() -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)


def _dead_dir() -> Path:
    DEAD_DIR.mkdir(parents=True, exist_ok=True)
    return DEAD_DIR


def _buffer_image(image_path: str | None, item_id: str) -> Path | None:
    """入队时把成品图复制一份。复制不了不阻断入队，flush 时退回用原路径。"""
    raw = (image_path or "").strip()
    if not raw:
        return None
    source = Path(raw)
    if not source.is_file():
        # 记录生成完就被挪走了：没有图可复制。照常入队，flush 时按"缺图"转死信
        logger.warning("入队时成品图已不在，无法复制补传缓冲: %s", raw)
        return None
    return image_buffer.store(source, item_id)


def enqueue(fields: dict, image_path: str | None, blocked_reason: str = "") -> Path:
    """记录入队。blocked_reason 非空表示"环境问题导致暂时传不上去"（如 401），
    界面据此提示员工去检查设置，而不是显示成正常待补传。"""
    _ensure()
    item_id = f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    payload = {"fields": fields, "image_path": image_path, "created_at": datetime.now().isoformat()}
    buffered = _buffer_image(image_path, item_id)
    if buffered is not None:
        payload["buffered_image"] = str(buffered)
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


def remove(path: Path, item: dict | None = None) -> None:
    """成功补传或业务拒绝后删除。损坏文件的留档走 to_dead。

    顺手删掉缓冲里的成品图：记录已经落地了，副本留着只会白占磁盘。
    """
    if item:
        image_buffer.discard(item)
    path.unlink(missing_ok=True)


def empty_result() -> dict:
    """没做补传时给界面的空结果。形状必须和 flush() 完全一致——
    少一个字段前端拿到的是 undefined，比报错更难查。"""
    return {
        "sent": 0,
        "skipped": 0,
        "lost": 0,
        "remaining": len(pending()),
        "dead": len(dead_letters()),
        "blocked": len(blocked_items()),
        "block_reason": "",
    }


def rewrite(path: Path, item: dict) -> None:
    """更新失败计数等元数据。写坏也不至于丢记录：tmp + 原子替换。"""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def to_dead(path: Path, item: dict, reason: str) -> None:
    item["dead_reason"] = reason
    item["dead_at"] = datetime.now().isoformat()
    target = _dead_dir() / path.name
    # 死信要自带图：人工点"重试死信"时还能补上，否则这条记录只剩字段没有成品
    image_buffer.to_dead(item, DEAD_DIR, path.stem)
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
        # 图搬回缓冲，别让它一直躺在 dead/ 里（重试成功后缓冲会被一起清掉）
        image_buffer.restore(item)
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
