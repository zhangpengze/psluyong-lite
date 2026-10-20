"""PyInstaller 打包入口（sidecar）

单独一个入口文件，避免把 client/__init__.py 当成启动点导致相对导入失效。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 打包后 sys.path 里没有仓库根，显式补上，core 才 import 得到
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from client.sidecar.server import main  # noqa: E402

if __name__ == "__main__":
    main()
