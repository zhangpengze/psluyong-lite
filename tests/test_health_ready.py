"""就绪探针：数据库 + 数据目录可写 + 调度器存活。

为什么值得单独一个文件：这个端点被 compose 的 healthcheck 直接消费，
它说什么运维就信什么。以前它只做 SELECT 1——磁盘满、数据卷只读时
读取照样成功，写入却全 500，而探针仍然报"就绪"。这类故障最难查，
因为所有仪表都是绿的。
"""

from __future__ import annotations

import pytest

from server.app.services import health_service


# ---------- 整体形状 ----------

def test_ready_returns_component_detail(client):
    resp = client.get("/health/ready")
    assert resp.status_code == 200
    body = resp.get_json()
    # 老字段向后兼容：编排脚本/监控可能还在读 db
    assert body["ok"] is True and body["status"] == "ready" and body["db"] == "ok"
    assert set(body["checks"]) == {"db", "data_dir", "scheduler"}
    assert body["checks"]["data_dir"]["state"] == "ok"


def test_health_stays_liveness_only(client):
    body = client.get("/health").get_json()
    assert body == {"ok": True, "status": "alive"}


# ---------- 致命项：数据库 ----------

def test_db_down_is_503_and_hides_detail(client, monkeypatch):
    def boom():
        raise RuntimeError("connection refused: postgres://user:pw@10.0.0.9:5432")

    monkeypatch.setattr(health_service, "ping_database", boom)
    resp = client.get("/health/ready")

    assert resp.status_code == 503
    body = resp.get_json()
    assert body["status"] == "not_ready" and body["db"] == "error"
    assert body["checks"]["db"]["detail"] == "RuntimeError"
    raw = resp.get_data(as_text=True)
    assert "10.0.0.9" not in raw and "postgres://" not in raw, "探针失败不许把连接串吐给调用方"


# ---------- 致命项：数据目录可写 ----------

def test_data_dir_not_writable_is_503(client, monkeypatch):
    def boom():
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(health_service, "probe_data_dir", boom)
    resp = client.get("/health/ready")

    assert resp.status_code == 503
    body = resp.get_json()
    assert body["checks"]["data_dir"]["state"] == "error"
    assert body["checks"]["db"]["state"] == "ok", "一项失败不能连带把其他项也标红"
    assert "No space left" not in resp.get_data(as_text=True)


def test_real_probe_writes_fsyncs_and_cleans_up(tmp_path, monkeypatch):
    """真走一遍 open/write/fsync/close：权限检查能过、写入会失败的场景太多。"""
    from server.app import config as config_mod

    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path)
    health_service.probe_data_dir()
    assert list(tmp_path.iterdir()) == [], "探针文件必须自己清掉，不能在数据目录留垃圾"


def test_real_probe_reports_error_and_leaves_nothing_behind(tmp_path, monkeypatch):
    from server.app import config as config_mod

    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path)

    def boom(_fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(health_service.os, "fsync", boom)
    with pytest.raises(OSError):
        health_service.probe_data_dir()
    assert list(tmp_path.iterdir()) == [], "写失败也要把半截文件删掉"


# ---------- 非致命项：调度器 ----------

def test_scheduler_not_started_is_off_not_failure(client):
    """多进程部署时没跑调度器的进程本来就该没有 job，算成就绪失败是误报。"""
    body = client.get("/health/ready").get_json()
    assert body["checks"]["scheduler"]["state"] == "off"
    assert body["ok"] is True


def test_scheduler_enabled_but_not_started_stays_off(monkeypatch):
    """conftest 里 ENABLE_SCHEDULER=0，这条把开关打开、调度器仍为空——
    覆盖的是「配置说该跑、实际没跑」这一支，它必须是 off 而不是 error。"""
    from bot import scheduler as sched
    from server.app.config import Config

    monkeypatch.setattr(Config, "ENABLE_SCHEDULER", True)
    monkeypatch.setattr(sched, "_scheduler", None)

    state, detail = health_service.scheduler_state()
    assert state == "off" and "未启动" in detail


def test_scheduler_disabled_by_config_is_off(client, monkeypatch):
    from server.app.config import Config

    monkeypatch.setattr(Config, "ENABLE_SCHEDULER", False)
    state, detail = health_service.scheduler_state()
    assert state == "off" and "ENABLE_SCHEDULER" in detail


def test_scheduler_running_without_jobs_is_flagged(monkeypatch):
    """真正的坏形态：说自己在跑，却一个 job 都没有 —— 日报永远不会推。"""
    from bot import scheduler as sched
    from server.app.config import Config

    monkeypatch.setattr(Config, "ENABLE_SCHEDULER", True)  # conftest 里默认关掉调度器
    monkeypatch.setattr(
        sched, "_scheduler", type("S", (), {"running": True, "get_jobs": lambda self: []})()
    )
    state, detail = health_service.scheduler_state()
    assert state == "error" and "没有任何 job" in detail


def test_scheduler_running_with_jobs_is_ok(monkeypatch):
    from datetime import datetime

    from bot import scheduler as sched
    from server.app.config import Config

    monkeypatch.setattr(Config, "ENABLE_SCHEDULER", True)
    job = type("J", (), {"next_run_time": datetime(2026, 10, 25, 8, 30)})()

    monkeypatch.setattr(
        sched,
        "_scheduler",
        type("S", (), {"running": True, "get_jobs": lambda self: [job, job]})(),
    )
    state, detail = health_service.scheduler_state()
    assert state == "ok" and "2 个 job" in detail and "10-25 08:30" in detail


def test_scheduler_get_jobs_failure_does_not_raise(monkeypatch):
    from bot import scheduler as sched

    def boom(self):
        raise RuntimeError("executor is shut down")

    monkeypatch.setattr(
        sched, "_scheduler", type("S", (), {"running": True, "get_jobs": boom})()
    )
    info = sched.scheduler_status()
    assert info == {"running": True, "jobs": 0, "next_run": None}


def test_scheduler_status_without_scheduler():
    from bot import scheduler as sched

    assert sched.scheduler_status() == {"running": False, "jobs": 0, "next_run": None}


def test_scheduler_error_does_not_break_readiness(client, monkeypatch):
    """调度器探针自己炸了也只是 unknown，不能让整站判成未就绪。"""

    def boom():
        raise RuntimeError("cannot inspect")

    monkeypatch.setattr(health_service, "scheduler_state", boom)
    resp = client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.get_json()["checks"]["scheduler"]["state"] == "unknown"
