"""
Photoshop 自动化服务

封装 COM 操作：连接、打开 PSD、文本替换、导生成片。
逻辑移植自 psluyong，去掉 UI 接口依赖，保留全部加固点：
  - COM 错误 → 人话翻译（HRESULT 表 + 关键字规则）
  - 零匹配治理：没有任何图层被替换时不返回假成功
  - 导出后校验文件真实落盘
  - 字体可用性 / 文本超出画布告警
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .ps_com_errors import format_com_error
from .ps_connector import COM_AVAILABLE, PSConnector
from .ps_text import check_text_overflow, match_text, replace_text, safe_layer_name

logger = logging.getLogger(__name__)

if COM_AVAILABLE:  # pragma: no branch
    import pywintypes
else:  # pragma: no cover - 非 Windows
    pywintypes = None  # type: ignore[assignment]


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
                msg = self._format_com_error(e)
                logger.error("连接 Photoshop COM 错误: %s", msg)
                return False, msg
            logger.error("连接 Photoshop 失败: %s", e)
            return False, f"无法连接到 Photoshop: {e}"

    def _format_com_error(self, error: Any) -> str:
        """COM 错误 → 人话（委托 ps_com_errors，方法保留供调用方使用）"""
        return format_com_error(error)

    def is_connected(self) -> bool:
        return self._connector.app is not None

    def disconnect(self) -> None:
        if self._current_document:
            try:
                self._connector.close_document(self._current_document)
            except Exception as e:  # noqa: BLE001 - 断开链路不允许抛错
                logger.debug("关闭文档时出错（可忽略）: %s", e)
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
                msg = self._format_com_error(e)
                logger.error("打开文档 COM 错误: %s", msg)
                return False, f"打开文档失败: {msg}"
            logger.error("打开文档失败: %s", e)
            return False, f"打开文档失败: {e}"

    def close_document(self, save_changes: bool = False) -> None:
        if not self._current_document:
            return
        try:
            self._current_document.Close(1 if save_changes else 2)
        except Exception as e:  # noqa: BLE001 - 关闭失败不应中断上层清理
            logger.debug("关闭文档时出错（可忽略）: %s", e)
        finally:
            self._current_document = None

    def set_display_dialogs(self, mode: int = 3) -> None:
        """1=仅错误 2=全部 3=不显示"""
        self._connector.set_display_dialogs(mode)

    # ========== 文本替换 ==========
    def apply_text_replacements(
        self, replacements: List[Dict[str, Any]]
    ) -> Tuple[bool, "str | None", str]:
        """
        对当前文档所有文本图层应用替换规则。

        Returns:
            (是否成功, 第一个替换文本（用于文件名）, 错误信息)
        """
        if not self._current_document:
            return False, None, "没有打开的文档"

        first_replacement_text: "str | None" = None
        warnings: List[str] = []
        self.last_replacement_warnings = warnings
        changed_layer_count = 0
        matched_rules: set[int] = set()

        try:
            try:
                available_fonts = {
                    str(font.PostScriptName) for font in self._current_document.Application.Fonts
                }
            except Exception:  # noqa: BLE001 - 取不到字体表不阻断生成，只降级为告警
                available_fonts = None
                warnings.append("字体可用性未验证，请打开成品图确认")

            def process_layers(layers_obj: Any) -> None:
                nonlocal first_replacement_text, changed_layer_count

                for layer in layers_obj:
                    if getattr(layer, "TypeName", "") == "LayerSet":
                        process_layers(layer.Layers)
                        continue
                    # Kind == 2 表示文本图层
                    if getattr(layer, "Kind", None) != 2:
                        continue

                    try:
                        text_item = layer.TextItem
                        original = str(text_item.Contents)
                        new_text = original

                        for rule_index, cfg in enumerate(replacements):
                            find = str(cfg.get("find", ""))
                            replace = str(cfg.get("replace", ""))
                            use_regex = bool(cfg.get("regex", False))
                            match_mode = str(cfg.get("match_mode", "精确"))
                            if not find:
                                continue
                            try:
                                if match_text(new_text, find, use_regex, match_mode):
                                    matched_rules.add(rule_index)
                                    new_text = replace_text(
                                        new_text, find, replace, use_regex, match_mode
                                    )
                                    if first_replacement_text is None and replace:
                                        first_replacement_text = replace
                            except re.error as e:
                                layer_name = safe_layer_name(layer)
                                warning = (
                                    f"图层「{layer_name}」正则规则无效，已跳过: {find!r} ({e})"
                                )
                                warnings.append(warning)
                                logger.warning(warning)

                        if new_text != original:
                            text_item.Contents = new_text
                            changed_layer_count += 1
                            check_text_overflow(
                                self._current_document, layer, text_item, available_fonts, warnings
                            )

                    except (AttributeError, TypeError):
                        continue

            process_layers(self._current_document.Layers)

            for index, cfg in enumerate(replacements):
                if cfg.get("find") and index not in matched_rules:
                    warnings.append(f"第 {index + 1} 条替换规则未命中，请核对成品")

            # 零匹配治理：没有任何图层被替换时不能返回成功，
            # 否则会导出一张与模板一模一样的图，员工以为做完了。
            if changed_layer_count == 0:
                logger.warning("文本替换零匹配：未找到任何可替换的文本")
                return (
                    False,
                    None,
                    "未找到任何可替换的文本，请检查模板占位符与查找规则是否一致",
                )

            return True, first_replacement_text, ""

        except (AttributeError, RuntimeError) as e:
            logger.error("文本替换失败: %s", e)
            return False, None, f"文本替换失败: {e}"
        except Exception as e:  # noqa: BLE001 - 兜底翻译 COM 错误
            if pywintypes is not None and isinstance(e, pywintypes.com_error):
                msg = self._format_com_error(e)
                logger.error("文本替换 COM 错误: %s", msg)
                return False, None, f"文本替换失败: {msg}"
            logger.error("文本替换未知错误: %s", e)
            return False, None, f"文本替换失败: {e}"

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
                msg = self._format_com_error(e)
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
