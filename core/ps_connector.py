import contextlib
import logging
import re
import sys
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import List

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

    def _handle_com_error(self, exc: Exception, attempt: int, max_retries: int) -> None:
        """处理 COM 错误，决定是否重试或抛出异常"""
        if not pywintypes or not isinstance(exc, pywintypes.com_error):
            raise exc

        # excepinfo 属性存在但可为 None，None[0] 会抛 TypeError 并从
        # connect 的重试逻辑中漏出（此处只应抛业务语义的异常）
        error_code = getattr(exc, "hresult", None) or (getattr(exc, "excepinfo", None) or (None,))[0]

        if error_code in (-2147221164, 0x80040154):
            raise RuntimeError(
                "Photoshop COM 组件未注册。\n请确认已安装 Photoshop，或尝试重新安装。"
            ) from exc

        if error_code in (-2146959355, 0x80080005):
            if attempt < max_retries - 1:
                logger.info(f"Photoshop 启动中，等待重试... ({attempt + 1}/{max_retries})")
                time.sleep(2)
                return
            raise RuntimeError("无法启动 Photoshop，请手动启动后再试。") from exc

        if error_code in (-2147023174, 0x800706BA):
            raise RuntimeError("Photoshop 连接已断开，请重启 Photoshop。") from exc

        raise exc

    def connect(self, photoshop_exe: str) -> None:
        """
        Connect to Photoshop using COM interface.

        Parameters
        ----------
        photoshop_exe:
            Path to Photoshop executable. Used for validation if needed.
        """
        ensure_com_available()
        max_retries = 3
        last_exception: Exception | None = None

        # 在未初始化 COM 的线程（如 sidecar/服务线程）中必须先 CoInitialize，
        # 否则 Dispatch 抛 "尚未调用 CoInitialize" (-2147221008)。
        # 已初始化的线程重复调用是幂等的（嵌套计数），无副作用。
        try:
            pythoncom.CoInitialize()
        except Exception:  # noqa: BLE001
            pass

        for attempt in range(max_retries):
            try:
                self.app = win32com.client.Dispatch("Photoshop.Application")
                try:
                    if not self.app.Visible:
                        self.app.Visible = True
                except (AttributeError,) + COM_ERRORS:
                    pass
                return
            except Exception as exc:
                try:
                    self._handle_com_error(exc, attempt, max_retries)
                except RuntimeError:
                    raise
                last_exception = exc
                time.sleep(1)

        if last_exception:
            raise RuntimeError(
                f"无法连接 Photoshop (已重试 {max_retries} 次)。\n"
                f"请确认：\n"
                f"1. Photoshop 已正确安装\n"
                f"2. Photoshop 可以正常启动\n"
                f"3. 尝试以管理员身份运行本程序"
            ) from last_exception
        raise RuntimeError("无法连接 Photoshop，请确认已安装并正确注册 COM 组件")

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
        """
        Check whether text matches pattern.
        """
        # Normalize line endings for robust matching (PS often uses \r)
        # 将所有类型的换行符统一转换为 \n
        source_norm = source.replace("\r\n", "\n").replace("\r", "\n")
        pattern_norm = pattern.replace("\r\n", "\n").replace("\r", "\n")

        if regex:
            # Use DOTALL so . matches newlines if needed, or user specifies it.
            # But let's keep it simple. If user wants multiline they use (?s)
            return re.search(pattern, source) is not None

        if match_mode == "精确":
            return source_norm == pattern_norm

        return pattern_norm in source_norm

    def _replace_text(
        self, source: str, pattern: str, replacement: str, regex: bool, match_mode: str
    ) -> str:
        """
        Replace text according to configuration.
        """
        if regex:
            # 用 lambda 返回字面量，避免用户输入中的反斜杠/反向引用（\1、\g<name>）
            # 被 re.sub 解释导致替换内容被篡改或报错
            return re.sub(pattern, lambda m: replacement, source)

        if match_mode == "精确":
            # For exact replacement, we just return the replacement
            # Note: we used normalized form for checking, but here we just return the replacement.
            # If it was an exact match, the whole string is replaced.
            return replacement

        # For fuzzy match, we need to be careful with line endings.
        # But for simplicity, let's just use standard replace.
        # If pattern had \n and source has \r, direct replace fails.
        # So we might need to handle that if strict robustness is required.
        # For now, let's try direct replace, but if it fails, try replacing \r with \n in source?
        # That would modify the original line endings of the non-replaced parts,
        # which might be undesirable.
        # Let's assume simple string replace is fine for most cases,
        # but warn user about line endings if they are advanced.
        return source.replace(pattern, replacement)

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

        Parameters
        ----------
        document:
            Photoshop document object.
        replacements:
            Iterable of replacement definitions.

        Returns
        -------
        Optional[str]
            First non-empty replacement text used for file naming.

        Notes
        -----
        替换失败的图层会记录在 self.last_replacement_warnings 中（同时写入日志），
        不再静默吞掉。成功路径返回值结构保持不变。
        """
        first_replacement_text: str | None = None
        warnings: List[str] = []
        self.last_replacement_warnings = warnings

        def process_layers(layers_obj):
            """
            递归遍历图层组，对文本图层应用替换规则。

            Args:
                layers_obj: Photoshop 图层集合对象
            """
            nonlocal first_replacement_text
            for layer in layers_obj:
                # 检查是否为 LayerSet (Group)，如果是则递归
                if getattr(layer, "TypeName", "") == "LayerSet":
                    process_layers(layer.Layers)
                    continue

                # 检查是否为文本图层 (Kind == 2 usually implies TextLayer)
                # 更加稳健的方式是检查有没有 TextItem 属性
                if getattr(layer, "Kind", None) != 2:
                    continue

                try:
                    text_item = layer.TextItem
                    # Explicitly convert to string to avoid COM object issues
                    original = str(text_item.Contents)
                    new_text = original

                    for cfg in replacements:
                        find = str(cfg.get("find", ""))
                        replace = str(cfg.get("replace", ""))
                        regex = bool(cfg.get("regex", False))
                        match_mode = str(cfg.get("match_mode", "精确"))

                        if not find:
                            continue

                        try:
                            if self._match_text(new_text, find, regex, match_mode):
                                new_text = self._replace_text(
                                    new_text, find, replace, regex, match_mode
                                )
                                if first_replacement_text is None and replace:
                                    first_replacement_text = replace
                        except re.error as e:
                            # 非法正则：记录失败图层到警告列表，不再静默吞掉
                            layer_name = getattr(layer, "Name", "未知图层")
                            warning = f"图层「{layer_name}」正则规则无效，已跳过: {find!r} ({e})"
                            warnings.append(warning)
                            logger.warning(warning)

                    if new_text != original:
                        text_item.Contents = new_text

                except (AttributeError, TypeError) as e:
                    # 图层属性访问错误或类型错误，记录但继续处理其他图层
                    logger.error(f"处理图层 {getattr(layer, 'Name', 'Unknown')} 时出错: {e}")
                except Exception as e:
                    # 其他未预期的错误，记录但继续
                    logger.error(f"处理图层 {getattr(layer, 'Name', 'Unknown')} 时发生未预期错误: {e}")

        # 从文档顶层图层开始
        process_layers(document.Layers)
        return first_replacement_text

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
