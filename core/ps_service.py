"""Photoshop 自动化服务

封装 COM 操作：连接、打开 PSD、文本替换、导出成品。
逻辑移植自 psluyong，去掉 UI 接口依赖，保留全部加固点：COM 错误 → 人话翻译
（HRESULT 表 + 关键字规则）、零匹配与必填占位符治理、导出后校验真实落盘、
字体可用性 / 文本超出画布告警。

文本替换的流程在 ps_replace_flow、导出前体检在 ps_preflight、连接重试在
ps_retry——这里只做装配与错误翻译，避免单文件越过 300 行的可维护上限。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .ps_com_errors import format_com_error
from .ps_connector import COM_AVAILABLE, PSConnector
from .ps_preflight import document_info, prepare_for_export
from .ps_replace_flow import apply_replacements

logger = logging.getLogger(__name__)

if COM_AVAILABLE:  # pragma: no branch
    import pywintypes
else:  # pragma: no cover - 非 Windows
    pywintypes = None  # type: ignore[assignment]

# psConstants.psRGB。模板是 CMYK 时导出 JPG 会偏色，导出前统一转过来
PS_MODE_RGB = 3


class PSService:
    """Photoshop 服务（进程内单例）"""

    _instance: "PSService | None" = None

    def __new__(cls) -> "PSService":
        if cls._instance is None:
            inst = super().__new__(cls)
            inst._connector = PSConnector()
            inst._current_document = None
            inst._ps_path = ""
            inst.last_replacement_warnings: List[str] = []
            cls._instance = inst
        return cls._instance

    # ========== 连接管理 ==========
    def connect(self, ps_path: str = "") -> Tuple[bool, str]:
        """连接 Photoshop，返回 (是否成功, 错误信息)"""
        try:
            self._ps_path = ps_path
            self._connector.connect(ps_path)
            return True, ""
        except FileNotFoundError as e:
            return False, f"Photoshop 路径不存在: {e}"
        except Exception as e:
            if pywintypes is not None and isinstance(e, pywintypes.com_error):
                msg = format_com_error(e)
                logger.error("连接 Photoshop COM 错误: %s", msg)
                return False, msg
            logger.error("连接 Photoshop 失败: %s", e)
            return False, f"无法连接到 Photoshop: {e}"

    def _format_com_error(self, error: Any) -> str:
        """COM 错误 → 人话（委托 ps_com_errors）"""
        return format_com_error(error)

    def is_connected(self) -> bool:
        return self._connector.app is not None

    def disconnect(self) -> None:
        if self._current_document:
            try:
                self._connector.close_document(self._current_document)
            except Exception as e:  # noqa: BLE001 - 断开链路不允许抛错
                logger.warning("断开时关闭文档失败（PS 里可能还开着）: %s", e)
            self._current_document = None
        self._connector.app = None

    # ========== 文档操作 ==========
    def open_document(self, template_path: Path) -> Tuple[bool, str]:
        try:
            if self._current_document:
                self._connector.close_document(self._current_document)
            self._current_document = self._connector.open_document(template_path)
            return True, ""
        except FileNotFoundError as e:
            return False, f"文档不存在: {e}"
        except Exception as e:
            if pywintypes is not None and isinstance(e, pywintypes.com_error):
                msg = format_com_error(e)
                logger.error("打开文档 COM 错误: %s", msg)
                return False, f"打开文档失败: {msg}"
            logger.error("打开文档失败: %s", e)
            return False, f"打开文档失败: {e}"

    def close_document(self, save_changes: bool = False) -> str:
        """关闭当前文档。返回空串表示关掉了，否则是失败原因。

        以前失败只 `logger.debug` 一句"（可忽略）"然后置空引用。可它一点都不可
        忽略：PS 卡在对话框/被占用时 Close() 会抛，文档就留在 PS 里，一次一张，
        攒到十几张之后员工看到的是"暂存盘不足"——一个跟他做的事毫无关系的报错，
        worker.log 里只有一行 debug。现在返回原因，由 runner 挂到**这次的结果**
        上，员工当场就能看到"PS 里还留着一个没关的模板"。
        """
        if not self._current_document:
            return ""
        try:
            self._current_document.Close(1 if save_changes else 2)
            return ""
        except Exception as e:  # noqa: BLE001 - 关闭失败不能中断上层清理
            message = f"关闭 Photoshop 文档失败：{e}"
            logger.warning("%s（这个模板的文档可能还开在 PS 里）", message)
            return message
        finally:
            # 引用照旧清空：留着它，下次 open_document 会去关一个可能已经失效的
            # COM 对象，把真正的报错盖成"关闭文档失败"。
            self._current_document = None

    def set_display_dialogs(self, mode: int = 3) -> None:
        """1=仅错误 2=全部 3=不显示"""
        self._connector.set_display_dialogs(mode)

    # ========== 文本替换 ==========
    def apply_text_replacements(
        self, replacements: List[Dict[str, Any]]
    ) -> Tuple[bool, "str | None", str]:
        """对当前文档应用替换规则。流程与三道治理见 ps_replace_flow。

        Returns:
            (是否成功, 第一个替换文本（用于文件名）, 错误信息)
        """
        if not self._current_document:
            return False, None, "没有打开的文档"
        warnings: List[str] = []
        self.last_replacement_warnings = warnings
        return apply_replacements(self._current_document, replacements, warnings)

    def get_all_text_layers(self) -> List[str]:
        """列出当前文档所有文本图层内容（用于模板占位符体检）"""
        texts: List[str] = []
        if not self._current_document:
            return texts

        def walk(layers_obj: Any) -> None:
            for layer in layers_obj:
                if getattr(layer, "TypeName", "") == "LayerSet":
                    walk(layer.Layers)
                    continue
                if getattr(layer, "Kind", None) == 2:
                    try:
                        content = layer.TextItem.Contents
                        if content:
                            texts.append(str(content))
                    except (AttributeError, TypeError) as e:
                        logger.debug("获取文本图层内容时出错: %s", e)

        walk(self._current_document.Layers)
        return texts

    # ========== 导出 ==========
    # 导出前体检的实现在 ps_preflight（只跟文档属性打交道），这里转发给 runner 用
    def document_info(self) -> Dict[str, Any]:
        return document_info(self._current_document)

    def prepare_for_export(self) -> Tuple[bool, List[str]]:
        return prepare_for_export(self._current_document)

    def export_document(
        self, output_path: Path, output_format: str = "JPG", quality: int = 12
    ) -> Tuple[bool, str]:
        if not self._current_document:
            return False, "没有打开的文档"

        try:
            import win32com.client  # 局部导入：非 Windows 环境不会走到这里

            fmt = output_format.upper()
            if fmt in ("JPG", "JPEG"):
                options = win32com.client.Dispatch("Photoshop.JPEGSaveOptions")
                options.Quality = quality
            elif fmt == "PNG":
                options = win32com.client.Dispatch("Photoshop.PNGSaveOptions")
            elif fmt == "PSD":
                options = win32com.client.Dispatch("Photoshop.PhotoshopSaveOptions")
            elif fmt in ("TIFF", "TIF"):
                options = win32com.client.Dispatch("Photoshop.TiffSaveOptions")
            else:
                return False, f"不支持的输出格式: {output_format}"

            self._current_document.SaveAs(str(output_path), options, True)

            # 假成功治理：COM SaveAs 返回不代表文件真的落盘了
            out_file = Path(str(output_path))
            if not out_file.exists() or out_file.stat().st_size <= 0:
                logger.error("导出文件未真实落盘: %s", output_path)
                return False, f"导出文件未生成（目标: {output_path}），请检查输出目录与磁盘空间后重试"

            return True, ""

        except FileNotFoundError as e:
            return False, f"输出路径不存在: {e}"
        except Exception as e:  # noqa: BLE001 - 兜底翻译 COM 错误
            if pywintypes is not None and isinstance(e, pywintypes.com_error):
                msg = format_com_error(e)
                logger.error("导出文档 COM 错误: %s", msg)
                # 带上目标路径：「文件不存在」这类通用翻译不说是哪个文件，
                # 员工永远想不到是设置里的输出目录填错了
                return False, f"导出失败: {msg}（目标: {output_path}）"
            logger.error("导出文档失败: %s", e)
            return False, f"导出失败: {e}（目标: {output_path}）"


_ps_service: "PSService | None" = None


def get_ps_service() -> PSService:
    """获取 Photoshop 服务单例"""
    global _ps_service
    if _ps_service is None:
        _ps_service = PSService()
    return _ps_service
