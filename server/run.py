"""启动入口（从仓库根目录运行）

开发：python -m server.run
生产：python -m server.run  或  waitress-serve --port=8080 server.wsgi:app
"""

from __future__ import annotations

import logging
import os

from server.app import create_app
from server.app.config import Config

app = create_app()
logger = logging.getLogger(__name__)


def maybe_start_scheduler() -> None:
    if not Config.ENABLE_SCHEDULER:
        logger.info("日报调度器已禁用（ENABLE_SCHEDULER=0）")
        return
    from bot.scheduler import start_scheduler

    start_scheduler(app)


if __name__ == "__main__":
    maybe_start_scheduler()

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))

    if os.getenv("FLASK_DEBUG", "0") == "1":
        app.run(host=host, port=port, debug=True)
    else:
        from waitress import serve

        logger.info("服务启动 http://%s:%s", host, port)
        serve(app, host=host, port=port, threads=8)
