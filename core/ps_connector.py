import contextlib
import logging
import sys
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import List

from . import ps_retry
from .ps_doc_ops import apply_document_replacements
from .ps_text import match_text as _match_text_impl
from .ps_text import replace_text as _replace_text_impl

logger = logging.getLogger(__name__)

# Windows COM 依赖改为可选导入。
# 模块级硬 import 会让非 Windows 环境（CI、Linux 上的后端容器）无法 import 本包，
# 连不碰 PS 的单元测试都跑不起来。真正调用 connect() 时才校验 COM_AVAILABLE。
try:
    import pythoncom
    import pywintypes
    import win32com.client  # type: ignore

    COM_AVAILABLE = True
    COM_ERRORS: tuple[type[BaseException], ...] = (pywintypes.com_error,)
except ImportError:  # pragma: no cover - 非 Windows 环境
    pythoncom = None  # type: ignore[assignment]
    pywintypes = None  # type: ignore[assignment]
    win32com = None  # type: ignore[assignment]
    COM_AVAILABLE = False

    class _NeverRaisedError(Exception):
        """占位异常：非 Windows 环境没有 COM 错误类型，用它填充 except 元组。"""

    COM_ERRORS = (_NeverRaisedError,)


def ensure_com_available() -> None:
    """在真正发起 COM 调用前校验运行环境。"""
    if not COM_AVAILABLE:
        raise RuntimeError(
            f"Photoshop 自动化仅支持 Windows（当前平台: {sys.platform}）。"
            "请确认已安装 pywin32：pip install pywin32"
        )


class PSConnector:
    """
    Photoshop COM connector and operations.
    """

    def __init__(self) -> None:
        self.app = None

    @staticmethod
    def _connect_failed_message(cold_start: bool) -> str:
        """连不上时给员工的下一步。区分"第一次启动慢"和"真连不上"。"""
        budget = int(ps_retry.budget_for(cold_start))
        if cold_start:
            head = (
                f"Photoshop 没能在 {budget} 秒内启动完成。\n"
                "第一次启动 PS 本来就慢，请等 PS 界面完全出来（能看到工具栏）后再点一次「生成录用图」。"
            )
        else:
            head = f"连不上已经在运行的 Photoshop（已等 {budget} 秒）。"
        return (
            f"{head}\n"
            "还不行就按顺序检查：\n"
            "1. Photoshop 能手动打开，而且没有卡在弹窗上\n"
            "2. 设置里的 Photoshop 路径指向 Photoshop.exe（不是快捷方式）\n"
            "3. 尝试以管理员身份运行本程序\n"
            "4. 重装/修复安装 Photoshop（COM 组件可能未注册）"
        )

    def connect(self, photoshop_exe: str = "") -> None:
        """连接 Photoshop；PS 没开就先把它拉起来，再等 COM 就绪。

        重试预算按"PS 在不在跑"分档（冷启 60s / 已运行 10s），原因见 ps_retry
        文件头：旧实现只有约 6 秒，而冷启动要 15~40 秒——员工装好软件后第一次
        点生成必然失败，还会入库一条 failed 并触发失败告警。

        Parameters
        ----------
        photoshop_exe:
            配置里的 Photoshop 路径。以前赋值后从未使用，现在用来提前拉起进程。
        """
        ensure_com_available()

        # 在未初始化 COM 的线程（如 sidecar/服务线程）中必须先 CoInitialize，
        # 否则 Dispatch 抛 "尚未调用 CoInitialize" (-2147221008)。
        # 已初始化的线程重复调用是幂等的（嵌套计数），无副作用。
        try:
            pythoncom.CoInitialize()
        except Exception:  # noqa: BLE001
            pass

        cold_start = not ps_retry.is_photoshop_running(win32com)
        if cold_start:
            ps_retry.prelaunch(photoshop_exe)

        deadline = time.monotonic() + ps_retry.budget_for(cold_start)
        attempt = 0
        last_exception: Exception | None = None

        while attempt < ps_retry.MAX_ATTEMPTS:
            try:
                self.app = win32com.client.Dispatch("Photoshop.Application")
                try:
                    if not self.app.Visible:
                        self.app.Visible = True
                except (AttributeError,) + COM_ERRORS:
                    pass
                if attempt:
                    logger.info("Photoshop 已就绪（等了 %d 轮）", attempt)
                return
            except Exception as exc:
                kind = ps_retry.classify(exc)
                if kind == ps_retry.FATAL:
                    # 没装/没注册/已崩溃：再等也没用，别让员工对着转圈等 60 秒
                    raise RuntimeError(ps_retry.fatal_message(exc)) from exc
                if kind != ps_retry.RETRY:
                    raise
                last_exception = exc
                attempt += 1
                if attempt >= ps_retry.MAX_ATTEMPTS or time.monotonic() >= deadline:
                    break
                delay = ps_retry.retry_delay(attempt - 1)
                logger.info("Photoshop 启动中，%.1f 秒后重试（第 %d 次）", delay, attempt)
                time.sleep(delay)

        raise RuntimeError(self._connect_failed_message(cold_start)) from last_exception

    def set_display_dialogs(self, mode: int = 3) -> None:
        """
        Set Photoshop display dialogs mode.
        1: psDisplayErrorDialogs
        2: psDisplayAllDialogs
        3: psDisplayNoDialogs
        """
        if self.app:
            with contextlib.suppress((AttributeError,) + COM_ERRORS):
                self.app.DisplayDialogs = mode

    def open_document(self, psd_path: Path):
        """
        Open a PSD document in Photoshop.
        """
        if self.app is None:
            raise RuntimeError("Photoshop COM 未连接")
        try:
            # Ensure absolute path to avoid ambiguity
            return self.app.Open(str(psd_path.resolve()))
        except Exception as exc:
            # Check for common "User Cancelled" error (happens if PS dialogs
            # are blocking or user hits cancel on font missing)
            # Or file corrupt.
            # We can't fix corrupt files, but we can give better error message.
            raise RuntimeError(
                f"PSD 文件无法打开: {psd_path}\n"
                f"原因: {exc}\n"
                "请检查文件是否损坏，或 Photoshop 是否有未处理的弹窗。"
            ) from exc

    def _match_text(self, source: str, pattern: str, regex: bool, match_mode: str) -> bool:
        """Check whether text matches pattern.（实现收拢到 ps_text，方法保留供调用方使用）"""
        return _match_text_impl(source, pattern, regex, match_mode)

    def _replace_text(
        self, source: str, pattern: str, replacement: str, regex: bool, match_mode: str
    ) -> str:
        """Replace text according to configuration.（实现收拢到 ps_text，方法保留供调用方使用）"""
        return _replace_text_impl(source, pattern, replacement, regex, match_mode)

    def get_all_text_layers(self, document) -> List[str]:
        """
        Scan document and return list of all text layer contents.
        """
        texts = []

        def process_layers(layers_obj):
            """
            递归遍历图层组，查找所有文本图层的内容。

            Args:
                layers_obj: Photoshop 图层集合对象
            """
            for layer in layers_obj:
                # 检查是否为 LayerSet (Group)
                if getattr(layer, "TypeName", "") == "LayerSet":
                    process_layers(layer.Layers)
                    continue

                # 检查是否为文本图层
                if getattr(layer, "Kind", None) == 2:
                    try:
                        text_item = layer.TextItem
                        content = text_item.Contents
                        if content:
                            texts.append(content)
                    except (AttributeError,) + COM_ERRORS:
                        pass

        if document:
            process_layers(document.Layers)

        return texts

    def apply_text_replacements(
        self,
        document,
        replacements: Iterable[Mapping[str, object]],
    ) -> str | None:
        """
        Apply text replacements to all text layers of document (recursively).

        实现收拢到 ps_doc_ops（方法保留供调用方使用）。
        替换失败的图层会记录在 self.last_replacement_warnings 中（同时写入日志），
        不再静默吞掉。成功路径返回值结构保持不变。
        """
        warnings: List[str] = []
        self.last_replacement_warnings = warnings
        return apply_document_replacements(document, replacements, warnings)

    def export_document(
        self, document, output_path: Path, output_format: str, quality: int = 12
    ) -> None:
        """
        Export Photoshop document to image file.
        """
        if output_format.upper() == "JPG":
            options = win32com.client.Dispatch("Photoshop.JPEGSaveOptions")
            options.Quality = quality
            document.SaveAs(str(output_path), options, True)
        elif output_format.upper() == "PNG":
            options = win32com.client.Dispatch("Photoshop.PNGSaveOptions")
            document.SaveAs(str(output_path), options, True)
        else:
            raise ValueError(f"Unsupported output format: {output_format}")

    def close_document(self, document) -> None:
        """
        Close Photoshop document without saving.
        """
        document.Close(2)

    def disconnect(self) -> None:
        """
        断开与 Photoshop COM 的连接并释放资源。
        应在不再需要使用 Photoshop 时调用此方法。
        """
        if self.app is not None:
            try:
                # 释放 COM 对象引用
                self.app = None
            except Exception:
                pass
        # 强制垃圾回收以释放 COM 资源
        import gc

        gc.collect()
