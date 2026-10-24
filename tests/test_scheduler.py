"""调度器测试：不起真 APScheduler 线程，假后端只记录注册参数；
日报 / 夜维的推送出口统一 monkeypatch 捕获，断言捕获值而非内部调用序列"""

from __future__ import annotations

import logging

import pytest

from bot import report as report_mod
from bot import scheduler as scheduler_mod
from server.app.config import Config
import server.backup as backup_mod


@pytest.fixture(autouse=True)
def _fresh():
    """每个用例前后经公开 API 清全局调度器，避免用例间互相污染"""
    scheduler_mod.stop_scheduler()
    yield
    scheduler_mod.stop_scheduler()


class _FakeScheduler:
    """替身后台调度器：记录注册参数与生命周期，不起真实线程"""

    created: list = []

    def __init__(self, timezone=None, **kwargs):
        self.timezone = timezone
        self.jobs: dict[str, dict] = {}
        self.started = False
        self.shutdown_waits: list = []
        _FakeScheduler.created.append(self)

    def add_job(self, func, trigger, **kwargs):
        self.jobs[kwargs["id"]] = {"func": func, "trigger": trigger, **kwargs}

    def start(self):
        self.started = True

    def shutdown(self, wait=True):
        self.shutdown_waits.append(wait)


@pytest.fixture
def backend(monkeypatch):
    """把 BackgroundScheduler 换成假后端，返回创建实例列表"""
    _FakeScheduler.created.clear()
    monkeypatch.setattr(
        "apscheduler.schedulers.background.BackgroundScheduler", _FakeScheduler
    )
    return _FakeScheduler.created


@pytest.fixture
def ops_alerts(monkeypatch):
    """捕获 push_ops_alert——调度器两条链路唯一的推送出口"""
    captured: list[tuple[str, str]] = []

    def fake_alert(title: str, detail: str = ""):
        captured.append((title, detail))
        return True, "已捕获"

    monkeypatch.setattr(report_mod, "push_ops_alert", fake_alert)
    return captured


def _cron_hm(trigger) -> tuple[str, str]:
    """CronTrigger 的 (hour, minute) 字符串表达，如 ("18", "0")"""
    vals = {f.name: str(f) for f in trigger.fields}
    return vals["hour"], vals["minute"]


def _start(app, backend) -> _FakeScheduler:
    scheduler_mod.start_scheduler(app)
    assert len(backend) == 1, "start_scheduler 应只创建一个调度器实例"
    return backend[0]


def _run_daily_job(sched) -> None:
    """取回注册的日报任务并执行一次（模拟 cron 触发）"""
    sched.jobs["daily_report"]["func"]()


# ---------- 调度器注册 / 生命周期 ----------

def test_start_registers_two_jobs_on_config_schedule(app, backend, monkeypatch):
    """注册契约：日报/夜维按 Config 时点挂载，错过 1 小时内补跑"""
    monkeypatch.setattr(Config, "DAILY_REPORT_HOUR", 6)
    monkeypatch.setattr(Config, "DAILY_REPORT_MINUTE", 30)
    monkeypatch.setattr(Config, "BACKUP_HOUR", 4)
    monkeypatch.setattr(Config, "BACKUP_MINUTE", 15)

    sched = _start(app, backend)

    assert sched.started is True
    assert str(sched.timezone) == Config.TZ
    assert set(sched.jobs) == {"daily_report", "nightly_maintenance"}
    # cron 时点必须来自 Config（防止写死后配了不生效）
    assert _cron_hm(sched.jobs["daily_report"]["trigger"]) == ("6", "30")
    assert _cron_hm(sched.jobs["nightly_maintenance"]["trigger"]) == ("4", "15")
    # 夜维任务挂的是真实函数（防止注册了空壳）
    assert sched.jobs["nightly_maintenance"]["func"] is scheduler_mod.nightly_job
    assert callable(sched.jobs["daily_report"]["func"])
    for job in sched.jobs.values():
        assert job["misfire_grace_time"] == 3600  # 重启错过时点 1h 内补跑
        assert job["coalesce"] is True            # 连续错过只补跑一次


def test_duplicate_start_is_ignored(app, backend):
    """重复启动只生效一次：第二次直接返回，不再建第二个调度器"""
    _start(app, backend)
    scheduler_mod.start_scheduler(app)

    assert len(backend) == 1
    assert set(backend[0].jobs) == {"daily_report", "nightly_maintenance"}


def test_start_warns_when_no_mirror_configured(app, backend, monkeypatch, caplog):
    """没配 IMAGES_MIRROR_DIR：启动时必须告警（盘坏即全部丢失）"""
    monkeypatch.setattr(Config, "IMAGES_MIRROR_DIR", "")
    with caplog.at_level(logging.WARNING, logger="bot.scheduler"):
        scheduler_mod.start_scheduler(app)

    assert any("IMAGES_MIRROR_DIR" in r.getMessage() for r in caplog.records)


def test_stop_scheduler_shuts_down_and_resets(app, backend):
    """stop：不等待线程即关闭；全局清零后可以重新启动"""
    sched = _start(app, backend)
    scheduler_mod.stop_scheduler()
    assert sched.shutdown_waits == [False]

    scheduler_mod.start_scheduler(app)  # 全局已清零 → 会建新实例
    assert len(backend) == 2


# ---------- 定时日报 job ----------

def test_daily_job_runs_report_and_stays_quiet(app, backend, ops_alerts, monkeypatch):
    """推送成功：跑一次日报、push=True、透传 app，不发任何运维告警"""
    calls = []

    def fake_run_daily(a, day=None, push=True, record_status=True):
        calls.append({"app": a, "push": push})
        return {"pushed": True, "message": "推送成功",
                "date": "2026-10-21", "path": "x"}

    monkeypatch.setattr(report_mod, "run_daily", fake_run_daily)
    sched = _start(app, backend)
    _run_daily_job(sched)

    assert len(calls) == 1
    assert calls[0]["push"] is True
    assert calls[0]["app"] is app
    assert ops_alerts == []


def test_daily_job_alerts_when_configured_but_push_failed(app, backend, ops_alerts, monkeypatch):
    """配了机器人却推送失败 → 必须推群告警（后台徽标没人天天点开看）"""
    monkeypatch.setattr(Config, "NAPCAT_WEBHOOK", "http://napcat.test:3000")
    monkeypatch.setattr(report_mod, "run_daily",
                        lambda *a, **k: {"pushed": False, "message": "已重试 3 次",
                                         "date": "2026-10-21", "path": "x"})
    sched = _start(app, backend)
    _run_daily_job(sched)

    assert ops_alerts == [("定时日报未推送成功", "日期 2026-10-21｜已重试 3 次")]


def test_daily_job_stays_quiet_when_webhook_unconfigured(app, backend, ops_alerts, monkeypatch):
    """没配推送通道时不告警——同一通道本来就发不出去，告警只会堆日志"""
    monkeypatch.setattr(Config, "NAPCAT_WEBHOOK", "")
    monkeypatch.setattr(report_mod, "run_daily",
                        lambda *a, **k: {"pushed": False,
                                         "message": "未配置 NAPCAT_WEBHOOK，跳过推送",
                                         "date": "2026-10-21", "path": "x"})
    sched = _start(app, backend)
    _run_daily_job(sched)

    assert ops_alerts == []


def test_daily_job_swallows_exception_and_alerts(app, backend, ops_alerts, monkeypatch):
    """run_daily 抛异常：任务不能死（抛出即失败），告警详情截断在 200 字符内"""
    def boom(*a, **k):
        raise RuntimeError("database is locked " * 30)

    monkeypatch.setattr(report_mod, "run_daily", boom)
    sched = _start(app, backend)
    _run_daily_job(sched)

    assert len(ops_alerts) == 1
    title, detail = ops_alerts[0]
    assert title == "定时日报生成异常"
    assert "RuntimeError" in detail and "database is locked" in detail
    assert len(detail) <= 200


# ---------- 夜间维护 job ----------

def _patch_nightly(monkeypatch, calls, *, backup=None, mirror=None, dir_size=None):
    """默认三步全成功；按需覆盖某一步（calls 记录实际执行了哪些步骤）"""

    def ok_backup(db_path, backup_dir, keep):
        calls["backup"].append((db_path, backup_dir, keep))
        if backup is not None:
            return backup()
        return None

    def ok_mirror(src, dst):
        calls["mirror"].append((src, dst))
        if mirror is not None:
            return mirror()
        return {"failed": 0, "copied": 1}

    def ok_dir_size(root):
        calls["disk"].append(root)
        if dir_size is not None:
            return dir_size()
        return 0

    monkeypatch.setattr(backup_mod, "create_backup", ok_backup)
    monkeypatch.setattr(backup_mod, "mirror_images", ok_mirror)
    monkeypatch.setattr(backup_mod, "dir_size_bytes", ok_dir_size)


def test_nightly_backup_failure_alerts_but_other_steps_still_run(app, ops_alerts, monkeypatch):
    """备份失败：告警带异常上下文，且镜像 / 磁盘检查不能被跳过"""
    calls = {"backup": [], "mirror": [], "disk": []}

    def fail_backup():
        raise OSError("disk full")

    _patch_nightly(monkeypatch, calls, backup=fail_backup)
    monkeypatch.setattr(Config, "IMAGES_MIRROR_DIR", "mirror-out")
    monkeypatch.setattr(Config, "DISK_ALERT_GB", 20.0)

    scheduler_mod.nightly_job()  # 任一步失败都不能让任务整体抛出

    assert ops_alerts == [("数据库备份失败", "OSError: disk full")]
    # 失败不中断：镜像与磁盘检查照样执行
    assert len(calls["mirror"]) == 1
    assert len(calls["disk"]) == 1
    assert calls["backup"][0][2] == Config.BACKUP_KEEP  # 保留份数来自配置


def test_nightly_mirror_partial_failure_alerts(app, ops_alerts, monkeypatch):
    """镜像有文件失败：必须告警，带失败数与目标目录"""
    calls = {"backup": [], "mirror": [], "disk": []}
    _patch_nightly(monkeypatch, calls, mirror=lambda: {"failed": 3, "copied": 5})
    monkeypatch.setattr(Config, "IMAGES_MIRROR_DIR", "mirror-out")
    monkeypatch.setattr(Config, "DISK_ALERT_GB", 20.0)

    scheduler_mod.nightly_job()

    assert ops_alerts == [("图片镜像有文件失败", "失败 3 个｜目标 mirror-out")]


def test_nightly_mirror_exception_alerts_and_disk_check_still_runs(app, ops_alerts, monkeypatch):
    """镜像整体抛异常：告警 + 后续磁盘检查继续"""
    calls = {"backup": [], "mirror": [], "disk": []}

    def boom():
        raise RuntimeError("nas unreachable")

    _patch_nightly(monkeypatch, calls, mirror=boom)
    monkeypatch.setattr(Config, "IMAGES_MIRROR_DIR", "mirror-out")
    monkeypatch.setattr(Config, "DISK_ALERT_GB", 20.0)

    scheduler_mod.nightly_job()

    assert ops_alerts == [("图片镜像失败", "RuntimeError: nas unreachable")]
    assert len(calls["disk"]) == 1  # 镜像炸了，磁盘检查也不能停


@pytest.mark.parametrize("used_gb,threshold,expect_alert", [
    (19.5, 20.0, False),   # 未到阈值不告警
    (20.0, 20.0, True),    # 正好等于阈值要告警（>= 而不是 >）
    (25.0, 20.0, True),    # 超阈值告警
    (25.0, 0.0, False),    # 阈值配 0 = 关闭告警
])
def test_nightly_disk_threshold_boundary(app, ops_alerts, monkeypatch,
                                         used_gb, threshold, expect_alert):
    """磁盘占用按 >= 阈值告警；未配置镜像目录时镜像步骤整体跳过"""
    calls = {"backup": [], "mirror": [], "disk": []}
    _patch_nightly(monkeypatch, calls, dir_size=lambda: int(used_gb * 1024 ** 3))
    monkeypatch.setattr(Config, "IMAGES_MIRROR_DIR", "")
    monkeypatch.setattr(Config, "DISK_ALERT_GB", threshold)

    scheduler_mod.nightly_job()

    assert calls["mirror"] == [], "未配置 IMAGES_MIRROR_DIR 时不应跑镜像"
    if expect_alert:
        assert len(ops_alerts) == 1
        title, detail = ops_alerts[0]
        assert title == "成品图占用已超阈值"
        assert f"当前 {used_gb:.1f} GB" in detail
        assert f"阈值 {threshold:.0f}" in detail
    else:
        assert ops_alerts == []


def test_nightly_disk_check_exception_does_not_break_run(app, ops_alerts, monkeypatch):
    """磁盘检查抛异常：只记日志、不告警、不让任务整体失败"""
    calls = {"backup": [], "mirror": [], "disk": []}

    def boom():
        raise OSError("path vanished")

    _patch_nightly(monkeypatch, calls, dir_size=boom)
    monkeypatch.setattr(Config, "IMAGES_MIRROR_DIR", "")
    monkeypatch.setattr(Config, "DISK_ALERT_GB", 20.0)

    scheduler_mod.nightly_job()  # 抛出即失败

    assert ops_alerts == []
    assert calls["backup"] and calls["disk"]  # 前序步骤确实执行过
