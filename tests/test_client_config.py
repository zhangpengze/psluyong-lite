"""客户端配置落盘：原子写 / 备份自愈 / 逐字段校验

config.json 是员工机器上**唯一**的配置来源，写坏等于客户端报废——没有
自动更新，只能逐台远程修。所以这里的用例都用真文件跑，不打桩 open：

1. 写一半失败（断电/磁盘满）不能把旧文件毁掉
2. 读到坏文件要先留档、再试备份，实在不行才回默认值，并且**把话说出来**
3. 一个字段类型不对只影响它自己，不能让所有接口 500
4. 并发保存不能互相覆盖（窗口位置记忆 + 设置页保存会撞车）
"""

from __future__ import annotations

import json
import threading

import pytest

from client.sidecar import config as cfgmod
from client.sidecar.config import ClientConfig, load_config, save_config, update_config


@pytest.fixture()
def cfgfile(tmp_path, monkeypatch):
    """把落盘路径指到临时目录，并清掉进程级"上次自愈"状态"""
    path = tmp_path / "cfg" / "config.json"
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", path)
    cfgmod.reset_last_recovery()
    yield path
    cfgmod.reset_last_recovery()


def _bak(path):
    return path.with_name(path.name + ".bak")


def _write(path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")


def _two_saves(**kwargs) -> None:
    """存两次：第一次建文件，第二次才有 .bak（和真实使用一致）"""
    save_config(ClientConfig(**kwargs))
    save_config(load_config())


# ==================== 原子写 ====================


def test_first_save_creates_file_without_backup(cfgfile):
    save_config(ClientConfig(api_key="k1"))
    assert json.loads(cfgfile.read_text(encoding="utf-8"))["api_key"] == "k1"
    assert not _bak(cfgfile).exists(), "没有旧文件时不该凭空造一个备份"


def test_second_save_keeps_previous_version_as_backup(cfgfile):
    save_config(ClientConfig(api_key="k1"))
    save_config(ClientConfig(api_key="k2"))
    assert json.loads(cfgfile.read_text(encoding="utf-8"))["api_key"] == "k2"
    assert json.loads(_bak(cfgfile).read_text(encoding="utf-8"))["api_key"] == "k1"


def test_no_tmp_files_left_behind(cfgfile):
    save_config(ClientConfig(api_key="k1"))
    save_config(ClientConfig(api_key="k2"))
    leftovers = [p.name for p in cfgfile.parent.iterdir() if p.suffix == ".tmp"]
    assert leftovers == [], f"临时文件没清掉：{leftovers}"


def test_interrupted_save_leaves_old_file_intact(cfgfile, monkeypatch):
    """模拟断电/磁盘满：换名那一步失败，旧文件必须还是旧文件。

    旧实现是 write_text（先 truncate 再写），这一步失败的结果是一个
    0 字节的 config.json —— 员工的 Key、NAS 地址全没。
    """
    save_config(ClientConfig(api_key="k1", employee_name="张三"))
    before = cfgfile.read_text(encoding="utf-8")

    def boom(_tmp, _path):
        raise OSError("磁盘满了")

    real_replace = cfgmod._replace_with_retry
    monkeypatch.setattr(cfgmod, "_replace_with_retry", boom)
    with pytest.raises(OSError):
        save_config(ClientConfig(api_key="k2", employee_name="张三"))

    assert cfgfile.read_text(encoding="utf-8") == before, "保存失败不能毁掉旧配置"
    assert not [p for p in cfgfile.parent.iterdir() if p.suffix == ".tmp"]

    # 只恢复换名函数（不能用 monkeypatch.undo：那会把 cfgfile 的路径桩一起撤掉）
    monkeypatch.setattr(cfgmod, "_replace_with_retry", real_replace)
    assert load_config().api_key == "k1"


# ==================== 自愈 ====================


def test_truncated_file_recovered_from_backup(cfgfile):
    """写一半断电留下空文件：必须从 .bak 把 Key 捞回来"""
    _two_saves(api_key="k1", employee_name="张三", server_url="http://nas:8080")
    _write(cfgfile, "")

    cfg = load_config()
    assert cfg.api_key == "k1", "备份里的 Key 必须恢复"
    assert cfg.employee_name == "张三"
    assert json.loads(cfgfile.read_text(encoding="utf-8"))["api_key"] == "k1", "恢复后要写回一份完整文件"
    assert list(cfgfile.parent.glob("config.broken-*.json")), "坏文件要留档，不能静默删掉"
    assert "备份" in cfgmod.last_recovery()


def test_broken_file_quarantined_and_reported(cfgfile):
    _write(cfgfile, '{"api_key": "k1"')
    cfg = load_config()
    assert cfg.api_key == ""
    assert list(cfgfile.parent.glob("config.broken-*.json"))
    assert "损坏" in cfgmod.last_recovery()
    assert "API Key" in cfgmod.last_recovery(), "要告诉员工去重填 Key，而不是只说'出错了'"
    # 已经写回一份可解析的文件，下次启动不再反复自愈
    assert json.loads(cfgfile.read_text(encoding="utf-8"))["api_key"] == ""


def test_non_object_json_treated_as_broken(cfgfile):
    _write(cfgfile, "[1, 2, 3]")
    assert load_config().server_url == ClientConfig().server_url
    assert "顶层不是 JSON 对象" in cfgmod.last_recovery()


def test_corrupt_file_never_overwrites_good_backup(cfgfile):
    """坏文件状态下再保存一次，不能把好的 .bak 覆盖成坏的"""
    _two_saves(api_key="k1")
    assert json.loads(_bak(cfgfile).read_text(encoding="utf-8"))["api_key"] == "k1"

    _write(cfgfile, "{坏掉了")
    save_config(ClientConfig(employee_name="李四"))

    assert json.loads(_bak(cfgfile).read_text(encoding="utf-8"))["api_key"] == "k1"


def test_missing_config_restores_from_backup(cfgfile):
    """员工只留了 .bak（或误删了 config.json）：也能恢复"""
    _two_saves(api_key="k1", employee_name="张三")
    cfgfile.unlink()
    cfg = load_config()
    assert cfg.api_key == "k1"
    assert cfgfile.exists()


def test_trailing_space_key_is_trimmed(cfgfile):
    """API Key 后面多一个空格是最经典的「Key 明明对却 401」"""
    _write(cfgfile, {"api_key": " k1 \n", "employee_name": " 张三 "})
    assert load_config().api_key == "k1"
    assert load_config().employee_name == "张三"


# ==================== 暂时读不到 ≠ 文件坏了 ====================


def _make_unreadable(monkeypatch):
    """模拟"文件被别的程序占着"（Windows 上换名瞬间/杀软扫描/备份软件）"""
    monkeypatch.setattr(
        cfgmod, "_read_text_with_retry", lambda _path: (None, "共享冲突", True)
    )


def test_transient_read_error_keeps_file_and_does_not_quarantine(cfgfile, monkeypatch):
    """读不到就回默认值？不行——那会顺手把好文件改名留档，再写一份默认值进去"""
    _two_saves(api_key="k1", employee_name="张三")
    before = cfgfile.read_text(encoding="utf-8")

    _make_unreadable(monkeypatch)
    cfg = load_config()
    assert cfg.api_key == "", "读不到只能用内存默认值顶着"
    assert cfgfile.read_text(encoding="utf-8") == before, "磁盘上的文件一个字都不能动"
    assert not list(cfgfile.parent.glob("config.broken-*.json")), "暂时读不到不是损坏，不该留档"


def test_transient_read_error_blocks_write(cfgfile, monkeypatch):
    """读不到当前内容还硬写 = 拿默认值把员工 Key 冲掉，所以必须拒绝写入"""
    _two_saves(api_key="k1", employee_name="张三")
    before = cfgfile.read_text(encoding="utf-8")

    _make_unreadable(monkeypatch)
    with pytest.raises(cfgmod.ConfigBusyError):
        update_config({"theme": "light"})
    assert cfgfile.read_text(encoding="utf-8") == before


# ==================== 逐字段校验 ====================


def test_wrong_type_field_only_affects_itself(cfgfile):
    """`{"output_dir": 123}` 以前会让所有接口 500，界面上根本修不回来"""
    _write(
        cfgfile,
        {
            "output_dir": 123,
            "employee_name": "张三",
            "jpg_quality": 999,
            "theme": "rainbow",
            "partners": "甲",
            "win_x": "左边",
        },
    )
    cfg = load_config()
    defaults = ClientConfig()
    assert cfg.employee_name == "张三", "一个字段坏了不能牵连别的字段"
    assert cfg.output_dir == defaults.output_dir
    assert cfg.jpg_quality == 12, "越界值钳到上界，而不是悄悄回默认 10"
    assert cfg.theme == "dark"
    assert cfg.partners == []
    assert cfg.win_x == defaults.win_x


def test_unknown_keys_ignored(cfgfile):
    """降级/升级来回切时旧版本多出来的键不能把客户端搞崩"""
    _write(cfgfile, {"api_key": "k1", "some_future_key": {"a": 1}})
    assert load_config().api_key == "k1"


def test_normalization_still_applied_after_load(cfgfile):
    _write(cfgfile, {"output_dir": "C://Users//张三//期刊录用", "partners": ["甲", " 甲 ", ""]})
    cfg = load_config()
    assert cfg.output_dir == "C:/Users/张三/期刊录用"
    assert cfg.partners == ["甲"]


# ==================== 并发 ====================


def test_concurrent_update_keeps_every_field(cfgfile):
    """三个窗口同时保存窗口位置：谁也不能把别人的字段盖回去"""
    save_config(ClientConfig())
    fields = ["win_x", "win_y", "history_win_x", "history_win_y", "settings_win_x", "settings_win_y"]
    barrier = threading.Barrier(len(fields))
    errors: list[BaseException] = []

    def worker(index: int, field: str) -> None:
        try:
            barrier.wait(timeout=5)
            for _ in range(5):
                update_config({field: 100 + index})
        except BaseException as e:  # noqa: BLE001 - 线程里抛错要带回主线程
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i, f)) for i, f in enumerate(fields)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors, errors
    cfg = load_config()
    for index, field in enumerate(fields):
        assert getattr(cfg, field) == 100 + index, f"{field} 被别的写入盖掉了"


def test_update_config_preserves_other_fields(cfgfile):
    save_config(ClientConfig(api_key="k1", employee_name="张三"))
    update_config({"theme": "light"})
    cfg = load_config()
    assert cfg.api_key == "k1" and cfg.employee_name == "张三" and cfg.theme == "light"
