"""模板与合作伙伴字典路由（拆自 server.py）：/templates* /dicts。

模板和合作伙伴名单都由员工本机维护（存在配置里），断网也能用；
本文件只做编排，文件系统判断在 file_ops.py。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from flask import Flask, jsonify, request

from .config import load_config, template_dir_of
from .file_ops import (
    MAX_TEMPLATE_BYTES,
    PSD_MAGIC,
    TEMPLATE_SUFFIX,
    _unique_target,
    list_local_templates,
    sanitize_psd_name,
)

logger = logging.getLogger(__name__)


def register_template_routes(app: Flask) -> None:
    # ========== 模板 / 合作伙伴（都由员工本机维护）==========
    @app.get("/templates")
    def templates():
        cfg = load_config()
        base = template_dir_of(cfg)
        items = list_local_templates(cfg)
        payload: dict = {"ok": True, "items": items, "template_dir": str(base)}
        if not base.is_dir():
            payload["warning"] = "模板目录不存在，请到设置里填写正确的路径"
        elif not items:
            payload["warning"] = f"模板目录里还没有 {TEMPLATE_SUFFIX} 文件"
        return jsonify(payload)

    @app.post("/templates/upload")
    def upload_templates():
        """员工上传 PSD：一次可选多个，落到本机模板目录"""
        files = request.files.getlist("files")
        if not files:
            return jsonify({"ok": False, "error": "没有收到文件"}), 400

        cfg = load_config()
        base = template_dir_of(cfg)
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return jsonify({"ok": False, "error": f"模板目录不可写：{e}"}), 200

        saved: list[str] = []
        errors: list[str] = []
        for item in files:
            raw_name = item.filename or ""
            if not raw_name.lower().endswith(TEMPLATE_SUFFIX):
                errors.append(f"{raw_name or '(未命名)'}：只支持 {TEMPLATE_SUFFIX} 文件")
                continue
            head = item.stream.read(len(PSD_MAGIC))
            item.stream.seek(0, os.SEEK_END)
            size = item.stream.tell()
            item.stream.seek(0)
            if head != PSD_MAGIC:
                errors.append(f"{raw_name}：文件内容不是 PSD（可能只是改了扩展名）")
                continue
            if size > MAX_TEMPLATE_BYTES:
                limit = MAX_TEMPLATE_BYTES // (1024 * 1024)
                errors.append(f"{raw_name}：超过单文件上限 {limit} MB")
                continue
            try:
                target = _unique_target(base, sanitize_psd_name(raw_name))
                item.save(target)
            except OSError as e:
                errors.append(f"{raw_name}：保存失败（{e}）")
                continue
            saved.append(target.name)
            logger.info("模板已上传 %s (%d KB)", target.name, target.stat().st_size // 1024)

        return jsonify(
            {
                "ok": bool(saved),
                "saved": saved,
                "errors": errors,
                "items": list_local_templates(cfg),
            }
        )

    @app.post("/templates/delete")
    def delete_template():
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name") or "")
        # 只允许删模板目录里的一级文件，挡住 ../ 之类的路径穿越
        if not name or Path(name).name != name or not name.lower().endswith(TEMPLATE_SUFFIX):
            return jsonify({"ok": False, "error": "模板名不合法"}), 400

        cfg = load_config()
        target = template_dir_of(cfg) / name
        try:
            target.unlink()
        except FileNotFoundError:
            return jsonify({"ok": False, "error": "模板已不存在"}), 200
        except OSError as e:
            return jsonify({"ok": False, "error": f"删除失败：{e}"}), 200

        logger.info("模板已删除 %s", name)
        return jsonify({"ok": True, "items": list_local_templates(cfg)})

    @app.post("/templates/open-dir")
    def open_template_dir():
        """把模板目录建出来再打开，省得员工自己找路径"""
        cfg = load_config()
        base = template_dir_of(cfg)
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return jsonify({"ok": False, "error": f"无法创建目录：{e}"}), 200
        try:
            if sys.platform == "win32":
                os.startfile(base)  # type: ignore[attr-defined]
            else:
                return jsonify({"ok": False, "error": "仅 Windows 支持"}), 200
        except OSError as e:
            return jsonify({"ok": False, "error": f"打开失败：{e}"}), 200
        return jsonify({"ok": True, "path": str(base)})

    @app.get("/dicts")
    def dicts():
        """合作伙伴名单存在本机配置里，断网也能选"""
        cfg = load_config()
        return jsonify({"ok": True, "grouped": {"partner": cfg.partners}})
