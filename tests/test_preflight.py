"""scripts/preflight_check.py 的测试。

这个脚本是上线前的最后一闸门，它自己的错误分两种，都很贵：
  - 该拦的没拦（把 FAIL 判成 PASS）：带着"只有一份成品图"上线，盘一坏全没
  - 不该拦的拦了（把 TODO 判成 FAIL）：网络盘上 WAL 本来就不可用，误报会让人
    学会"跑不过也照样上线"，闸门从此失效
所以这里把每一档的三态判据都钉住，另外钉住退出码——CI 只看退出码。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "preflight_check", REPO / "scripts" / "preflight_check.py"
)
assert SPEC and SPEC.loader
pf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pf)


# ---------- 造一个假的 responses.get ----------

class _FakeResponse:
    def __init__(self, status_code: int, body, *, is_json: bool = True):
        self.status_code = status_code
        self._body = body
        self._is_json = is_json

    def json(self):
        if not self._is_json:
            raise ValueError("not json")
        return self._body


@pytest.fixture()
def patch_get(monkeypatch):
    """替换 requests.get：脚本在函数内部 import requests，patch 模块属性即可"""

    def _install(response=None, exc=None):
        def fake_get(url, timeout=None, **kwargs):
            if exc is not None:
                raise exc
            return response

        monkeypatch.setattr("requests.get", fake_get)

    return _install


# ---------- 终端对齐：中文占两格 ----------

def test_display_width_counts_cjk_as_two_cells():
    assert pf._display_width("abc") == 3
    assert pf._display_width("不通过") == 6
    assert pf._display_width("密钥非示例值") == 12


def test_pad_aligns_by_display_width_not_len():
    # "钥匙" 只有 2 个字符，但占 4 格：按 len 补齐会少补两格
    assert pf._display_width("钥匙") == 4
    assert pf._pad("钥匙", 4) == "钥匙"
    assert pf._pad("钥匙", 6) == "钥匙  "
    assert pf._pad("abc", 6) == "abc   "


def test_table_columns_line_up_in_terminal(monkeypatch, capsys):
    """三种状态标记宽度不同（通过/不通过/需人工），但明细必须从同一列开始。

    注意比的是"显示列"不是字符下标：中文一个字占两格，index() 看不出来。
    """
    report = {
        "rows": [
            pf._row("短", pf.PASS, "明细一", ""),
            pf._row("长一点的项目", pf.FAIL, "明细二", "照着改"),
            pf._row("中等项目", pf.TODO, "明细三", ""),
        ],
        "manual": [],
        "passed": 1,
        "failed": 1,
        "todo": 1,
    }
    monkeypatch.setattr(pf, "run", lambda base_url="": report)
    capsys.readouterr()
    pf.main([])
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "明细" in ln]
    assert len(lines) == 3
    columns = {pf._display_width(ln[: ln.index("明细")]) for ln in lines}
    assert len(columns) == 1, f"明细列没对齐：{lines}"


# ---------- 退出码：CI 只看这个 ----------

def _install_main(monkeypatch, report):
    class _Ctx:
        def push(self):
            pass

        def pop(self):
            pass

    class _App:
        def app_context(self):
            return _Ctx()

    monkeypatch.setattr(pf, "run", lambda base_url="": report)
    monkeypatch.setattr("server.app.create_app", lambda *a, **kw: _App())


def _report(rows):
    return {
        "rows": rows,
        "manual": [],
        "passed": sum(1 for r in rows if r["status"] == pf.PASS),
        "failed": sum(1 for r in rows if r["status"] == pf.FAIL),
        "todo": sum(1 for r in rows if r["status"] == pf.TODO),
    }


def test_all_pass_exits_zero(monkeypatch):
    _install_main(monkeypatch, _report([pf._row("a", pf.PASS, "ok", "")]))
    assert pf.main([]) == 0


def test_any_fail_exits_one(monkeypatch):
    _install_main(
        monkeypatch,
        _report([
            pf._row("a", pf.PASS, "ok", ""),
            pf._row("b", pf.FAIL, "坏了", "修它"),
        ]),
    )
    assert pf.main([]) == 1


def test_todo_alone_does_not_block_the_gate(monkeypatch):
    """TODO 是"机器判不了"，不是"不通过"。混为一谈会让人直接无视闸门"""
    _install_main(
        monkeypatch,
        _report([
            pf._row("a", pf.PASS, "ok", ""),
            pf._row("b", pf.TODO, "需要人看", ""),
        ]),
    )
    assert pf.main([]) == 0


def test_app_context_failure_exits_two(monkeypatch, capsys):
    def boom(*a, **kw):
        raise RuntimeError("no flask app")

    monkeypatch.setattr("server.app.create_app", boom)
    assert pf.main([]) == 2
    assert "无法初始化服务上下文" in capsys.readouterr().err


def test_json_output_is_machine_readable(monkeypatch, capsys):
    _install_main(
        monkeypatch,
        _report([pf._row("密钥非示例值", pf.PASS, "ok", "")]),
    )
    capsys.readouterr()
    assert pf.main(["--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["rows"][0]["item"] == "密钥非示例值"
    assert body["passed"] == 1


# ---------- 单项炸了不能拖垮整张表 ----------

def test_broken_check_becomes_fail_row_and_others_still_run(monkeypatch):
    seen = []

    def boom():
        raise OSError("磁盘没了")

    def fine():
        seen.append(True)
        return pf.PASS, "好的", ""

    monkeypatch.setattr(pf, "CHECKS", [("坏项", boom), ("好项", fine)])
    monkeypatch.setattr(pf, "check_readiness", lambda base_url: (pf.TODO, "跳过", ""))

    report = pf.run("")

    assert report["failed"] == 1
    row = next(r for r in report["rows"] if r["item"] == "坏项")
    assert row["status"] == pf.FAIL
    assert "OSError" in row["detail"]  # 报出是哪类错，不然没法转给运维
    assert seen, "前一项炸了，后面的检查就不跑了——那等于少查了好几项"


def test_readiness_row_always_present(monkeypatch):
    monkeypatch.setattr(pf, "CHECKS", [])
    monkeypatch.setattr(pf, "check_readiness", lambda base_url: (pf.TODO, "没给地址", ""))
    report = pf.run("")
    assert [r["item"] for r in report["rows"]] == ["就绪探针"]


# ---------- 密钥 ----------

def test_secrets_all_good_passes(monkeypatch):
    from server.app.config import Config

    monkeypatch.setattr(Config, "SECRET_KEY", "a" * 40)
    monkeypatch.setattr(Config, "API_KEY", "b" * 40)
    monkeypatch.setattr(Config, "ADMIN_PASSWORD", "c" * 40)
    assert pf.check_secrets()[0] == pf.PASS


@pytest.mark.parametrize(
    "value,expect",
    [
        ("", "为空"),
        ("   ", "为空"),
        ("changeme", "示例值"),
        ("your-secret-key", "示例值"),
        ("please-change-me-please", "示例值"),  # 含 changeme
        ("example-key-long-enough", "示例值"),
        ("short", "太短"),
    ],
)
def test_secrets_reject_empty_placeholder_and_short(monkeypatch, value, expect):
    from server.app.config import Config

    monkeypatch.setattr(Config, "SECRET_KEY", "a" * 40)
    monkeypatch.setattr(Config, "API_KEY", "b" * 40)
    monkeypatch.setattr(Config, "ADMIN_PASSWORD", value)
    status, detail, hint = pf.check_secrets()
    assert status == pf.FAIL
    assert expect in detail
    assert "rotate_secrets" in hint  # 光说"不合格"没用，得给出怎么改


def test_secrets_reports_every_bad_key_not_just_the_first(monkeypatch):
    from server.app.config import Config

    monkeypatch.setattr(Config, "SECRET_KEY", "")
    monkeypatch.setattr(Config, "API_KEY", "changeme")
    monkeypatch.setattr(Config, "ADMIN_PASSWORD", "x")
    detail = pf.check_secrets()[1]
    assert "SECRET_KEY" in detail and "API_KEY" in detail and "ADMIN_PASSWORD" in detail


# ---------- 备份 ----------

def test_backup_missing_dir_fails(monkeypatch, tmp_path):
    monkeypatch.setattr("server.app.config.BACKUP_DIR", tmp_path / "nope")
    assert pf.check_backup_dir()[0] == pf.FAIL


def test_backup_empty_dir_fails(monkeypatch, tmp_path):
    d = tmp_path / "backups"
    d.mkdir()
    monkeypatch.setattr("server.app.config.BACKUP_DIR", d)
    status, detail, hint = pf.check_backup_dir()
    assert status == pf.FAIL
    assert "没有任何" in detail
    assert "server.backup" in hint


def test_backup_present_passes_and_names_newest(monkeypatch, tmp_path):
    d = tmp_path / "backups"
    d.mkdir()
    (d / "app-20261020.db.gz").write_bytes(b"x")
    (d / "app-20261024.db.gz").write_bytes(b"x")
    monkeypatch.setattr("server.app.config.BACKUP_DIR", d)
    status, detail, _ = pf.check_backup_dir()
    assert status == pf.PASS
    assert "2 份" in detail and "app-20261024.db.gz" in detail


# ---------- 图片第二份：本项目最大的单点 ----------

def test_mirror_unconfigured_fails(monkeypatch):
    from server.app.config import Config

    monkeypatch.setattr(Config, "IMAGES_MIRROR_DIR", "")
    status, detail, hint = pf.check_mirror_dir()
    assert status == pf.FAIL
    assert "只有一份" in detail
    assert "NAS" in hint or "盘" in hint


def test_mirror_missing_path_fails(monkeypatch, tmp_path):
    from server.app.config import Config

    monkeypatch.setattr(Config, "IMAGES_MIRROR_DIR", str(tmp_path / "gone"))
    assert pf.check_mirror_dir()[0] == pf.FAIL


def test_mirror_unwritable_fails(monkeypatch, tmp_path):
    from server.app.config import Config

    target = tmp_path / "mirror"
    target.mkdir()
    monkeypatch.setattr(Config, "IMAGES_MIRROR_DIR", str(target))

    def deny(*a, **kw):
        raise PermissionError("拒绝访问")

    monkeypatch.setattr(Path, "write_bytes", deny)
    status, detail, hint = pf.check_mirror_dir()
    assert status == pf.FAIL
    assert "不可写" in detail
    assert "权限" in hint or "凭据" in hint


def test_mirror_on_same_drive_is_only_todo(monkeypatch, tmp_path):
    """同一块盘坏了两份一起没，值得提醒，但不算"没配"——不能判 FAIL 挡住上线"""
    from server.app.config import Config

    target = tmp_path / "mirror"
    target.mkdir()
    monkeypatch.setattr(Config, "IMAGES_MIRROR_DIR", str(target))
    monkeypatch.setattr("server.app.config.IMAGES_DIR", tmp_path / "images")
    status, detail, hint = pf.check_mirror_dir()
    if target.drive:  # Windows 才有盘符，POSIX 上这条判据本就不适用
        assert status == pf.TODO
        assert "同一分区" in detail
    else:
        assert status == pf.PASS


def test_mirror_on_other_drive_passes(monkeypatch, tmp_path):
    from server.app.config import Config

    target = tmp_path / "mirror"
    target.mkdir()
    monkeypatch.setattr(Config, "IMAGES_MIRROR_DIR", str(target))
    other = "Z:" if target.drive.lower() != "z:" else "Y:"
    monkeypatch.setattr("server.app.config.IMAGES_DIR", Path(f"{other}/images"))
    status, _, _ = pf.check_mirror_dir()
    assert status == pf.PASS


# ---------- 图片目录 ----------

def test_images_dir_missing_is_todo_not_fail(monkeypatch, tmp_path):
    """首次上传前目录本来就不存在，判 FAIL 会让第一次上线永远过不了"""
    monkeypatch.setattr("server.app.config.IMAGES_DIR", tmp_path / "nope")
    assert pf.check_images_dir()[0] == pf.TODO


def test_images_dir_counts_files(monkeypatch, tmp_path):
    d = tmp_path / "images"
    (d / "2026-10-24").mkdir(parents=True)
    (d / "2026-10-24" / "a.jpg").write_bytes(b"x")
    (d / "2026-10-24" / "b.jpg").write_bytes(b"x")
    monkeypatch.setattr("server.app.config.IMAGES_DIR", d)
    status, detail, _ = pf.check_images_dir()
    assert status == pf.PASS
    assert "2 个文件" in detail


# ---------- WAL ----------

def test_wal_mode_passes_on_real_db(app):
    """真实库就是 WAL：这条走真连接，不是替身，防止判据本身写错"""
    from server.app.db import db

    with app.app_context():
        status, detail, _ = pf.check_wal_mode()
    assert status == pf.PASS
    assert "wal" in detail
    assert db.session is not None


def _patch_session(monkeypatch, behaviour):
    from server.app.db import db

    class _Sess:
        def execute(self, *a, **kw):
            return behaviour()

    monkeypatch.setattr(db, "session", _Sess())


def test_non_wal_is_todo_not_fail(monkeypatch):
    """DEPLOY 明确允许网络盘上改 DELETE：判 FAIL 就是误报，会让人养成无视闸门的习惯"""

    class _R:
        def scalar(self):
            return "delete"

    _patch_session(monkeypatch, lambda: _R())
    status, detail, hint = pf.check_wal_mode()
    assert status == pf.TODO
    assert "delete" in detail
    assert "网络盘" in hint


def test_wal_read_error_fails(monkeypatch):
    def boom():
        raise RuntimeError("数据库锁住了")

    _patch_session(monkeypatch, boom)
    assert pf.check_wal_mode()[0] == pf.FAIL


# ---------- 调度器 ----------

@pytest.mark.parametrize("enabled", [True, False])
def test_scheduler_is_never_pass_or_fail(monkeypatch, enabled):
    """独立脚本里看不到调度器有没有在跑，硬判 PASS 就是自欺"""
    from server.app.config import Config

    monkeypatch.setattr(Config, "ENABLE_SCHEDULER", enabled)
    assert pf.check_scheduler()[0] == pf.TODO


# ---------- 仓库卫生 ----------

def _patch_git_files(monkeypatch, files):
    class _R:
        stdout = "\n".join(files)

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: _R())


def test_no_env_tracked_passes(monkeypatch):
    _patch_git_files(monkeypatch, [".env.example", "README.md", "server/app/config.py"])
    assert pf.check_git_env_hygiene()[0] == pf.PASS


@pytest.mark.parametrize("leaked", [".env", ".env.bak", "server/.env.local"])
def test_tracked_env_fails(monkeypatch, leaked):
    _patch_git_files(monkeypatch, [".env.example", leaked])
    status, detail, hint = pf.check_git_env_hygiene()
    assert status == pf.FAIL
    assert leaked in detail
    assert "git rm --cached" in hint


def test_git_unavailable_is_todo(monkeypatch):
    def boom(*a, **kw):
        raise OSError("git not found")

    monkeypatch.setattr("subprocess.run", boom)
    assert pf.check_git_env_hygiene()[0] == pf.TODO


# ---------- 就绪探针 ----------

def test_readiness_without_base_url_is_todo():
    status, detail, hint = pf.check_readiness("")
    assert status == pf.TODO
    assert "--base-url" in hint


def test_readiness_ok_passes(patch_get):
    patch_get(
        _FakeResponse(
            200,
            {
                "ok": True,
                "status": "ready",
                "checks": {"db": {"state": "ok"}, "data_dir": {"state": "ok"}},
            },
        )
    )
    status, detail, _ = pf.check_readiness("http://127.0.0.1:8080")
    assert status == pf.PASS
    assert "db=ok" in detail


def test_readiness_503_fails_and_says_where_to_look(patch_get):
    patch_get(
        _FakeResponse(
            503,
            {
                "ok": False,
                "status": "degraded",
                "checks": {"db": {"state": "ok"}, "data_dir": {"state": "error"}},
            },
        )
    )
    status, detail, hint = pf.check_readiness("http://127.0.0.1:8080")
    assert status == pf.FAIL
    assert "503" in detail
    assert "data_dir" in hint


def test_readiness_connection_error_fails(patch_get):
    patch_get(exc=ConnectionError("refused"))
    status, detail, hint = pf.check_readiness("http://127.0.0.1:8080")
    assert status == pf.FAIL
    assert "ConnectionError" in detail
    assert "启动" in hint


def test_readiness_non_json_response_fails(patch_get):
    """地址指到了别的服务上（比如 80 端口的另一个站），要点破而不是报 JSON 解析栈"""
    patch_get(_FakeResponse(200, None, is_json=False))
    status, detail, hint = pf.check_readiness("http://127.0.0.1:8080")
    assert status == pf.FAIL
    assert "不是 JSON" in detail
    assert "别的服务" in hint


def test_readiness_strips_trailing_slash(patch_get, monkeypatch):
    seen = {}

    def fake_get(url, timeout=None, **kw):
        seen["url"] = url
        return _FakeResponse(200, {"ok": True, "checks": {}})

    monkeypatch.setattr("requests.get", fake_get)
    pf.check_readiness("http://127.0.0.1:8080/")
    assert seen["url"] == "http://127.0.0.1:8080/health/ready"


# ---------- 人工清单 ----------

def test_manual_list_survives_every_automated_pass(monkeypatch):
    """机器全过也不等于能上线：PS 版本、跑通一张图、备份演练都得人看"""
    monkeypatch.setattr(pf, "CHECKS", [])
    monkeypatch.setattr(pf, "check_readiness", lambda base_url: (pf.PASS, "ok", ""))
    report = pf.run("http://127.0.0.1:8080")
    assert report["failed"] == 0
    assert len(report["manual"]) >= 5


def test_readiness_bypasses_system_proxy(monkeypatch):
    """运维笔记本上配了 HTTP_PROXY 时，探自己的服务不能被送去代理：
    代理会回 502，读起来像"服务挂了"，实际它好好跑着。"""
    seen = {}

    def fake_get(url, timeout=None, **kw):
        seen.update(kw)
        return _FakeResponse(200, {"ok": True, "checks": {}})

    monkeypatch.setattr("requests.get", fake_get)
    pf.check_readiness("http://127.0.0.1:8080")
    assert seen.get("proxies") == {"http": None, "https": None}


def test_readiness_bypasses_system_proxy(monkeypatch):
    """运维笔记本上配了 HTTP_PROXY 时，探自己的服务不能被送去代理：
    代理会回 502，读起来像"服务挂了"，实际它好好跑着。"""
    seen = {}

    def fake_get(url, timeout=None, **kw):
        seen.update(kw)
        return _FakeResponse(200, {"ok": True, "checks": {}})

    monkeypatch.setattr("requests.get", fake_get)
    pf.check_readiness("http://127.0.0.1:8080")
    assert seen.get("proxies") == {"http": None, "https": None}
