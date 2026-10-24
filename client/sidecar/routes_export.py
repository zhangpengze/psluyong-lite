"""生成任务路由（拆自 server.py）：/export 启动、/export/status 轮询上报、/export/cancel 取消。

COM 跑在 worker 子进程（executor），这里只编排：校验参数 → 起任务 →
轮询到终态时组装记录字段上报服务器（网络问题进离线队列）。
_client 由 create_sidecar 晚绑定传入，测试 monkeypatch 必须在请求时生效。
"""

from __future__ import annotations

import json
import logging
import os
import platform
from datetime import date
from pathlib import Path

from flask import Flask, jsonify, request

from core.runner import DEFAULT_FILENAME_RULE, FILENAME_RULES  # noqa: E402

from . import queue
from .api_client import ServerError
from .config import ClientConfig, load_config
from .executor import BusyError
from .file_ops import _ensure_output_dir, _ensure_template
from .ps_info import _resolve_ps
from .worker import read_json, write_json_atomic

logger = logging.getLogger(__name__)


def register_export_routes(app: Flask, executor, _client) -> None:
    def _submit_fields(cfg: ClientConfig, fields: dict, image_path_str: str, success: bool) -> dict:
        """把一条记录上报服务器；网络问题进离线队列，不让员工白干。

        返回与旧版 /export 相同形状的响应负载。
        """
        image_path = Path(image_path_str) if image_path_str else None
        client = _client(cfg)

        if not success:
            # 失败的尝试也入库——否则"今天怎么这么慢"永远查不出原因
            try:
                client.submit_record(fields, None)
            except ServerError as e:
                logger.warning("失败记录上报失败: %s", e.message)
            return {"ok": False, "error": fields.get("error_message") or "生成失败", "uploaded": False}

        uploaded, queued, upload_message, record_id = False, False, "", None
        try:
            record = client.submit_record(fields, image_path)
            uploaded = True
            record_id = record.get("id")
        except ServerError as e:
            upload_message = e.message
            if e.status == 409:
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
            "upload_message": upload_message,
            "record_id": record_id,
            "flushed": flushed,
        }

    # ========== 生成（启动 + 轮询，COM 在 worker 子进程里）==========
    @app.post("/export")
    def export():
        payload = request.get_json(silent=True) or {}
        template_id = str(payload.get("template_id") or "").strip()
        author = str(payload.get("author") or "").strip()
        title = str(payload.get("title") or "").strip()
        partner_name = str(payload.get("partner_name") or "").strip()

        if not template_id:
            return jsonify({"ok": False, "error": "请先选择模板"}), 400
        if not author or not title:
            return jsonify({"ok": False, "error": "作者和标题都必须填写"}), 400

        cfg = load_config()
        if not cfg.employee_name:
            return jsonify({"ok": False, "error": "请先在设置里填写你的姓名"}), 400

        # 1) 解析本机模板路径（模板归员工本机管，不再从服务器下载）
        try:
            template_file, template = _ensure_template(cfg, template_id)
            _ensure_output_dir(cfg)
        except ServerError as e:
            return jsonify({"ok": False, "error": e.message}), 200

        # 2) 启动 worker 子进程。忙时立刻拒绝——员工连点第二下必须得到明确反馈
        params = {
            "template_path": str(template_file),
            "author": author,
            "title": title,
            "output_dir": cfg.output_dir,
            "output_format": "JPG",
            "jpg_quality": cfg.jpg_quality,
            "author_placeholder": cfg.author_placeholder or "{{作者}}",
            "title_placeholder": cfg.title_placeholder or "{{标题}}",
            # 命名规则在这里收口：非法值一律回落到默认，别把脏配置传进 worker
            "filename_rule": cfg.filename_rule
            if cfg.filename_rule in FILENAME_RULES
            else DEFAULT_FILENAME_RULE,
            # 识别出的真实路径：worker 拿它连接/记日志，员工手填为空时
            # 以前这里传的是空串，日志里看不出到底连的是哪个 PS
            "ps_path": _resolve_ps(cfg).path,
            # 生成当天的日期：离线记录可能隔天才补传，服务端不给就用补传当天，
            # 日报/统计会把它算到错误的一天
            "work_date": date.today().isoformat(),
            # 以下仅用于任务完成后上报，worker 不读
            "template_id": template["id"],
            "template_name": template["name"],
            "partner_name": partner_name,
            "employee_name": cfg.employee_name,
        }
        try:
            task_id = executor.start(params)
        except BusyError as e:
            return jsonify({"ok": False, "error": str(e), "busy": True}), 409
        return jsonify({"ok": True, "task_id": task_id, "state": "started"})

    @app.get("/export/status")
    def export_status():
        task_id = str(request.args.get("task_id") or "").strip()
        if not task_id:
            return jsonify({"ok": False, "error": "缺少 task_id"}), 400

        cfg = load_config()
        status = executor.status(task_id)

        # 终态只处理一次：首次轮询到完成时做上报，结果写 result.json，
        # 之后无论再轮询多少次都返回同一份（幂等）
        if status.get("finished"):
            from .config import app_data_dir

            task_dir = app_data_dir() / "executions" / task_id
            result_path = task_dir / "result.json"
            cached = read_json(result_path)
            if cached is not None:
                return jsonify(cached)

            state = status.get("state")
            if state == "cancelled":
                # 员工自己点的取消，不是失败：绝不能上报一条 failed 记录。
                # 报了的话"今日失败数"里会多出一条查不出原因的记录，成功率
                # 被无端拉低，而真正的原因是"他不想等了"。
                payload = {
                    "ok": False,
                    "cancelled": True,
                    "error": status.get("message", "已取消"),
                }
            elif state == "uncertain":
                payload = {"ok": False, "error": status.get("message", "结果未知"), "uncertain": True}
            elif state == "error":
                payload = {"ok": False, "error": status.get("message", "生成模块异常")}
            elif not status.get("success"):
                payload = {"ok": False, "error": status.get("message") or "生成失败", "warnings": status.get("warnings", [])}
                fields = _record_fields(cfg, task_dir, status, payload["error"])
                try:
                    _submit_fields(cfg, fields, "", success=False)
                except Exception:  # noqa: BLE001 - 失败上报不能挡住给员工的响应
                    logger.warning("失败记录上报异常", exc_info=True)
            else:
                fields = _record_fields(cfg, task_dir, status, "")
                # 用识别出的路径取版本，而不是只认设置里手填的那个：
                # 手填为空时以前这里永远写不出 ps_version，报障时查不到是哪一版
                fields["ps_version"] = _resolve_ps(cfg).version
                payload = _submit_fields(cfg, fields, status.get("image_path", ""), success=True)
                payload["warnings"] = status.get("warnings", [])
                payload["duration_ms"] = status.get("duration_ms", 0)

            payload["task_id"] = task_id
            payload["finished"] = True
            write_json_atomic(result_path, payload)
            return jsonify(payload)

        return jsonify({"ok": True, "state": status.get("state"), "finished": False, **status})

    @app.post("/export/cancel")
    def export_cancel():
        """取消当前生成。

        之前没有这个入口：PS 卡在对话框或大模板上挂住时，前端永远停在
        "正在导出图片… Ns"，按钮一直禁用，员工唯一出路是关掉整个软件。
        现在前端等久了就把主按钮变成「取消生成」。
        """
        payload = request.get_json(silent=True) or {}
        task_id = str(payload.get("task_id") or "").strip()
        if not task_id:
            # 没带 task_id 就取消当前唯一在跑的任务：客户端是单任务模型，
            # 前端也不必自己记 id 才能自保
            task_id = executor.running_task() or ""
        if not task_id:
            return jsonify({"ok": False, "error": "当前没有正在生成的任务"}), 200

        ok, message = executor.cancel(task_id)
        return jsonify({"ok": ok, "task_id": task_id, "error": None if ok else message, "message": message}), 200

    def _log_tail(lines: int = 3, max_chars: int = 300) -> str:
        """取 worker 日志尾部，给失败记录当"现场"。

        员工只会说"生不出来"，而 PS 的真实报错在 worker.log 里。把最后几行带上，
        后台详情页就能直接看到原因，不用再让对方去找 %APPDATA% 下的日志文件。
        """
        from .config import app_data_dir

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

    def _record_fields(cfg: ClientConfig, task_dir: Path, status: dict, error_message: str) -> dict:
        params = read_json(task_dir / "params.json") or {}
        warnings = list(status.get("warnings") or [])
        if not status.get("success"):
            tail = _log_tail()
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
