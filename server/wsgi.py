"""WSGI 入口：waitress-serve --port=8080 server.wsgi:app"""

from __future__ import annotations

from server.run import app, maybe_start_scheduler

maybe_start_scheduler()

__all__ = ["app"]
