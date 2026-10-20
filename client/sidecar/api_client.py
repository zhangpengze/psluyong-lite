"""与后端通信

所有请求统一走这里，方便集中处理超时、错误翻译和 API Key。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10
UPLOAD_TIMEOUT = 120  # 成品图可能十几 MB，慢网要留够时间


class ServerError(Exception):
    """服务端返回了业务错误（4xx/5xx），message 可直接展示给员工"""

    def __init__(self, message: str, status: int = 0, payload: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.payload = payload or {}


class ApiClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key}

    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = f"{self.base_url}{path}"
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        try:
            resp = requests.request(method, url, headers=self._headers, **kwargs)
        except requests.ConnectionError as e:
            raise ServerError(f"连不上服务器 {self.base_url}，请检查网络或服务是否启动") from e
        except requests.Timeout as e:
            raise ServerError("服务器响应超时，请稍后重试") from e

        try:
            payload = resp.json()
        except ValueError:
            payload = {}

        if resp.status_code >= 400 or not payload.get("ok", False):
            message = payload.get("error") or f"服务器返回 {resp.status_code}"
            raise ServerError(message, resp.status_code, payload)

        return payload

    # ========== 记录提交 ==========
    def submit_record(self, fields: dict, image_path: Path | None) -> dict:
        data = {k: ("" if v is None else str(v)) for k, v in fields.items()}
        files = None
        handle = None
        try:
            if image_path and image_path.exists():
                handle = open(image_path, "rb")
                files = {"image": (image_path.name, handle, "application/octet-stream")}
            payload = self._request(
                "POST", "/api/v1/records", data=data, files=files, timeout=UPLOAD_TIMEOUT
            )
            return payload.get("data", {})
        finally:
            if handle:
                handle.close()

    def health(self) -> bool:
        try:
            requests.get(f"{self.base_url}/health", timeout=3)
            return True
        except requests.RequestException:
            return False

    # ========== 历史 ==========
    def list_records(self, employee: str, size: int = 50) -> list[dict]:
        params = {"size": size, "employee": employee} if employee else {"size": size}
        return self._request("GET", "/api/v1/records", params=params).get("items", [])

    def fetch_image(self, record_id: int, thumb: bool = True) -> bytes:
        """浏览器里 <img> 带不了 API Key，所以由 sidecar 代取再转出去"""
        url = f"{self.base_url}/api/v1/records/{record_id}/image"
        params = {"thumb": "1"} if thumb else {}
        try:
            resp = requests.get(url, headers=self._headers, params=params, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as e:
            raise ServerError(f"取图失败: {e}") from e
        if resp.status_code >= 400:
            raise ServerError("图片不存在", resp.status_code)
        return resp.content
