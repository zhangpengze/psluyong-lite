"""任务结算（settler）测试

守的是这条账：**生成出来的图，必须对应一条进到服务器或离线队列的记录**。

旧版把上报写在 GET /export/status 里当副作用，只有前端还在轮询时才发生。
员工生成完就关软件、切窗口、或前端 fetch 断掉，图躺在输出目录、后台一条
记录都没有，且事后查不出是谁做的哪一篇。所以这里的用例全部围绕"没人轮询"
和"进程被杀之后"这两个真实场景。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from client.sidecar import queue, settler
from client.sidecar.api_client import ServerError
from client.sidecar.config import ClientConfig

TASK_ID = "20261024-101010-abcd1234"


class FakeClient:
    def __init__(self, fail_status: int | None = None, payload: dict | None = None):
        self.fail_status = fail_status
        self.payload = payload or {}
        self.submitted: list[dict] = []

    def submit_record(self, fields, image_path):
        if self.fail_status is not None:
            raise ServerError("服务器炸了", self.fail_status, self.payload)
        self.submitted.append({"fields": fields, "image": str(image_path) if image_path else None})
        return {"id": len(self.submitted)}


@pytest.fixture(autouse=True)
def _reset_settler():
    """_STARTED/_INFLIGHT 是模块级状态，用例之间必须归零"""
    settler._STARTED = False
    settler._INFLIGHT.clear()
    yield
    settler._STARTED = False
    settler._INFLIGHT.clear()


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    root = tmp_path / "client"
    root.mkdir()
    monkeypatch.setattr(settler, "app_data_dir", lambda: root)
    monkeypatch.setattr(queue, "QUEUE_DIR", root / "pending")
    monkeypatch.setattr(queue, "DEAD_DIR", root / "dead")
    return root


@pytest.fixture()
def cfg():
    return ClientConfig(server_url="http://127.0.0.1:8080", api_key="k", employee_name="张三")


def make_task(root: Path, task_id: str = TASK_ID, **status_over) -> Path:
    """按 worker 的真实文件协议造一个"跑完了"的任务目录"""
    task_dir = root / "executions" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    params = {
        "author": "李四",
        "title": "深度学习",
        "template_id": "tpl-1.psd",
        "template_name": "中国收藏",
        "partner_name": "甲",
        "employee_name": "张三",
        "work_date": "2026-10-24",
    }
    (task_dir / "params.json").write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")
    status = {
        "state": "done",
        "success": True,
        "image_path": str(root / "out" / "李四.jpg"),
        "message": "",
        "warnings": [],
        "duration_ms": 5200,
    }
    status.update(status_over)
    (task_dir / "status.json").write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    return task_dir


def result_of(task_dir: Path) -> dict | None:
    path = task_dir / "result.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


# ========== 核心：没人轮询也必须报上去 ==========
def test_settles_task_that_nobody_polled(data_dir, cfg):
    """员工点完生成就把软件关了：下次启动对账要把它补报上去

    旧版这条记录不存在——上报只发生在 /export/status 里，没人轮询就没人报。
    """
    task_dir = make_task(data_dir)
    fake = FakeClient()

    counts = settler.settle_pending(lambda c: fake, cfg=cfg)

    assert counts["settled"] == 1 and counts["remaining"] == 0
    assert len(fake.submitted) == 1
    sent = fake.submitted[0]["fields"]
    assert sent["author"] == "李四"
    assert sent["template_name"] == "中国收藏"
    assert sent["work_date"] == "2026-10-24", "日期必须是生成那天，不是补报那天"
    assert sent["status"] == "success"
    assert fake.submitted[0]["image"].endswith("李四.jpg"), "图片要跟着记录一起上传"
    assert result_of(task_dir)["uploaded"] is True


def test_settle_is_idempotent(data_dir, cfg):
    """对账跑两遍不能报两条：启动对账与后台兜底会反复扫同一个目录"""
    make_task(data_dir)
    fake = FakeClient()

    settler.settle_pending(lambda c: fake, cfg=cfg)
    settler.settle_pending(lambda c: fake, cfg=cfg)
    settler.settle_task(TASK_ID, lambda c: fake, cfg=cfg)

    assert len(fake.submitted) == 1


def test_settle_returns_cached_payload(data_dir, cfg):
    """result.json 同时是 /export/status 的响应缓存：内容必须一模一样"""
    make_task(data_dir)
    fake = FakeClient()

    first = settler.settle_task(TASK_ID, lambda c: fake, cfg=cfg)
    second = settler.settle_task(TASK_ID, lambda c: fake, cfg=cfg)

    assert first == second
    assert first["task_id"] == TASK_ID and first["finished"] is True


def test_running_task_is_left_alone(data_dir, cfg):
    """还在跑（没有 status.json）的任务不能被结算，更不能报一条空记录"""
    task_dir = data_dir / "executions" / TASK_ID
    task_dir.mkdir(parents=True)
    (task_dir / "params.json").write_text("{}", encoding="utf-8")
    (task_dir / "heartbeat").touch()
    fake = FakeClient()

    assert settler.settle_pending(lambda c: fake, cfg=cfg)["settled"] == 0
    assert fake.submitted == []
    assert result_of(task_dir) is None


# ========== 不该上报的三种终态 ==========
@pytest.mark.parametrize("state", ["cancelled", "uncertain", "timeout"])
def test_cancelled_and_uncertain_are_marked_but_not_reported(data_dir, cfg, state):
    """取消是"不想等了"、崩溃是"不知道成没成"，都不能算失败记录"""
    task_dir = make_task(data_dir, state=state, success=False, message="说明")
    fake = FakeClient()

    payload = settler.settle_task(TASK_ID, lambda c: fake, cfg=cfg)

    assert fake.submitted == []
    assert result_of(task_dir) is not None, "必须留标记，否则每一轮扫描都会再看一遍"
    assert payload[state] is True


def test_hard_timeout_keeps_the_ps_warning(data_dir, cfg):
    """硬超时的提醒（PS 里可能留着没关的文档）必须传到界面上——

    那条提醒是"暂存盘不足"的唯一线索，丢了就等于没修。
    超时不报 failed：图可能已经导出来了，报失败会让失败率多一条假数据。
    """
    warn = "生成被强制中止：Photoshop 里可能留下这个模板的未保存文档"
    make_task(data_dir, state="timeout", success=False, message="等了 5 分钟还没结束", warnings=[warn])
    fake = FakeClient()

    payload = settler.settle_task(TASK_ID, lambda c: fake, cfg=cfg)

    assert payload["uncertain"] is True
    assert fake.submitted == []
    assert payload["warnings"] == [warn]


def test_worker_exception_is_reported_as_failed(data_dir, cfg):
    """worker 抛错（state=error）以前根本没人报，客户端整批坏掉时后台一片安静"""
    make_task(data_dir, state="error", success=False, message="生成模块异常: COM 调用失败")
    fake = FakeClient()

    payload = settler.settle_task(TASK_ID, lambda c: fake, cfg=cfg)

    assert fake.submitted[0]["fields"]["status"] == "failed"
    assert "COM 调用失败" in fake.submitted[0]["fields"]["error_message"]
    assert payload["ok"] is False


# ========== 断网 / 磁盘故障 ==========
def test_offline_record_lands_in_queue_and_counts_as_settled(data_dir, cfg):
    """连不上服务器：记录进离线队列，任务算结算（不会丢），且不重复提交"""
    task_dir = make_task(data_dir)
    fake = FakeClient(fail_status=0)

    payload = settler.settle_task(TASK_ID, lambda c: fake, cfg=cfg)

    assert payload["queued"] is True and payload["uploaded"] is False
    items = queue.pending()
    assert len(items) == 1
    queued = queue.load(items[0])
    assert queued["fields"]["author"] == "李四"
    assert queued["image_path"].endswith("李四.jpg"), "断网时图片路径必须一起留下"
    assert result_of(task_dir)["queued"] is True

    assert settler.settle_pending(lambda c: fake, cfg=cfg)["settled"] == 0


def test_queue_write_failure_keeps_task_unsettled(data_dir, cfg, monkeypatch):
    """队列也写不进去（磁盘满）时绝不能标已结算，否则这条记录真的没了"""
    task_dir = make_task(data_dir)
    fake = FakeClient(fail_status=0)

    def boom(*_a, **_kw):
        raise OSError("磁盘已满")

    monkeypatch.setattr(queue, "enqueue", boom)

    payload = settler.settle_task(TASK_ID, lambda c: fake, cfg=cfg)

    assert payload["unsettled"] is True
    assert payload["finished"] is True, "前端不能再无限轮询下去"
    assert "图片在" in payload["error"], "必须把图片路径告诉员工"
    assert result_of(task_dir) is None
    assert settler.unsettled_task_ids() == [TASK_ID], "留给下一轮重试"


def test_duplicate_409_counts_as_already_reported(data_dir, cfg):
    """上一次其实报成功了、只是没来得及写 result.json（进程被杀）：
    服务端返回 409 duplicate，必须按已上报收尾，否则永远收不了尾"""
    make_task(data_dir)
    fake = FakeClient(fail_status=409, payload={"duplicate": True})

    payload = settler.settle_task(TASK_ID, lambda c: fake, cfg=cfg)

    assert payload["duplicate"] is True
    assert payload["queued"] is False
    assert queue.pending() == [], "已经报过的记录不能再进补传队列"
    assert result_of(data_dir / "executions" / TASK_ID) is not None


# ========== 待补报清单 ==========
def test_unsynced_only_counts_old_enough_tasks(data_dir, cfg):
    """刚跑完、正在结算的不算"待补报"，否则界面每次生成完闪一下"""
    task_dir = make_task(data_dir)

    assert settler.unsettled_task_ids() == [TASK_ID]
    assert settler.unsettled_task_ids(min_age_s=60) == []

    settler.settle_task(TASK_ID, lambda c: FakeClient(), cfg=cfg)
    assert settler.unsettled_task_ids() == [], "结算完就从清单里消失"


# ========== 后台兜底线程 ==========
def test_background_settler_settles_without_any_poll(data_dir, cfg):
    """前端彻底不问的时候（组件卸载/窗口关了），后台线程自己把账结掉"""
    stop = threading.Event()
    fake = FakeClient()
    assert settler.start_settler(lambda c: fake, interval=0.05, stop=stop) is True

    # 线程起来之后才有任务跑完——模拟"运行中前端不轮询"
    task_dir = make_task(data_dir)

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and result_of(task_dir) is None:
        time.sleep(0.02)
    stop.set()

    assert fake.submitted, "后台结算没跟上，这条记录就丢了"
    assert result_of(task_dir)["uploaded"] is True


def test_start_settler_only_once(data_dir, cfg):
    """重复启动会变成两个线程抢同一批任务，必须只启一次"""
    stop = threading.Event()
    try:
        assert settler.start_settler(lambda c: FakeClient(), interval=5, stop=stop) is True
        assert settler.start_settler(lambda c: FakeClient(), interval=5, stop=stop) is False
    finally:
        stop.set()


def test_startup_reconcile_reports_what_it_found(data_dir, cfg, caplog):
    """启动对账要留下日志：员工报"昨天的记录没了"时能查到补了几条"""
    task_dir = make_task(data_dir)
    stop = threading.Event()
    try:
        with caplog.at_level("INFO", logger=settler.__name__):
            settler.start_settler(lambda c: FakeClient(), interval=5, stop=stop)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and (
                result_of(task_dir) is None or "启动对账" not in caplog.text
            ):
                time.sleep(0.02)
    finally:
        stop.set()
    assert result_of(task_dir) is not None, "启动对账没把上一轮的任务补上"
    assert "启动对账" in caplog.text


# ========== 装配：main() 必须真的把结算拉起来 ==========
def test_sidecar_main_starts_task_settler(monkeypatch):
    """这一行被重构掉，前面所有兜底（启动对账 + 后台扫描）就都不存在了"""
    from types import SimpleNamespace

    from client.sidecar import server as sc_server

    started: list = []
    monkeypatch.setattr(settler, "start_settler", lambda f, **kw: started.append(f) is None or True)
    monkeypatch.setattr(sc_server, "_attach_file_logger", lambda _name: None)
    monkeypatch.setattr(sc_server, "create_sidecar", lambda: SimpleNamespace(run=lambda **_kw: None))

    sc_server.main()

    assert started, "sidecar 启动时没有拉起任务结算：关窗丢记录的问题会原样回来"


# ========== 装配：main() 必须真的把结算拉起来 ==========
def test_sidecar_main_starts_task_settler(monkeypatch):
    """这一行被重构掉，前面所有兜底（启动对账 + 后台扫描）就都不存在了"""
    from types import SimpleNamespace

    from client.sidecar import server as sc_server

    started: list = []
    monkeypatch.setattr(settler, "start_settler", lambda f, **kw: started.append(f) is None or True)
    monkeypatch.setattr(sc_server, "_attach_file_logger", lambda _name: None)
    monkeypatch.setattr(sc_server, "create_sidecar", lambda: SimpleNamespace(run=lambda **_kw: None))

    sc_server.main()

    assert started, "sidecar 启动时没有拉起任务结算：关窗丢记录的问题会原样回来"


def test_429_on_settle_keeps_the_record_in_the_offline_queue(data_dir, cfg):
    """服务端限流（429）不是"这条数据有问题"，必须入队而不是丢掉

    服务端刚给 `POST /api/v1/records` 加了上传闸门，429 从此是正常路径上会出现
    的一种回答。侧边这里如果按"业务拒绝"处理，员工那张图对应的记录就永久没了——
    而图还在输出目录里躺着，谁也不会发现少了一条。
    """
    make_task(data_dir)
    fake = FakeClient(fail_status=429)

    payload = settler.settle_task(TASK_ID, lambda c: fake, cfg=cfg)

    assert payload["uploaded"] is False
    assert payload["queued"] is True
    assert len(queue.pending()) == 1, "429 之后记录必须留在本地队列里"
    assert "429" in payload["upload_message"]
    item = queue.load(queue.pending()[0])
    assert item["blocked_reason"], "受阻原因要落盘，界面才能告诉员工在等什么"
