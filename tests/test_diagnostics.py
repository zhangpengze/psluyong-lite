"""一键诊断包：内容、脱敏、失败降级。

这个包的用途很具体：员工说"我这边出错了"，主管把 zip 一打开就能判断是
服务器挂了、Key 错了、还是模板缺占位符——不用再问十句话。
所以这里重点验三件事：该有的都在、**Key 一定不在**、单份材料坏掉不拖垮整包。
"""

from __future__ import annotations

import json
import zipfile

import pytest

from client.sidecar import diagnostics
from client.sidecar.config import ClientConfig, save_config


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """把 sidecar 的数据目录整个指到临时目录，并塞进一批真实形状的材料"""
    root = tmp_path / "appdata"
    (root / "logs").mkdir(parents=True)
    (root / "executions").mkdir()

    (root / "logs" / "sidecar.log").write_text(
        "\n".join(f"第 {i} 行日志" for i in range(600)), encoding="utf-8"
    )
    (root / "logs" / "worker.log").write_text("worker 一行", encoding="utf-8")
    (root / "logs" / "crash-20261024-120000.txt").write_text("崩溃现场", encoding="utf-8")

    for i in range(8):  # 8 个任务，包里只该带最近 5 个
        task = root / "executions" / f"20261024120{i}00-ab{i}"
        task.mkdir()
        (task / "params.json").write_text(json.dumps({"author": "李四"}), encoding="utf-8")
        (task / "status.json").write_text(json.dumps({"state": "done"}), encoding="utf-8")
    (root / "executions" / "20261024120700-ab7" / "result.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(diagnostics, "app_data_dir", lambda: root)
    monkeypatch.setattr(
        diagnostics, "_resolve_ps", lambda _cfg: type("PS", (), {"to_dict": lambda self: {"found": True}})()
    )
    save_config(ClientConfig(server_url="http://10.0.0.5:8080", api_key="super-secret-key", employee_name="张三"))
    return root


def _names(bundle) -> list[str]:
    with zipfile.ZipFile(bundle) as zf:
        return zf.namelist()


def _read(bundle, name) -> str:
    with zipfile.ZipFile(bundle) as zf:
        return zf.read(name).decode("utf-8")


# ---------- 内容 ----------

def test_bundle_contains_every_material(data_dir, tmp_path):
    bundle = diagnostics.build_bundle(None, out_dir=tmp_path)
    names = _names(bundle)

    assert "README.txt" in names
    for required in ("meta.json", "config.sanitized.json", "server_health.json"):
        assert required in names, f"缺 {required}，运维就得回头再要一次"
    assert "logs/sidecar.log" in names and "logs/worker.log" in names
    assert "logs/crash-20261024-120000.txt" in names


def test_api_key_is_masked(data_dir, tmp_path):
    """这是整个包里唯一必须抹掉的东西：Key 泄漏等于别人能冒充员工上报记录。"""
    bundle = diagnostics.build_bundle(None, out_dir=tmp_path)
    raw = _read(bundle, "config.sanitized.json")

    assert "super-secret-key" not in raw, "Key 明文进了诊断包"
    assert "已隐藏" in raw
    assert "http://10.0.0.5:8080" in raw, "服务器地址要留着，否则没法判断地址对不对"


def test_meta_carries_versions_and_queue_counts(data_dir, tmp_path):
    meta = json.loads(_read(diagnostics.build_bundle(None, out_dir=tmp_path), "meta.json"))
    assert meta["sidecar_version"]
    assert meta["employee"] == "张三"
    assert set(meta["queue"]) >= {"pending", "dead", "blocked"}
    assert "unsynced" in meta


def test_logs_are_truncated_to_tail(data_dir, tmp_path):
    """日志可以几十万行，全量打包会让 zip 变成几百 MB，员工发不出去"""
    text = _read(diagnostics.build_bundle(None, out_dir=tmp_path), "logs/sidecar.log")
    assert "第 599 行日志" in text
    assert "第 0 行日志" not in text
    assert "仅保留最后" in text, "截断要写明，否则运维以为日志只有这么点"


def test_only_recent_tasks_are_included(data_dir, tmp_path):
    names = [n for n in _names(diagnostics.build_bundle(None, out_dir=tmp_path)) if n.startswith("executions/")]
    tasks = {n.split("/")[1] for n in names}
    assert len(tasks) == diagnostics.RECENT_TASKS
    assert "20261024120700-ab7" in tasks, "最新的必须在里面"


def test_result_json_included_when_present(data_dir, tmp_path):
    names = _names(diagnostics.build_bundle(None, out_dir=tmp_path))
    assert "executions/20261024120700-ab7/result.json" in names


def test_latest_task_included(data_dir, tmp_path):
    """员工刚点完就报障的场景：最新那个任务必须在包里"""
    names = _names(diagnostics.build_bundle(None, out_dir=tmp_path))
    assert any("20261024120700-ab7" in n for n in names)


# ---------- 服务器探针 ----------

def test_server_health_skipped_without_client(data_dir, tmp_path):
    health = json.loads(_read(diagnostics.build_bundle(None, out_dir=tmp_path), "server_health.json"))
    assert "skipped" in health


def test_server_health_records_error_instead_of_raising(data_dir, tmp_path):
    class BadClient:
        base_url = "http://127.0.0.1:1"

    health = json.loads(_read(diagnostics.build_bundle(BadClient(), out_dir=tmp_path), "server_health.json"))
    assert "error" in health, "探不到本身就是有用信息，不能因为探不到就不出包"


# ---------- 降级 ----------

def test_missing_dirs_still_produce_a_bundle(tmp_path, monkeypatch):
    """最需要诊断包的恰恰是"目录都可能是坏的"那种现场"""
    monkeypatch.setattr(diagnostics, "app_data_dir", lambda: tmp_path / "nope")
    monkeypatch.setattr(
        diagnostics, "_resolve_ps", lambda _cfg: type("PS", (), {"to_dict": lambda self: {}})()
    )
    bundle = diagnostics.build_bundle(None, out_dir=tmp_path)
    assert "meta.json" in _names(bundle)


def test_queue_read_failure_does_not_break_meta(data_dir, tmp_path, monkeypatch):
    from client.sidecar import queue

    monkeypatch.setattr(queue, "pending", lambda: (_ for _ in ()).throw(OSError("io")))
    meta = json.loads(_read(diagnostics.build_bundle(None, out_dir=tmp_path), "meta.json"))
    assert "error" in meta["queue"]


def test_unreadable_log_becomes_a_note_not_an_exception(data_dir, tmp_path):
    (data_dir / "logs" / "sidecar.log").unlink()
    with zipfile.ZipFile(diagnostics.build_bundle(None, out_dir=tmp_path)) as zf:
        assert "logs/sidecar.log" not in zf.namelist()
