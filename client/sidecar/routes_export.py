"""生成任务路由（拆自 server.py）：/export 启动、/export/status 轮询、/export/cancel 取消。

COM 跑在 worker 子进程（executor），这里只编排：校验参数 → 起任务 →
终态交给 settler 结算（记录上报 / 离线入队 / 缓存），本文件不再碰数据库和网络。
_client 由 create_sidecar 晚绑定传入，测试 monkeypatch 必须在请求时生效。
"""

from __future__ import annotations

import logging
import time
from datetime import date

from flask import Flask, jsonify, request

from core.runner import DEFAULT_FILENAME_RULE, FILENAME_RULES  # noqa: E402

from . import settler, task_paths
from .api_client import ServerError
from .config import app_data_dir, load_config
from .executor import BusyError
from .file_ops import _ensure_output_dir, _ensure_template
from .ps_info import _resolve_ps
from .worker import read_json

logger = logging.getLogger(__name__)

# 另一个线程正在结算时，最多等它这么久再回「还在处理」
SETTLE_WAIT_S = 5.0


def _wait_settled(task_id: str, timeout_s: float = SETTLE_WAIT_S) -> dict | None:
    """等另一个线程把 result.json 写出来（很短，正常几十毫秒）"""
    task_dir = task_paths.task_dir(app_data_dir(), task_id)
    if task_dir is None:
        return None
    path = task_dir / "result.json"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        cached = read_json(path)
        if cached is not None:
            return cached
        time.sleep(0.05)
    return None


def register_export_routes(app: Flask, executor, _client) -> None:
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
        if not task_paths.is_valid(task_id):
            # 400 而不是 404：这个 id 永远不可能是我们发的，说清是参数问题
            logger.warning("拒绝非法的 task_id（轮询）: %r", task_id)
            return jsonify({"ok": False, "error": task_paths.reject_message()}), 400

        status = executor.status(task_id)
        if not status.get("finished"):
            return jsonify({"ok": True, "state": status.get("state"), "finished": False, **status})

        # 终态只结算一次（结果写 result.json，之后每次轮询返回同一份）。
        # 上报本身不归这个接口：员工关窗/断线时没人轮询，记录会永久丢失，
        # 所以结算逻辑在 settler 里，后台线程和启动对账走的是同一个幂等函数。
        payload = settler.settle_task(task_id, _client, status=status)
        if payload is None:
            # 后台线程正在结算同一个任务：等它一会儿，别让员工多等一个轮询周期
            payload = _wait_settled(task_id)
        if payload is None:
            return jsonify({"ok": True, "state": status.get("state"), "finished": False, "settling": True})
        return jsonify(payload)

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
        if not task_paths.is_valid(task_id):
            # 不带 task_id 时取的是 executor 自己记的 id（必然合法），
            # 能走到这里的只有前端传上来的串
            logger.warning("拒绝非法的 task_id（取消）: %r", task_id)
            return jsonify({"ok": False, "error": task_paths.reject_message()}), 400

        ok, message = executor.cancel(task_id)
        return jsonify({"ok": ok, "task_id": task_id, "error": None if ok else message, "message": message}), 200
