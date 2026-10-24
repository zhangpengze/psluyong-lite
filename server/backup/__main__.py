"""`python -m server.backup` 入口：等价于原 backup.py 底部的 if __name__ == "__main__"。"""

import sys

from .cli import main

sys.exit(main())
