"""历史记录路由（拆自 server.py）：/history 区间查询、/history/<id>/image 图片代理。

客户端只看自己的记录；区间语义（today/7/30/all/custom）在这里收口，
_client 由 create_sidecar 晚绑定传入（monkeypatch 兼容见 server.py）。
"""

from __future__ import annotations

from datetime import date, timedelta

from flask import Flask, jsonify, request

from .api_client import ServerError
from .config import load_config


def register_history_routes(app: Flask, _client) -> None:
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
