"""PS 模板批量生成引擎对外入口"""

from .ps_service import PSService, get_ps_service
from .runner import (
    DEFAULT_FILENAME_RULE,
    FILENAME_RULE_AUTHOR,
    FILENAME_RULE_AUTHOR_TITLE,
    FILENAME_RULES,
    ExportRequest,
    ExportResult,
    build_output_path,
    run_export,
    safe_filename,
)

__all__ = [
    "PSService",
    "get_ps_service",
    "ExportRequest",
    "ExportResult",
    "run_export",
    "build_output_path",
    "safe_filename",
    "FILENAME_RULES",
    "FILENAME_RULE_AUTHOR",
    "FILENAME_RULE_AUTHOR_TITLE",
    "DEFAULT_FILENAME_RULE",
]
