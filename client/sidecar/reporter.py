"""记录字段组装与上报（从 routes_export 抽出来的副作用）

单独一个文件的原因：这段代码要发网络请求、要把失败记录塞进离线队列，
和"什么时候结算、结算几次"（settler.py）是两件事。混在一起时改上报口径
（比如 401 该不该删记录）容易碰到幂等判定，反过来也一样。
"""

from __future__ import annotations

import json
import logging
import os
import platform
from pathlib import Path

from . import queue
from .api_client import ServerError
from .config import ClientConfig, app_data_dir
from .worker import read_json

logger = logging.getLogger(__name__)

LOG_TAIL_LINES = 3
LOG_TAIL_CHARS = 300


def log_tail(lines: int = LOG_TAIL_LINES, max_chars: int = LOG_TAIL_CHARS) -> str:
    """取 worker 日志尾部，给失败记录当"现场"。

    员工只会说"生不出来"，而 PS 的真实报错在 worker.log 里。把最后几行带上，
    后台详情页就能直接看到原因，不用再让对方去找 %APPDATA% 下的日志文件。
    """
    path = app_data_dir() / "logs" / "worker.log"
    try:
        # 日志按 2MB 轮转，直接读尾部的 8KB 足够覆盖最后几行
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 8192))
            raw = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    picked = [line.strip() for line in raw.splitlines() if line.strip()][-lines:]
    return " / ".join(picked)[-max_chars:]


def record_fields(cfg: ClientConfig, task_dir: Path, status: dict, error_message: str) -> dict:
    params = read_json(task_dir / "params.json") or {}
    warnings = list(status.get("warnings") or [])
    if not status.get("success"):
        tail = log_tail()
        if tail:
            warnings.append(f"客户端日志：{tail}")
    return {
        "work_date": params.get("work_date", ""),
        "employee_name": params.get("employee_name") or cfg.employee_name,
        "template_id": params.get("template_id", ""),
        "template_name": params.get("template_name", ""),
        "partner_name": params.get("partner_name", ""),
        "author": params.get("author", ""),
        "title": params.get("title", ""),
        "client_machine": platform.node(),
        "ps_version": "",
        "status": "success" if status.get("success") else "failed",
        "error_message": error_message,
        "warnings": json.dumps(warnings, ensure_ascii=False) if warnings else "",
        "processing_ms": status.get("duration_ms", 0),
    }


def submit_fields(
    cfg: ClientConfig,
    fields: dict,
    image_path_str: str,
    success: bool,
    client_factory,
) -> dict:
    """把一条记录上报服务器；网络问题进离线队列，不让员工白干。

    返回与旧版 /export 相同形状的响应负载（前端 ExportOk 直接吃这份）。
    """
    image_path = Path(image_path_str) if image_path_str else None
    client = client_factory(cfg)

    if not success:
        # 失败的尝试也入库——否则"今天怎么这么慢"永远查不出原因
        try:
            client.submit_record(fields, None)
        except Exception as e:  # noqa: BLE001 - 失败上报不许挡住给员工的响应
            logger.warning("失败记录上报失败: %s", getattr(e, "message", e))
        return {"ok": False, "error": fields.get("error_message") or "生成失败", "uploaded": False}

    uploaded, queued, upload_message, record_id, duplicate = False, False, "", None, False
    try:
        record = client.submit_record(fields, image_path)
        uploaded = True
        record_id = record.get("id")
    except ServerError as e:
        upload_message = e.message
        if e.status == 409 and e.payload.get("duplicate"):
            # 服务端已有同一条记录 = 上一次其实报成功了，只是本地没来得及写
            # result.json（进程被杀 / 断电）。这不是错误，必须算已结算，
            # 否则每轮扫描都会再试一次，永远收不了尾。
            duplicate = True
            upload_message = "服务器已有这条记录，按已报送处理"
            logger.info("记录此前已上报成功，补记结算标记: %s", e.message)
        elif e.status == 409:
            # 直接透传服务端文案。判重规则会变（比如键里加上模板名），
            # 客户端硬编码一份迟早和服务端对不上，员工看到的原因就是错的。
            upload_message = f"服务器提示：{e.message}"
        elif e.status in queue.DISCARD_STATUS:
            # 400/422：参数或图片本身不合法，重试永远不会成功。
            # 塞进离线队列只会变成补不成功的毒记录，还会堵住后面的正常记录。
            upload_message = f"服务器拒绝了这条记录（{e.status}）：{e.message}"
        else:
            # 401/403/404/429/5xx 都不是"这条数据有问题"，而是配置或环境问题
            # （Key 轮换没同步、地址指向旧版本、被限流、服务端故障）。
            # 旧版在这里直接丢弃不排队——员工看到"生成失败"，图其实已经生成，
            # 这条记录永久消失。现在落队列并标受阻，配置修好后自动补传。
            blocked_reason = ""
            if e.status in queue.BLOCKING_STATUS or e.status == 429:
                blocked_reason = f"HTTP {e.status}: {e.message}"
            queue.enqueue(
                fields,
                str(image_path) if image_path else None,
                blocked_reason=blocked_reason,
            )
            queued = True
            upload_message = (
                f"服务器暂时无法接收（{e.status}）：{e.message}，记录已存本地，"
                "修好配置或服务恢复后自动补传"
            )

    # 顺手把之前积压的补传掉
    flushed = queue.flush(client) if uploaded else {
        "sent": 0,
        "skipped": 0,
        "remaining": len(queue.pending()),
        "dead": len(queue.dead_letters()),
        "blocked": len(queue.blocked_items()),
        "block_reason": "",
    }
    return {
        "ok": True,
        "image_path": str(image_path) if image_path else "",
        "image_name": image_path.name if image_path else "",
        "uploaded": uploaded,
        "queued": queued,
        # 幂等补记：服务端已有记录，等同于 uploaded
        "duplicate": duplicate,
        "upload_message": upload_message,
        "record_id": record_id,
        "flushed": flushed,
    }
