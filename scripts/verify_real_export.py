"""真机验证：走 sidecar /export 真实调用 Photoshop，核对成品文件名是否符合命名规则。

用法：
    python scripts/verify_real_export.py [作者] [标题]

依赖本机 sidecar 已在 127.0.0.1:8765 运行、Photoshop 可被 COM 拉起。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8765"


def call(path: str, payload: dict | None = None, method: str | None = None, timeout: float = 30):
    url = f"{BASE}{path}"
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method or ("POST" if data else "GET"))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    author = sys.argv[1] if len(sys.argv) > 1 else "李四"
    title = sys.argv[2] if len(sys.argv) > 2 else "命名规则真机验证"

    status = call("/status")
    cfg = status["config"]
    print(f"[env] version={status.get('version')} com={status.get('com_available')} busy={status.get('busy')}")
    print(f"[env] filename_rule={cfg.get('filename_rule')} output_dir={cfg.get('output_dir')}")

    tpls = call("/templates")
    items = tpls.get("items") or []
    if not items:
        print("[fail] 本机没有模板，无法验证")
        return 2
    tpl_id = cfg.get("last_template_id") or items[0]["id"]
    if not any(t["id"] == tpl_id for t in items):
        tpl_id = items[0]["id"]
    print(f"[env] template={tpl_id}")

    started = time.time()
    start = call(
        "/export",
        {"template_id": tpl_id, "author": author, "title": title, "partner_name": ""},
        timeout=60,
    )
    if not start.get("ok"):
        print(f"[fail] 启动失败：{start}")
        return 3
    task_id = start["task_id"]
    print(f"[run] task_id={task_id}")

    final = None
    while True:
        s = call(f"/export/status?task_id={task_id}")
        if s.get("finished"):
            final = s
            break
        if time.time() - started > 240:
            print("[fail] 超时 240s")
            return 4
        time.sleep(2)

    took = time.time() - started
    print(f"[run] finished in {took:.1f}s")
    print(json.dumps({k: final.get(k) for k in ("ok", "image_name", "image_path", "uploaded", "error", "warnings")},
                     ensure_ascii=False, indent=2))

    if not final.get("ok"):
        return 5

    name = final.get("image_name") or ""
    rule = cfg.get("filename_rule")
    if rule == "author":
        ok = name.startswith(author) and title not in name
    else:
        ok = name.startswith(author) and title in name
    print(f"[check] rule={rule} name={name} -> {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 6


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as e:  # pragma: no cover
        print(f"[fail] HTTP {e.code}: {e.read().decode('utf-8', 'replace')}")
        raise SystemExit(7)
