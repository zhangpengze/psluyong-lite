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


def test_sidecar_启动后要放进_job():
    """Tauri 被强杀时，只有内核级 job 能一次收走 sidecar 与 worker

    漏调 `win_job::guard` 不会让任何逻辑测试变红——它只是悄悄少了一层兜底，
    所以这条（和 lib.rs 里那条同源的守卫）盯的是"有没有接线"。
    CI 不跑 cargo，所以 Python 侧必须有这一份。
    """
    src = _lib_source()

    assert "\nmod win_job;\n" in src, "模块没声明，guard 编译不进来"
    assert "win_job::guard(&child)" in src, "spawn 之后没有调 guard，兜底是空的"


def test_job_对象打开的是杀进程开关():
    """KILL_ON_JOB_CLOSE 才是全部意义；设错了这个 job 就只是个摆设"""
    job_src = (ROOT / "client" / "src-tauri" / "src" / "win_job.rs").read_text(
        encoding="utf-8"
    )

    assert "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE" in job_src
    assert "SetInformationJobObject" in job_src, "只建 job 不设 limit，等于没开兜底"


def _win_job_source() -> str:
    return (
        ROOT / "client" / "src-tauri" / "src" / "win_job.rs"
    ).read_text(encoding="utf-8").replace("\r\n", "\n")


def _win_job_code() -> str:
    """去掉 `//` 注释行：守卫要盯的是代码，注释里提到旧写法不算违规

    第一版守卫没过这一步，被自己那段"这里当初写错了"的注释绊倒——注释里正好
    引用了 `rc != WAIT_TIMEOUT`，于是守卫恒红。守卫红在正确的事上才有价值，
    红在注释上纯属噪音。
    """
    return "\n".join(
        ln for ln in _win_job_source().splitlines() if not ln.lstrip().startswith("//")
    )


def test_wait_exit_必须申请_SYNCHRONIZE():
    """`WaitForSingleObject` 要求句柄带 SYNCHRONIZE，少了它只会拿到 WAIT_FAILED

    这一条是真踩出来的：原来写的是 `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`，
    每次都返回 WAIT_FAILED，被判成"已退出"——于是两条 job 用例永远绿，
    `LimitFlags` 改成 0 也照样绿。CI 不跑 cargo，所以这条守卫放在 Python 侧。
    """
    code = _win_job_code()

    assert (
        "OpenProcess(SYNCHRONIZE, FALSE, pid)" in code
    ), "wait_exit 拿不到 SYNCHRONIZE 就永远'已退出'"
    assert "rc != WAIT_TIMEOUT" not in code, "WAIT_FAILED 被当成'已退出'了，要三态返回"


def test_job_用例先证明进程活着再断言它被杀():
    """没有前置的"还活着"，用例在进程根本没起来（或 wait_exit 说谎）时照样绿

    对照组（LimitFlags=0 时不杀进程）同样重要：它证明"关句柄杀进程"确实是这个
    标志位干的，而不是进程碰巧自己退了。
    """
    src = _win_job_source()

    assert src.count("assert_alive(child.id());") >= 2, "关闭句柄前后都得先确认它活着"
    assert "create_job_with_limit(0)" in src, "缺对照组：没有它就无法排除'碰巧自己退了'"
