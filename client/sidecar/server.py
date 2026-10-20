"""
员工客户端 sidecar

Tauri 前端通过 HTTP 调它（只绑 127.0.0.1）。为什么用 HTTP 而不是 stdio JSON-RPC：
Python 侧只需要一个 Flask，前端用 fetch 就能调，省掉 Rust 侧的协议桥接代码；
风险面靠"只绑回环 + CORS 白名单"收敛。
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import sys
from pathlib import Path

# 允许从 client/sidecar 直接运行：把仓库根加进 sys.path 才能 import core
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from flask import Flask, jsonify, request, send_file  # noqa: E402

from core.ps_connector import COM_AVAILABLE  # noqa: E402

from . import queue  # noqa: E402
from .api_client import ApiClient, ServerError  # noqa: E402
from .config import ClientConfig, ensure_dirs, load_config, save_config, template_dir_of  # noqa: E402
from .executor import BusyError, Executor  # noqa: E402
from .worker import read_json, write_json_atomic  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

# CORS 白名单：Tauri 生产环境是 tauri://localhost，开发是 vite 的 5173
ALLOWED_ORIGINS = {
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
}


def _client(cfg: ClientConfig) -> ApiClient:
    return ApiClient(cfg.server_url, cfg.api_key)


TEMPLATE_SUFFIX = ".psd"
# PSD 文件头魔数，用来挡掉改了扩展名的假文件（PS 打不开会在 COM 里报一堆错）
PSD_MAGIC = b"8BPS"
MAX_TEMPLATE_BYTES = 200 * 1024 * 1024
_ILLEGAL_NAME_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def sanitize_psd_name(raw: str) -> str:
    """保留中文的 PSD 文件名清洗。

    不能用 secure_filename：「中国收藏.psd」会被清成「psd」，
    模板名直接变成下拉框里的标签，必须保住中文。
    """
    stem = Path(raw or "").stem
    stem = _ILLEGAL_NAME_CHARS.sub("_", stem).strip().strip(". ")
    return f"{stem or '模板'}{TEMPLATE_SUFFIX}"


def _unique_target(base: Path, filename: str) -> Path:
    """同名模板不覆盖，自动加序号——员工重复上传不会悄悄顶掉旧模板"""
    target = base / filename
    if not target.exists():
        return target
    stem = Path(filename).stem
    for i in range(2, 1000):
        candidate = base / f"{stem}({i}){TEMPLATE_SUFFIX}"
        if not candidate.exists():
            return candidate
    raise OSError("同名模板太多，请先整理模板目录")


def list_local_templates(cfg: ClientConfig) -> list[dict]:
    """扫本机模板目录，列出可用 PSD。

    id 直接用文件名：同一目录内唯一，且能唯一定位到磁盘上的文件，
    不需要再下载或维护额外的 id 映射。
    """
    base = template_dir_of(cfg)
    if not base.is_dir():
        return []

    items: list[dict] = []
    for path in sorted(base.glob(f"*{TEMPLATE_SUFFIX}"), key=lambda p: p.name.lower()):
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
        except OSError as e:  # 权限不足/文件被占用，跳过而不是整个列表报错
            logger.warning("模板文件读取失败 %s: %s", path.name, e)
            continue
        items.append(
            {
                "id": path.name,
                "name": path.stem,
                "filename": path.name,
                "size": size,
            }
        )
    return items


def _ensure_template(cfg: ClientConfig, template_id: str) -> tuple[Path, dict]:
    """把模板 id（文件名）解析成本机可用的 PSD 路径"""
    template = next((t for t in list_local_templates(cfg) if t["id"] == template_id), None)
    if template is None:
        raise ServerError("模板文件找不到了，请到设置里检查模板目录")
    return template_dir_of(cfg) / template["filename"], template


def _ps_version(ps_path: str) -> str:
    """读 Photoshop.exe 的文件版本，读不到就留空（不影响生成）"""
    if not ps_path or not os.path.exists(ps_path):
        return ""
    try:
        import win32api  # type: ignore

        info = win32api.GetFileVersionInfo(ps_path, "\\")
        ms, ls = info["FileVersionMS"], info["FileVersionLS"]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}"
    except Exception:  # noqa: BLE001
        return ""


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
            upload_message = "服务器提示：今天已录入过同一作者+标题"
        else:
            queue.enqueue(fields, str(image_path) if image_path else None)
            queued = True

    # 顺手把之前积压的补传掉
    flushed = queue.flush(client) if uploaded else {
        "sent": 0,
        "skipped": 0,
        "remaining": len(queue.pending()),
        "dead": len(queue.dead_letters()),
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


def create_sidecar() -> Flask:
    ensure_dirs()
    app = Flask(__name__)
    # 模板上传是唯一的大文件入口，给足余量并留个好读的报错
    app.config["MAX_CONTENT_LENGTH"] = MAX_TEMPLATE_BYTES * 3
    executor = Executor()

    @app.errorhandler(413)
    def too_large(_err):
        limit = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
        return jsonify({"ok": False, "error": f"文件太大，单次上传合计上限 {limit} MB"}), 413

    @app.after_request
    def cors(resp):
        origin = request.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
            resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
        return resp

    @app.route("/<path:_any>", methods=["OPTIONS"])
    def preflight(_any):
        return ("", 204)

    # ========== 状态 ==========
    @app.get("/health")
    def health():
        return jsonify({"ok": True, "com_available": COM_AVAILABLE, "platform": sys.platform})

    @app.get("/status")
    def status():
        cfg = load_config()
        client = _client(cfg)
        return jsonify(
            {
                "ok": True,
                "server_reachable": client.health(),
                "com_available": COM_AVAILABLE,
                "platform": platform.platform(),
                "config": cfg.to_dict(),
                "pending": len(queue.pending()),
                "dead": len(queue.dead_letters()),
                "machine": platform.node(),
                "busy": executor.running_task() is not None,
            }
        )

    # ========== 配置 ==========
    @app.get("/config")
    def get_config():
        return jsonify({"ok": True, "config": load_config().to_dict()})

    @app.post("/config")
    def set_config():
        payload = request.get_json(silent=True) or {}
        cfg = load_config()
        for key, value in payload.items():
            if hasattr(cfg, key) and value is not None:
                setattr(cfg, key, value)
        save_config(cfg)
        logger.info("配置已保存 employee=%s server=%s", cfg.employee_name, cfg.server_url)
        return jsonify({"ok": True, "config": cfg.to_dict()})

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
            "ps_path": cfg.ps_path,
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
            if state == "uncertain":
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
                fields["ps_version"] = _ps_version(cfg.ps_path)
                payload = _submit_fields(cfg, fields, status.get("image_path", ""), success=True)
                payload["warnings"] = status.get("warnings", [])
                payload["duration_ms"] = status.get("duration_ms", 0)

            payload["task_id"] = task_id
            payload["finished"] = True
            write_json_atomic(result_path, payload)
            return jsonify(payload)

        return jsonify({"ok": True, "state": status.get("state"), "finished": False, **status})

    def _record_fields(cfg: ClientConfig, task_dir: Path, status: dict, error_message: str) -> dict:
        params = read_json(task_dir / "params.json") or {}
        return {
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
            "warnings": json.dumps(status.get("warnings", []), ensure_ascii=False) if status.get("warnings") else "",
            "processing_ms": status.get("duration_ms", 0),
        }

    # ========== 历史（客户端只看自己的）==========
    @app.get("/history")
    def history():
        cfg = load_config()
        if not cfg.employee_name:
            return jsonify({"ok": True, "items": []})
        try:
            items = _client(cfg).list_records(cfg.employee_name, size=60)
            return jsonify({"ok": True, "items": items})
        except ServerError as e:
            return jsonify({"ok": False, "error": e.message, "items": []}), 200

    @app.get("/history/<int:record_id>/image")
    def history_image(record_id: int):
        from flask import Response

        cfg = load_config()
        try:
            content = _client(cfg).fetch_image(record_id, thumb=request.args.get("full") != "1")
        except ServerError as e:
            return jsonify({"ok": False, "error": e.message}), e.status or 404
        return Response(content, mimetype="image/jpeg")

    # ========== 本地图片预览 ==========
    @app.get("/preview")
    def preview():
        cfg = load_config()
        raw = request.args.get("path", "")
        if not raw:
            return jsonify({"ok": False, "error": "缺少 path"}), 400

        output_root = Path(cfg.output_dir).resolve()
        target = Path(raw).resolve()
        # 只允许读输出目录内的文件，防路径穿越
        if output_root not in target.parents and target != output_root:
            return jsonify({"ok": False, "error": "非法路径"}), 403
        if not target.exists():
            return jsonify({"ok": False, "error": "文件不存在"}), 404

        return send_file(target, conditional=True)

    @app.post("/open-folder")
    def open_folder():
        """生成后"打开文件夹"按钮"""
        cfg = load_config()
        raw = request.args.get("path") or cfg.output_dir
        target = Path(raw)
        if target.is_file():
            target = target.parent
        try:
            if sys.platform == "win32":
                os.startfile(target)  # type: ignore[attr-defined]
            else:
                return jsonify({"ok": False, "error": "仅 Windows 支持"}), 400
            return jsonify({"ok": True})
        except OSError as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    # ========== 离线队列 ==========
    @app.get("/queue")
    def queue_status():
        return jsonify(
            {
                "ok": True,
                "pending": len(queue.pending()),
                "dead": len(queue.dead_letters()),
            }
        )

    @app.post("/queue/flush")
    def queue_flush():
        cfg = load_config()
        return jsonify({"ok": True, **queue.flush(_client(cfg))})

    @app.post("/queue/retry-dead")
    def queue_retry_dead():
        """把死信搬回队列并立刻试着补传一次。"""
        moved = queue.retry_dead()
        cfg = load_config()
        flushed = queue.flush(_client(cfg)) if moved["restored"] else {
            "sent": 0, "skipped": 0, "remaining": len(queue.pending()), "dead": moved["dead"],
        }
        return jsonify({"ok": True, "restored": moved["restored"], "failed": moved["failed"], **flushed})

    return app


def main() -> None:
    # PyInstaller 打包后 worker 由同一个 exe 以 --run-worker 方式拉起
    if "--run-worker" in sys.argv:
        idx = sys.argv.index("--run-worker")
        from .worker import run

        run(Path(sys.argv[idx + 1]))
        return

    port = int(os.getenv("SIDECAR_PORT", "8765"))
    app = create_sidecar()
    logger.info("sidecar 启动 http://127.0.0.1:%s", port)
    # 只绑回环：局域网内其他机器不应能调用本机 PS
    app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
