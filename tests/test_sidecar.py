"""员工客户端 sidecar 测试

worker 子进程与服务器提交都打桩，验证 sidecar 自己的编排逻辑：
参数校验、启动/轮询协议、忙时拒绝、离线入队、补传、路径穿越防护。
worker 文件协议本身的真实性由 test_executor.py 用真子进程覆盖。
"""

from __future__ import annotations

import io
import subprocess
import sys
import time
from pathlib import Path

import pytest

from client.sidecar import server as sidecar_server
from client.sidecar.api_client import ServerError
from client.sidecar.config import ClientConfig, load_config, save_config
from client.sidecar.executor import Executor


# 一个假 worker：按 FAKE_WORKER_RESULT 环境变量决定写什么终态。
# 走真实文件协议（params.json / status.json），只把 COM 调用换掉。
FAKE_WORKER = r"""
import json, os, sys
from pathlib import Path

task_dir = Path(sys.argv[-1])
params = json.loads((task_dir / "params.json").read_text(encoding="utf-8"))
result = os.environ.get("FAKE_WORKER_RESULT", "success")
out = Path(params["output_dir"])
out.mkdir(parents=True, exist_ok=True)

if result == "crash":
    sys.exit(2)

if result == "success":
    img = out / "fake.jpg"
    img.write_bytes(b"fakejpg")
    status = {
        "state": "done", "success": True, "image_path": str(img),
        "message": "", "warnings": ["图层「标题」字体不可用"], "duration_ms": 5200,
    }
else:
    status = {
        "state": "done", "success": False, "image_path": "",
        "message": "Photoshop 未运行，请先启动 Photoshop 再点执行",
        "warnings": [], "duration_ms": 100,
    }
(task_dir / "status.json").write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
"""


@pytest.fixture()
def sc():
    app = sidecar_server.create_sidecar()
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture()
def configured(tmp_path):
    """配好客户端 + 本机已有一个可用模板（模板归员工本机管）"""
    template_dir = tmp_path / "templates"
    template_dir.mkdir(parents=True, exist_ok=True)
    (template_dir / "tpl-1.psd").write_bytes(b"8BPS" + b"\0" * 64)

    cfg = ClientConfig(
        server_url="http://127.0.0.1:8080",
        api_key="k",
        employee_name="张三",
        output_dir=str(tmp_path),
        template_dir=str(template_dir),
        partners=["甲", "乙"],
    )
    save_config(cfg)
    return cfg


def _stub_worker(monkeypatch) -> None:
    def spawn(self, task_dir):
        return subprocess.Popen(
            [sys.executable, "-c", FAKE_WORKER, str(task_dir)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    monkeypatch.setattr(Executor, "_spawn_worker", spawn)


def _poll_final(sc, task_id: str, timeout_s: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        data = sc.get(f"/export/status?task_id={task_id}").get_json()
        if data.get("finished"):
            return data
        assert data["state"] == "running"
        time.sleep(0.2)
    raise AssertionError(f"{timeout_s}s 内未出终态")


class FakeClient:
    """替身：记录提交内容，可配置是否失败"""

    def __init__(self, fail_status: int | None = None):
        self.fail_status = fail_status
        self.submitted: list[dict] = []

    def health(self):
        return self.fail_status is None

    def submit_record(self, fields, image_path):
        if self.fail_status is not None:
            raise ServerError("服务器炸了", self.fail_status)
        self.submitted.append({"fields": fields, "image": str(image_path) if image_path else None})
        return {"id": len(self.submitted)}


def test_status_endpoint(sc, configured, monkeypatch):
    monkeypatch.setattr(sidecar_server, "_client", lambda cfg: FakeClient())
    data = sc.get("/status").get_json()
    assert data["ok"] is True
    assert data["config"]["employee_name"] == "张三"
    assert data["pending"] == 0


def test_config_roundtrip(sc):
    resp = sc.post("/config", json={"employee_name": "李四", "jpg_quality": 8})
    assert resp.status_code == 200
    assert load_config().employee_name == "李四"
    assert load_config().jpg_quality == 8


def test_export_requires_fields(sc, configured):
    assert sc.post("/export", json={"author": "a", "title": "b"}).status_code == 400  # 缺模板
    assert sc.post("/export", json={"template_id": "t", "title": "b"}).status_code == 400  # 缺作者


def test_export_requires_employee_name(sc, tmp_path):
    save_config(ClientConfig(employee_name="", output_dir=str(tmp_path)))
    resp = sc.post("/export", json={"template_id": "t", "author": "a", "title": "b"})
    assert resp.status_code == 400
    assert "姓名" in resp.get_json()["error"]


def test_export_success_uploads(sc, configured, monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(sidecar_server, "_client", lambda cfg: fake)
    _stub_worker(monkeypatch)

    start = sc.post(
        "/export",
        json={"template_id": "tpl-1.psd", "author": "李四", "title": "深度学习", "partner_name": "甲"},
    ).get_json()
    assert start["ok"] is True and start["state"] == "started"

    data = _poll_final(sc, start["task_id"])
    assert data["ok"] is True
    assert data["uploaded"] is True
    assert data["queued"] is False
    assert data["duration_ms"] == 5200
    assert data["warnings"] == ["图层「标题」字体不可用"]

    sent = fake.submitted[0]["fields"]
    assert sent["author"] == "李四"
    assert sent["status"] == "success"
    assert sent["template_name"] == "tpl-1"  # 模板名取文件名（去扩展名）
    assert sent["template_id"] == "tpl-1.psd"

    # 终态幂等：重复轮询拿到同一份 result.json，不会重复上报
    again = sc.get(f"/export/status?task_id={start['task_id']}").get_json()
    assert again == data
    assert len(fake.submitted) == 1


def test_export_ps_failure_still_reported(sc, configured, monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(sidecar_server, "_client", lambda cfg: fake)
    _stub_worker(monkeypatch)
    monkeypatch.setenv("FAKE_WORKER_RESULT", "fail")

    start = sc.post("/export", json={"template_id": "tpl-1.psd", "author": "a", "title": "b"}).get_json()
    data = _poll_final(sc, start["task_id"])
    assert data["ok"] is False
    assert "Photoshop 未运行" in data["error"]
    # 失败尝试也要入库，便于事后排查
    assert fake.submitted[0]["fields"]["status"] == "failed"


def test_export_offline_goes_to_queue(sc, configured, monkeypatch):
    fake = FakeClient(fail_status=0)  # 0 = 连不上
    monkeypatch.setattr(sidecar_server, "_client", lambda cfg: fake)
    _stub_worker(monkeypatch)

    start = sc.post("/export", json={"template_id": "tpl-1.psd", "author": "a", "title": "b"}).get_json()
    data = _poll_final(sc, start["task_id"])
    assert data["ok"] is True
    assert data["queued"] is True
    assert data["uploaded"] is False
    assert sc.get("/queue").get_json()["pending"] == 1


def test_export_rejects_second_click_while_running(sc, configured, monkeypatch):
    """COM 只能单任务：第一个任务没结束，第二次点击必须立刻得到 409 而不是排队"""
    fake = FakeClient()
    monkeypatch.setattr(sidecar_server, "_client", lambda cfg: fake)

    def slow_spawn(self, task_dir):
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    monkeypatch.setattr(Executor, "_spawn_worker", slow_spawn)

    first = sc.post("/export", json={"template_id": "tpl-1.psd", "author": "a", "title": "b"}).get_json()
    assert first["ok"] is True

    second = sc.post("/export", json={"template_id": "tpl-1.psd", "author": "a", "title": "b"})
    assert second.status_code == 409
    assert second.get_json()["busy"] is True


def test_export_uncertain_when_worker_crashes(sc, configured, monkeypatch):
    """worker 硬崩（退出无终态）：报"结果未知"，绝不假成功"""
    fake = FakeClient()
    monkeypatch.setattr(sidecar_server, "_client", lambda cfg: fake)
    _stub_worker(monkeypatch)
    monkeypatch.setenv("FAKE_WORKER_RESULT", "crash")

    start = sc.post("/export", json={"template_id": "tpl-1.psd", "author": "a", "title": "b"}).get_json()
    data = _poll_final(sc, start["task_id"])
    assert data["ok"] is False
    assert data["uncertain"] is True
    assert "结果未知" in data["error"]
    # 崩溃的任务不知道成没成，不能上报假记录
    assert fake.submitted == []


def test_preview_blocks_path_traversal(sc, configured, tmp_path):
    outside = tmp_path.parent / "secret.jpg"
    outside.write_bytes(b"secret")

    resp = sc.get("/preview", query_string={"path": str(outside)})
    assert resp.status_code == 403

    inside = tmp_path / "ok.jpg"
    inside.write_bytes(b"ok")
    assert sc.get("/preview", query_string={"path": str(inside)}).status_code == 200


# ========== 本机模板（员工上传，不经服务器）==========
PSD_HEAD = b"8BPS" + b"\0" * 128


def _upload(sc, name: str, payload: bytes = PSD_HEAD):
    return sc.post(
        "/templates/upload",
        data={"files": [(io.BytesIO(payload), name)]},
        content_type="multipart/form-data",
    ).get_json()


def test_templates_list_from_local_dir(sc, configured):
    data = sc.get("/templates").get_json()

    assert data["ok"] is True
    assert [i["name"] for i in data["items"]] == ["tpl-1"]
    assert data["items"][0]["filename"] == "tpl-1.psd"


def test_templates_warns_when_dir_empty(sc, configured):
    (Path(configured.template_dir) / "tpl-1.psd").unlink()

    data = sc.get("/templates").get_json()

    assert data["items"] == []
    assert "还没有" in data["warning"]


def test_upload_keeps_chinese_name(sc, configured):
    result = _upload(sc, "中国收藏.psd")

    assert result["ok"] is True
    assert result["saved"] == ["中国收藏.psd"]
    # 中文不能被清成 "psd"：模板名就是下拉框里的标签
    assert "中国收藏" in [i["name"] for i in result["items"]]


def test_upload_rejects_fake_psd(sc, configured):
    result = _upload(sc, "假装是模板.psd", b"not really a psd")

    assert result["ok"] is False
    assert any("不是 PSD" in e for e in result["errors"])
    assert not (Path(configured.template_dir) / "假装是模板.psd").exists()


def test_upload_rejects_wrong_extension(sc, configured):
    result = _upload(sc, "图片.jpg", PSD_HEAD)

    assert result["ok"] is False
    assert any("只支持" in e for e in result["errors"])


def test_upload_same_name_does_not_overwrite(sc, configured):
    """重复上传不该悄悄顶掉已有模板，自动加序号"""
    first = _upload(sc, "中国收藏.psd")
    second = _upload(sc, "中国收藏.psd")

    assert first["saved"] == ["中国收藏.psd"]
    assert second["saved"] == ["中国收藏(2).psd"]
    names = [i["filename"] for i in second["items"]]
    assert "中国收藏.psd" in names and "中国收藏(2).psd" in names


def test_delete_template_removes_file(sc, configured):
    _upload(sc, "要删的.psd")

    result = sc.post("/templates/delete", json={"name": "要删的.psd"}).get_json()

    assert result["ok"] is True
    assert "要删的" not in [i["name"] for i in result["items"]]
    assert not (Path(configured.template_dir) / "要删的.psd").exists()


def test_delete_blocks_path_traversal(sc, configured):
    resp = sc.post("/templates/delete", json={"name": "../config.json"})

    assert resp.status_code == 400
    assert (Path(configured.template_dir).parent / "config.json").parent.exists()


def test_export_fails_when_template_missing(sc, configured, monkeypatch):
    """模板被删掉后再点执行，要给明确提示而不是进 PS 里报错"""
    monkeypatch.setattr(sidecar_server, "_client", lambda cfg: FakeClient())

    resp = sc.post("/export", json={"template_id": "不存在.psd", "author": "a", "title": "b"})

    assert resp.status_code == 200
    assert resp.get_json()["ok"] is False
    assert "模板" in resp.get_json()["error"]


# ========== 合作伙伴（本机配置）==========
def test_partners_come_from_local_config(sc, configured):
    """名单存在本机，断网也能选——不能再依赖服务器"""
    data = sc.get("/dicts").get_json()

    assert data["grouped"]["partner"] == ["甲", "乙"]


def test_partners_saved_trimmed_and_deduped(sc, configured):
    sc.post("/config", json={"partners": [" 华东出版社 ", "南方文化", "", "华东出版社", "  "]}).get_json()

    partners = sc.get("/dicts").get_json()["grouped"]["partner"]

    assert partners == ["华东出版社", "南方文化"], "空行与重复项要在保存时收口"


def test_normalize_path_collapses_duplicate_separators():
    """设置页粘贴常带双斜杠；折叠时不能动 UNC 路径"""
    from client.sidecar.config import _normalize_path

    assert _normalize_path("C://Program Files//Adobe//PS.exe") == r"C:/Program Files/Adobe/PS.exe"
    assert _normalize_path("E:/psluyong-lite//out") == "E:/psluyong-lite/out"
    assert _normalize_path(r"\\nas\share\\dir") == r"\\nas\share\\dir"  # UNC 原样保留
    assert _normalize_path("") == ""
