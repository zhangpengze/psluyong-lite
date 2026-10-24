"""员工客户端 sidecar 测试

worker 子进程与服务器提交都打桩，验证 sidecar 自己的编排逻辑：
参数校验、启动/轮询协议、忙时拒绝、离线入队、补传、路径穿越防护。
worker 文件协议本身的真实性由 test_executor.py 用真子进程覆盖。
"""

from __future__ import annotations

import io
import json
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
        self.list_calls: list[tuple] = []

    def health(self):
        return self.fail_status is None

    def submit_record(self, fields, image_path):
        if self.fail_status is not None:
            raise ServerError("服务器炸了", self.fail_status)
        self.submitted.append({"fields": fields, "image": str(image_path) if image_path else None})
        return {"id": len(self.submitted)}

    def list_records(self, employee, size=60, start="", end=""):
        self.list_calls.append((employee, size, start, end))
        return {"items": [{"id": 1, "author": "李四", "title": "深度学习"}], "total": 42}


def test_status_endpoint(sc, configured, monkeypatch):
    monkeypatch.setattr(sidecar_server, "_client", lambda cfg: FakeClient())
    data = sc.get("/status").get_json()
    assert data["ok"] is True
    assert data["config"]["employee_name"] == "张三"
    assert data["pending"] == 0


def test_status_exposes_blocked_queue_reason(sc, configured, monkeypatch, tmp_path):
    """配置受阻（401/403/404）的积压必须在 /status 里露出来

    旧版补传把这些记录直接删掉：界面"待补传 3 条"变 0，看着像补传成功，
    员工永远不会去检查 API Key。现在记录保留 + 给出原因，底栏才能提示。
    """
    from client.sidecar import queue

    monkeypatch.setattr(queue, "QUEUE_DIR", tmp_path / "pending")
    monkeypatch.setattr(queue, "DEAD_DIR", tmp_path / "dead")
    monkeypatch.setattr(sidecar_server, "_client", lambda cfg: FakeClient())
    queue.enqueue({"author": "张三"}, None, blocked_reason="HTTP 401: API Key 无效")

    data = sc.get("/status").get_json()

    assert data["pending"] == 1
    assert data["pending_blocked"] == 1
    assert "401" in data["blocked_reason"]


def test_config_roundtrip(sc):
    resp = sc.post("/config", json={"employee_name": "李四", "jpg_quality": 8})
    assert resp.status_code == 200
    assert load_config().employee_name == "李四"
    assert load_config().jpg_quality == 8


def test_status_survives_corrupt_config(sc, monkeypatch, tmp_path):
    """配置文件被手工改坏（或写一半断电）时，接口不能整体 500。

    旧行为：`{"output_dir": 123}` 一路 AttributeError，/status /config /export
    全部 500，界面只剩"本地服务连接失败"，重启无效、界面上修不回来。
    """
    from client.sidecar import config as cfgmod

    path = tmp_path / "broken" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"output_dir": 123,', encoding="utf-8")  # 半截 JSON
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", path)
    monkeypatch.setattr(sidecar_server, "_client", lambda cfg: FakeClient())

    resp = sc.get("/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["config"]["output_dir"] == ClientConfig().output_dir
    assert "损坏" in data["config_recovered"]

    # 坏文件还在（留档），并且已经写回一份能用的
    assert list(path.parent.glob("config.broken-*.json"))
    assert json.loads(path.read_text(encoding="utf-8"))["output_dir"]


def test_config_post_repairs_wrong_type_field(sc, monkeypatch, tmp_path):
    """手改出 `{"employee_name": 5}` 之后，设置页保存必须能把配置救回来"""
    from client.sidecar import config as cfgmod

    path = tmp_path / "typed" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"employee_name": 5, "api_key": "k1"}), encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", path)

    resp = sc.post("/config", json={"employee_name": "王五"})
    assert resp.status_code == 200
    saved = load_config()
    assert saved.employee_name == "王五"
    assert saved.api_key == "k1", "改一个字段不能把没坏的字段冲掉"


def test_config_post_refuses_when_file_temporarily_unreadable(sc, monkeypatch, tmp_path):
    """文件被别的程序占着时，宁可让员工点重试，也不能拿默认值回写"""
    from client.sidecar import config as cfgmod

    path = tmp_path / "busy" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"api_key": "k1"}), encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", path)
    monkeypatch.setattr(cfgmod, "_read_text_with_retry", lambda _p: (None, "共享冲突", True))

    resp = sc.post("/config", json={"employee_name": "王五"})
    assert resp.status_code == 503
    assert "稍后重试" in resp.get_json()["error"]
    assert json.loads(path.read_text(encoding="utf-8"))["api_key"] == "k1"


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


def test_preview_endpoint_removed(sc, configured, tmp_path):
    """成品预览灯箱已下线，配套的本地图片读取接口不再暴露（避免任意文件读取面）"""
    inside = tmp_path / "ok.jpg"
    inside.write_bytes(b"ok")
    assert sc.get("/preview", query_string={"path": str(inside)}).status_code in (404, 405)


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


# ========== 记录日期与历史区间 ==========
def test_export_carries_generation_date(sc, configured, monkeypatch):
    """生成当天的日期随记录上报：离线隔天补传不能算到补传那天"""
    from datetime import date

    fake = FakeClient()
    monkeypatch.setattr(sidecar_server, "_client", lambda cfg: fake)
    _stub_worker(monkeypatch)

    start = sc.post(
        "/export", json={"template_id": "tpl-1.psd", "author": "李四", "title": "深度学习"}
    ).get_json()
    _poll_final(sc, start["task_id"])

    assert fake.submitted[0]["fields"]["work_date"] == date.today().isoformat()


def test_history_range_drives_window_and_total(sc, configured, monkeypatch):
    """服务端默认只给近 7 天：客户端必须显式传区间，并把 total 透传上去"""
    from datetime import date, timedelta

    fake = FakeClient()
    monkeypatch.setattr(sidecar_server, "_client", lambda cfg: fake)
    today = date.today()

    data = sc.get("/history?range=30").get_json()
    assert data["total"] == 42, "total 要透传，否则界面只能显示本页条数"
    employee, size, start, end = fake.list_calls[-1]
    assert employee == "张三"
    assert start == (today - timedelta(days=29)).isoformat()
    assert end == today.isoformat()

    sc.get("/history?range=all")
    assert fake.list_calls[-1][2] == "2000-01-01", "全部区间要绕开服务端默认的 7 天窗口"

    sc.get("/history?range=abc")
    assert fake.list_calls[-1][2] == (today - timedelta(days=6)).isoformat(), "非法值回退近 7 天"

    sc.get("/history?range=today")
    assert fake.list_calls[-1][2] == today.isoformat(), "今天 = 起止都是今天"
    assert fake.list_calls[-1][3] == today.isoformat()


def test_history_custom_range_and_echo(sc, configured, monkeypatch):
    """员工手选日期：起止照传，并且要回显"真正生效"的区间。

    回显不是装饰：只填起始日时结束日由服务端补成今天，界面上必须让员工看见
    到底查到哪天，否则"我明明选了 9 月 1 日，为什么出来 60 条"没法解释。
    """
    from datetime import date, timedelta

    fake = FakeClient()
    monkeypatch.setattr(sidecar_server, "_client", lambda cfg: fake)
    today = date.today()

    data = sc.get("/history?range=custom&start=2026-09-01&end=2026-09-30").get_json()
    assert fake.list_calls[-1][2] == "2026-09-01"
    assert fake.list_calls[-1][3] == "2026-09-30"
    assert (data["start"], data["end"]) == ("2026-09-01", "2026-09-30")
    assert data["size"] == 60, "30 天跨度边界上按小范围处理，没必要拉 200 条"

    # 只填起始日 → 补到今天
    data = sc.get("/history?range=custom&start=2026-09-01").get_json()
    assert (data["start"], data["end"]) == ("2026-09-01", today.isoformat())

    # 只填结束日 → 就只看那一天（员工的直觉是"到 9 月 30 日为止的那批"，
    # 但起止都不给会变成全量查询，所以单边缺省一律收窄）
    data = sc.get("/history?range=custom&end=2026-09-30").get_json()
    assert (data["start"], data["end"]) == ("2026-09-30", "2026-09-30")

    # 起止写反 → 自动纠正，不返回空列表
    data = sc.get("/history?range=custom&start=2026-09-30&end=2026-09-01").get_json()
    assert (data["start"], data["end"]) == ("2026-09-01", "2026-09-30")

    # 两头都不给 / 脏值 → 当没筛，退回近 7 天（不能把列表清空）
    for q in ("?range=custom", "?range=custom&start=xx&end=yy"):
        data = sc.get(f"/history{q}").get_json()
        assert data["range"] == "7"
        assert data["start"] == (today - timedelta(days=6)).isoformat()

    # 跨度 ≥ 30 天才拉满 200 条
    data = sc.get("/history?range=custom&start=2026-01-01&end=2026-09-30").get_json()
    assert data["size"] == 200


# ========== 跨站请求与 Origin 校验 ==========
def test_cross_site_write_rejected(sc, configured):
    """带外站 Origin 的写请求必须 403：CORS 挡不住请求被执行"""
    evil = sc.post("/config", json={"employee_name": "黑客"}, headers={"Origin": "http://evil.example"})
    assert evil.status_code == 403
    assert load_config().employee_name == "张三", "被拒的请求不能改到配置"

    ok = sc.post("/config", json={"employee_name": "李四"}, headers={"Origin": "tauri://localhost"})
    assert ok.status_code == 200
    assert load_config().employee_name == "李四"

    # 本机程序（curl / worker）不带 Origin，要放行
    assert sc.post("/config", json={"employee_name": "王五"}).status_code == 200


def test_cross_site_read_is_not_rejected(sc, configured, monkeypatch):
    """只拦写：GET 不拦（读的是本机自己的数据，且白名单响应头已挡住外站读取）"""
    monkeypatch.setattr(sidecar_server, "_client", lambda cfg: FakeClient())
    assert sc.get("/status", headers={"Origin": "http://evil.example"}).status_code == 200


# ========== 版本与诊断 ==========
def test_status_exposes_version_and_log_dir(sc, configured, monkeypatch):
    """员工报障时靠这两个字段确认装的哪一版、日志在哪"""
    monkeypatch.setattr(sidecar_server, "_client", lambda cfg: FakeClient())
    data = sc.get("/status").get_json()

    assert data["version"] == sidecar_server.__version__
    assert data["log_dir"].endswith("logs")


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="打开日志目录走 os.startfile，POSIX 上没有该函数（客户端只跑 Windows）",
)
def test_open_log_dir_uses_fixed_path(sc, configured, monkeypatch):
    """日志目录由服务端固定，不接受参数——不能变成任意目录打开器"""
    import os

    from client.sidecar.config import app_data_dir

    opened: list[str] = []
    monkeypatch.setattr(os, "startfile", lambda p: opened.append(str(p)), raising=False)

    assert sc.post("/logs/open").get_json()["ok"] is True
    assert opened == [str(app_data_dir() / "logs")]


# ========== 成品图命名规则 ==========
def _export_params(sc, configured, monkeypatch) -> dict:
    """跑一次假生成，取 worker 实际读到的 params.json"""
    from client.sidecar.config import app_data_dir

    fake = FakeClient()
    monkeypatch.setattr(sidecar_server, "_client", lambda cfg: fake)
    _stub_worker(monkeypatch)

    start = sc.post(
        "/export", json={"template_id": "tpl-1.psd", "author": "李四", "title": "深度学习"}
    ).get_json()
    task_dir = app_data_dir() / "executions" / start["task_id"]
    params = json.loads((task_dir / "params.json").read_text(encoding="utf-8"))
    _poll_final(sc, start["task_id"])  # 收尾，别留后台 worker
    return params


def test_export_passes_filename_rule(sc, configured, monkeypatch):
    """设置里选的命名规则要一路传到 worker"""
    sc.post("/config", json={"filename_rule": "author"})

    assert _export_params(sc, configured, monkeypatch)["filename_rule"] == "author"


def test_export_bad_filename_rule_falls_back(sc, configured, monkeypatch):
    """config.json 被手工改坏时，sidecar 收口不让非法值进 worker"""
    cfg = load_config()
    cfg.filename_rule = "../../evil"
    save_config(cfg)  # 绕过 /config 直接落盘，模拟员工手改了配置文件

    assert _export_params(sc, configured, monkeypatch)["filename_rule"] == "author_title"


def test_config_rejects_out_of_range_values(sc, configured):
    """设置页滑块有范围，HTTP 接口也必须挡。

    原来 /config 只判 hasattr，直接 POST jpg_quality=999 就能把 PS 导出参数
    搞坏，而且这个值会写进 config.json，下次启动继续生效。
    """
    assert sc.post("/config", json={"filename_rule": "../../evil"}).status_code == 400
    assert sc.post("/config", json={"jpg_quality": 999}).status_code == 400
    assert sc.post("/config", json={"jpg_quality": 0}).status_code == 400
    assert sc.post("/config", json={"theme": "neon"}).status_code == 400
    assert sc.post("/config", json={"auto_clear": "yes"}).status_code == 400
    assert sc.post("/config", json={"partners": "不是数组"}).status_code == 400
    assert sc.post("/config", json={"nonsense": 1}).status_code == 400

    # 整包拒绝：不能出现"一半新值一半旧值"
    cfg = load_config()
    assert cfg.jpg_quality == 10
    assert cfg.theme == "dark"
    assert cfg.filename_rule == "author_title"


def test_config_accepts_numeric_strings_for_coords(sc, configured):
    """窗口坐标允许字符串数字：Rust 侧算出来的值偶尔以字符串传回来"""
    assert sc.post("/config", json={"win_x": "120", "win_y": 340}).status_code == 200
    cfg = load_config()
    assert cfg.win_x == 120
    assert cfg.win_y == 340


HANGING_WORKER = "import time; time.sleep(60)"


def test_cancel_returns_cancelled_and_skips_failure_record(sc, configured, monkeypatch):
    """取消 = 员工不想等了，不是失败：不能上报一条 failed 记录把成功率拉低"""
    fake = FakeClient()
    monkeypatch.setattr(sidecar_server, "_client", lambda cfg: fake)

    def spawn(self, task_dir):
        # 永远不写终态的"卡死的 worker"，模拟 PS 挂在对话框上
        return subprocess.Popen(
            [sys.executable, "-c", HANGING_WORKER],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    monkeypatch.setattr(Executor, "_spawn_worker", spawn)

    start = sc.post(
        "/export", json={"template_id": "tpl-1.psd", "author": "张三", "title": "测试"}
    ).get_json()
    assert start["ok"] is True, start
    task_id = start["task_id"]

    resp = sc.post("/export/cancel", json={"task_id": task_id})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True, resp.get_json()

    final = sc.get(f"/export/status?task_id={task_id}").get_json()
    assert final["finished"] is True
    assert final["cancelled"] is True
    assert final["ok"] is False
    # 关键断言：失败记录一条都不许有
    assert fake.submitted == []


def test_cancel_without_running_task_is_soft_error(sc, configured):
    """没有任务在跑时取消：返回 ok=false 但仍是 200，别让前端弹红条"""
    resp = sc.post("/export/cancel", json={})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is False


# ========== 上一次运行遗留的任务（结算/对账）==========
OLD_TASK_ID = "20261024-090000-old00001"


def _old_task_dir(**status_over) -> Path:
    """造一个"上次运行时跑完、但没来得及上报"的任务目录（重启后只存在于磁盘）"""
    from client.sidecar.config import app_data_dir

    task_dir = app_data_dir() / "executions" / OLD_TASK_ID
    task_dir.mkdir(parents=True, exist_ok=True)
    params = {
        "author": "老任务",
        "title": "遗留稿",
        "template_id": "tpl-1.psd",
        "template_name": "中国收藏",
        "partner_name": "甲",
        "employee_name": "张三",
        "work_date": "2026-10-23",
    }
    (task_dir / "params.json").write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")
    status = {
        "state": "done",
        "success": True,
        "image_path": str(task_dir / "老任务.jpg"),
        "message": "",
        "warnings": [],
        "duration_ms": 4200,
    }
    status.update(status_over)
    (task_dir / "status.json").write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    # 用例之间共用同一个数据目录：上一个用例可能已经把它结算过了
    (task_dir / "result.json").unlink(missing_ok=True)
    return task_dir


def test_export_status_settles_task_from_previous_run(sc, configured, monkeypatch):
    """软件被关掉时留下的终态任务：下次打开轮询到它就必须补报

    旧版把上报写在轮询里，但结果缓存和上报是同一步——重启后 result.json 不在，
    这条记录只会在有人碰它时才被处理；而员工根本不会再点这个任务。
    """
    fake = FakeClient()
    monkeypatch.setattr(sidecar_server, "_client", lambda cfg: fake)
    _old_task_dir()

    data = sc.get(f"/export/status?task_id={OLD_TASK_ID}").get_json()

    assert data["finished"] is True
    assert data["uploaded"] is True
    assert fake.submitted[0]["fields"]["author"] == "老任务"
    assert fake.submitted[0]["fields"]["work_date"] == "2026-10-23", "补报不能改成今天"


def test_status_surfaces_unsynced_tasks(sc, configured, monkeypatch):
    """待补报次数要露在 /status 上：结算一直失败时员工得看得见"""
    import os

    monkeypatch.setattr(sidecar_server, "_client", lambda cfg: FakeClient())
    assert sc.get("/status").get_json()["unsynced"] == 0

    task_dir = _old_task_dir()
    # 刚跑完的不算待补报（正在结算，界面不该闪一下）
    assert sc.get("/status").get_json()["unsynced"] == 0
    stale = time.time() - 3600
    os.utime(task_dir / "status.json", (stale, stale))

    assert sc.get("/status").get_json()["unsynced"] >= 1
