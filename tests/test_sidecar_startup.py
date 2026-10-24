"""sidecar 启动：端口被占必须留下痕迹 + 父进程看门狗接线

打包后 sidecar 的 stdout/stderr 都是 `Stdio::null()`，异常栈进黑洞。旧版端口
被占（多半是上一次的僵尸 sidecar 还占着 8765）时只抛一个没人看得见的 OSError：
新 sidecar 死了，前端连上的其实是上一轮那个——busy 永远 true、配置是旧的，
而日志里一个字都没有。
"""

from __future__ import annotations

import logging
import socket

import pytest

from client.sidecar import parent_watch, server


def _free_port() -> int:
    """找一个当期没人用的端口（拿到后立刻释放，剩下的窗口期足够本用例）"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FakeApp:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.called: list[dict] = []

    def run(self, **kwargs) -> None:
        self.called.append(kwargs)
        if self.error is not None:
            raise self.error


def test_serve_reraises_bind_error_as_exit(caplog):
    """探针放过去但 app.run 还是绑不上（TOCTOU）：同样要留痕 + 退出码 1"""
    app = _FakeApp(OSError(10048, "通常每个套接字地址只允许使用一次"))

    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as exc:
            server.serve(app, 8765)

    assert exc.value.code == 1
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "8765" in text, "得说清是哪个端口"


def test_serve_only_binds_loopback():
    """只绑回环：局域网里别的机器不该能调本机 PS"""
    app = _FakeApp()

    server.serve(app, _free_port())

    assert app.called[0]["host"] == "127.0.0.1"


def test_probe_port_reports_squatter():
    """端口上已经有人应答时必须能提前发现

    Windows 上 werkzeug 开了 SO_REUSEADDR，端口被占时 bind 有可能照样成功——
    两个 sidecar 同时活着，请求落到谁手里看运气。所以自己裸 bind 探一次。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(5)
        port = held.getsockname()[1]

        problem = server.probe_port(port)

    assert problem, "占着的端口必须判成不可用"
    assert str(port) in problem


def test_probe_port_accepts_free_port():
    assert server.probe_port(_free_port()) == ""


def test_serve_exits_when_port_taken_by_real_listener(caplog):
    """真占住端口再启动：必须立刻 SystemExit，而不是卡在那儿或静默退出"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(5)
        port = held.getsockname()[1]

        with caplog.at_level(logging.ERROR):
            with pytest.raises(SystemExit):
                server.serve(_FakeApp(), port)

    assert any("已被占用" in r.getMessage() for r in caplog.records)


def test_serve_先探端口再启动():
    """顺序守的是"先探后绑"：只靠 app.run 抛异常，在 Windows 上不一定抛得出来

    werkzeug 会开 SO_REUSEADDR，端口被占时 bind 有可能成功，于是两个 sidecar
    同时活着。所以 probe_port 必须在 app.run 之前。
    """
    import inspect

    src = inspect.getsource(server.serve)
    assert src.index("probe_port") < src.index("app.run")


def test_main_starts_parent_watchdog(monkeypatch):
    """main 里必须真的启动看门狗：漏了这一句，僵尸 sidecar 会一直占着端口"""
    started: list[bool] = []
    monkeypatch.setattr(server.parent_watch, "start", lambda: started.append(True))
    monkeypatch.setattr(server, "serve", lambda app, port: None)
    monkeypatch.setattr(server, "_attach_file_logger", lambda name: None)
    monkeypatch.setattr(server.settler, "start_settler", lambda client: False)
    monkeypatch.setattr(server, "create_sidecar", lambda: _FakeApp())

    server.main()

    assert started == [True]


def test_main_survives_without_parent_pid(monkeypatch):
    """没配 PSLUYONG_PARENT_PID（手动跑/测试）时不能因此起不来"""
    monkeypatch.delenv(parent_watch.PARENT_PID_ENV, raising=False)
    monkeypatch.setattr(server, "serve", lambda app, port: None)
    monkeypatch.setattr(server, "_attach_file_logger", lambda name: None)
    monkeypatch.setattr(server.settler, "start_settler", lambda client: False)
    monkeypatch.setattr(server, "create_sidecar", lambda: _FakeApp())

    server.main()  # 不抛
