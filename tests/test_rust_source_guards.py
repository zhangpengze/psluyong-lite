"""lib.rs 里的源码守卫，在 CI 里其实跑不到

`.github/workflows/tests.yml` 的 client job 只跑 vue-tsc / vitest / build，
没有任何 cargo 步骤——lib.rs 里那些 `include_str!("lib.rs")` 的接线守卫
在流水线中等同于不存在。这里在 Python 侧再钉一遍同样的三件事。

顺便固定住刚踩的坑：lib.rs 是 CRLF，needle 里写 `\\n` 而磁盘上是 `\\r\\n`，
`contains` 永远为假。这不是"守卫变红"，是守卫失效——红绿都说明不了问题，
而且当时还被当成"变异抓到了"（变异前后都是红的，等于什么都没证明）。
"""

from __future__ import annotations

from pathlib import Path

from client.sidecar import parent_watch

ROOT = Path(__file__).resolve().parents[1]
LIB_RS = ROOT / "client" / "src-tauri" / "src" / "lib.rs"

NORMALIZE = '.replace("\\r\\n", "\\n")'


def _lib_source() -> str:
    """读 lib.rs 并统一成 LF：和 Rust 侧守卫的处理方式保持一致"""
    return LIB_RS.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_每个_include_str_守卫都先统一行尾():
    """带换行的 needle 必须先统一行尾，否则守卫恒定失配"""
    lines = [ln for ln in _lib_source().splitlines() if "include_str!" in ln]

    assert lines, "守卫没了？这里至少该有两处源码检查"
    for line in lines:
        assert NORMALIZE in line, f"这处守卫会因 CRLF 恒定失配：{line.strip()}"


def test_rust_侧的变量名与_python_一致():
    """两边各写一份常量，名字对不上看门狗就永远不启用（静默失效）"""
    expected = f'const PARENT_PID_ENV: &str = "{parent_watch.PARENT_PID_ENV}";'

    assert expected in _lib_source()


def test_sidecar_启动处确实把_pid_传下去了():
    """漏掉这一句，sidecar 不知道爹是谁，Tauri 被强杀后它一直占着 8765

    只靠"包装函数存在"不够：得确认包装函数真的被调用，且所有返回路径都过它。
    """
    src = _lib_source()

    assert "\n    tag_parent_pid(&mut cmd);\n" in src, "resolve_sidecar 包装层漏了 pid 传递"
    assert "resolve_sidecar_raw(app)?" in src, "所有返回路径都要经过同一个包装函数"
