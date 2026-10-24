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
from datetime import date, timedelta
from pathlib import Path

# 允许从 client/sidecar 直接运行：把仓库根加进 sys.path 才能 import core
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from flask import Flask, jsonify, request, send_file  # noqa: E402

from core.ps_connector import COM_AVAILABLE  # noqa: E402
from core.runner import DEFAULT_FILENAME_RULE, FILENAME_RULES  # noqa: E402

from . import __version__, queue  # noqa: E402
from .api_client import ApiClient, ServerError  # noqa: E402
from .config import (
    ClientConfig,
    ensure_dirs,
    load_config,
    sanitize_config_patch,
    save_config,
    template_dir_of,
)  # noqa: E402
from .executor import BusyError, Executor  # noqa: E402
from .ps_detect import PSInstall, detect_photoshop  # noqa: E402
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


def _ensure_output_dir(cfg: ClientConfig) -> Path:
    """生成前预检输出目录：相对路径/不可写在这里就拦下来。

    不预检的代价：PS 白跑一轮 COM，最后报一句「文件不存在」，
    员工根本想不到是设置里的输出目录填错了（真实踩坑：填了「姓名」）。
    """
    raw = (cfg.output_dir or "").strip()
    if not raw:
        raise ServerError("输出目录未设置，请到设置里填写")
    target = Path(raw)
    if not target.is_absolute():
        raise ServerError(
            f"输出目录「{raw}」不是完整路径（应以盘符如 D:\\ 开头），请在设置里修正"
        )
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ServerError(f"输出目录「{raw}」不可用：{e}，请在设置里修正") from None
    return target


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


def _resolve_ps(cfg) -> PSInstall:
    """当前机器上的 Photoshop：手填优先，否则自动识别（任意版本/任意盘符）。

    识别结果只用于两件事——给记录写 ps_version、以及在设置页展示。
    COM 连接本身用版本无关的 ProgID，装哪版都能连，所以识别不到
    也不该拦住生成（员工可以手动指定路径）。
    """
    return detect_photoshop(getattr(cfg, "ps_path", "") or "")


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
        elif e.status and e.status < 500:
            # 4xx 是"服务端明确不要这一条"，重试永远不会成功：
            #   401/403 → API Key 错了；400 → 参数或图片不合法；
            #   404 → 客户端和服务端版本对不上
            # 塞进离线队列只会变成永远补不成功的毒记录，每次启动重试一遍，
            # 最后沉淀成死信，员工看到"有 N 条记录补传失败"却不知道原因在配置里。
            upload_message = f"服务器拒绝了这条记录（{e.status}）：{e.message}"
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

    @app.before_request
    def reject_cross_site_writes():
        """写操作自己判来源。

        CORS 只约束"能不能读响应"，约束不了"请求是否被执行"——恶意网页用
        form 提交（无需预检）照样能让本机 sidecar 改配置、打开目录。
        带 Origin 的写请求必须在白名单里；不带 Origin 的是本机程序（curl、
        PowerShell、Tauri 内部），放行。
        """
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        origin = request.headers.get("Origin")
        if origin and origin not in ALLOWED_ORIGINS:
            logger.warning("拒绝跨站写请求 origin=%s path=%s", origin, request.path)
            return jsonify({"ok": False, "error": "请求来源不被允许"}), 403
        return None

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
        from .config import app_data_dir

        return jsonify(
            {
                "ok": True,
                "server_reachable": client.health(),
                "com_available": COM_AVAILABLE,
                "platform": platform.platform(),
                # 本机 Photoshop 的自动识别结果：路径/版本/怎么找到的。
                # 员工不必再手填路径——装哪版、装在哪个盘都能认出来
                "ps": _resolve_ps(cfg).to_dict(),
                "config": cfg.to_dict(),
                "pending": len(queue.pending()),
                "dead": len(queue.dead_letters()),
                "machine": platform.node(),
                "busy": executor.running_task() is not None,
                # 版本与日志路径：员工报障时直接复制走，不用再问"你装的哪版"
                "version": __version__,
                "log_dir": str(app_data_dir() / "logs"),
            }
        )

    # ========== Photoshop 自动识别 ==========
    @app.get("/ps")
    def get_ps():
        """当前机器上的 Photoshop（手填优先，否则自动识别）。"""
        return jsonify({"ok": True, "ps": _resolve_ps(load_config()).to_dict()})

    @app.post("/ps/detect")
    def detect_ps():
        """重新识别：装完 PS / 换版本后点一下"重新识别"就走这里。

        故意不做成写配置：识别是运行时的事，把结果落到 ps_path 会让
        "自动识别"悄悄变成"写死路径"，员工下次换版本反而对不上。
        """
        return jsonify({"ok": True, "ps": _resolve_ps(load_config()).to_dict()})

    # ========== 配置 ==========
    @app.get("/config")
    def get_config():
        return jsonify({"ok": True, "config": load_config().to_dict()})

    @app.post("/config")
    def set_config():
        payload = request.get_json(silent=True) or {}
        cfg = load_config()
        clean, errors = sanitize_config_patch(payload)
        if errors:
            # 整包拒绝：只写一半会让配置文件处在"部分生效"的状态，
            # 员工看到的界面和实际行为对不上，比直接报错难查得多
            logger.warning("配置保存被拒绝: %s", "；".join(errors))
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "；".join(errors),
                        "config": cfg.to_dict(),
                    }
                ),
                400,
            )
        for key, value in clean.items():
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

    # ========== 历史（客户端只看自己的）==========
    # 服务端列表接口默认区间是「今天往前 7 天」，客户端要自己声明区间，
    # 否则「累计记录」会缩水成「最近 7 天」。
    # today/7/30/all 是快捷档；custom 是员工自己在界面上选的起止日期
    # （"我想看 9 月那批"），起止都由前端给，这里只做合法性与方向校验。
    HISTORY_RANGES = {"today": 1, "7": 7, "30": 30, "all": 0}
    HISTORY_ALL_START = date(2000, 1, 1)

    def _query_date(name):
        """YYYY-MM-DD → date；空或脏值返回 None（脏值不该让整页记录挂掉）"""
        raw = (request.args.get(name) or "").strip()
        if not raw:
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None

    @app.get("/history")
    def history():
        cfg = load_config()
        if not cfg.employee_name:
            return jsonify({"ok": True, "items": [], "total": 0, "range": "7", "start": "", "end": ""})

        today = date.today()
        raw_range = str(request.args.get("range") or "7")

        if raw_range == "custom":
            start_d = _query_date("start")
            end_d = _query_date("end")
            # 只填一头就补另一头：只填起 → 到今天；只填止 → 就只看那一天。
            # 两头都没填等于没筛，退回近 7 天（不能让空区间把列表清空）。
            if start_d and not end_d:
                end_d = today
            elif end_d and not start_d:
                start_d = end_d
            if not start_d or not end_d:
                raw_range = "7"

        if raw_range != "custom":
            if raw_range not in HISTORY_RANGES:
                raw_range = "7"
            days = HISTORY_RANGES[raw_range]
            end_d = today
            start_d = HISTORY_ALL_START if days == 0 else today - timedelta(days=days - 1)

        if start_d > end_d:
            start_d, end_d = end_d, start_d
        # 跨度大的区间才值得拉满：近 7 天拉 200 条纯属浪费带宽，而"全部"或
        # 自选一个月必须给足，否则界面上「共 120 条 · 只显示最近 60 条」很难看。
        size = 200 if (end_d - start_d).days >= 30 else 60

        try:
            data = _client(cfg).list_records(
                cfg.employee_name, size=size, start=start_d.isoformat(), end=end_d.isoformat()
            )
            return jsonify(
                {
                    "ok": True,
                    "items": data["items"],
                    "total": data["total"],
                    "range": raw_range,
                    # 回显真正生效的区间：前端直接显示它，省得自己再算一遍
                    # （"只填了起始日"到底查到哪天，界面上必须能看见）
                    "start": start_d.isoformat(),
                    "end": end_d.isoformat(),
                    "size": size,
                }
            )
        except ServerError as e:
            return jsonify(
                {
                    "ok": False,
                    "error": e.message,
                    "items": [],
                    "total": 0,
                    "range": raw_range,
                    "start": start_d.isoformat(),
                    "end": end_d.isoformat(),
                }
            ), 200

    @app.get("/history/<int:record_id>/image")
    def history_image(record_id: int):
        from flask import Response

        cfg = load_config()
        try:
            content = _client(cfg).fetch_image(record_id, thumb=request.args.get("full") != "1")
        except ServerError as e:
            return jsonify({"ok": False, "error": e.message}), e.status or 404
        resp = Response(content, mimetype="image/jpeg")
        # 图片是可变资源（同一条记录可能被重新生成/管理员替换），前端带 ?v=created_at
        # 做版本区分；这里只是允许浏览器缓存，避免每次进记录页都重拉几十张缩略图
        resp.headers["Cache-Control"] = "private, max-age=86400"
        return resp

    # ========== 打开目录 / 日志 ==========
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

    @app.post("/logs/open")
    def open_log_dir():
        """打开日志目录：路径由服务端固定，不接受参数（免得变成任意目录打开器）"""
        from .config import app_data_dir

        log_dir = app_data_dir() / "logs"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return jsonify({"ok": False, "error": f"无法创建日志目录：{e}"}), 200
        if sys.platform != "win32":
            return jsonify({"ok": False, "error": "仅 Windows 支持"}), 200
        try:
            os.startfile(log_dir)  # type: ignore[attr-defined]
        except OSError as e:
            return jsonify({"ok": False, "error": f"打开失败：{e}"}), 200
        return jsonify({"ok": True, "path": str(log_dir)})

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


def _attach_file_logger(filename: str) -> None:
    """sidecar 自己的日志落盘（实现与 worker 共用，避免两份格式化逻辑）"""
    from .worker import attach_file_logger

    attach_file_logger(filename)


def main() -> None:
    # PyInstaller 打包后 worker 由同一个 exe 以 --run-worker 方式拉起
    if "--run-worker" in sys.argv:
        idx = sys.argv.index("--run-worker")
        from .worker import run

        run(Path(sys.argv[idx + 1]))
        return

    _attach_file_logger("sidecar.log")
    port = int(os.getenv("SIDECAR_PORT", "8765"))
    app = create_sidecar()
    logger.info("sidecar 启动 http://127.0.0.1:%s", port)
    # 只绑回环：局域网内其他机器不应能调用本机 PS
    app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
