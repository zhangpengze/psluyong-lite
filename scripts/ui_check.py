"""界面自查：截图后台页面 + dump 客户端文案，人工核对用词是否贴合业务。

前置：后端已起（默认 http://127.0.0.1:8080），客户端前端已起（vite dev，5173）。
本机若无 Photoshop / pywin32，客户端只核对文案，不会真跑生成。

用法：
    python scripts/ui_check.py                  # 后台用 admin 登录，需交互输入密码
    python scripts/ui_check.py -u admin -p ***  # 直接给账号

产物落在 .verify/ 下，该目录已 gitignore。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

BACKEND = "http://127.0.0.1:8080"
CLIENT = "http://127.0.0.1:5173"
OUT = Path(__file__).resolve().parent.parent / ".verify"

ADMIN_PAGES = [
    ("dashboard", "/dashboard"),
    ("records", "/records"),
    ("reports", "/reports"),
]
CLIENT_TABS = [("record", "录用"), ("history", "记录"), ("settings", "设置")]


def shoot_admin(page, username: str, password: str) -> None:
    page.goto(f"{BACKEND}/login", wait_until="networkidle")
    page.screenshot(path=str(OUT / "00-login.png"))
    page.fill("input[name=username]", username)
    page.fill("input[name=password]", password)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")

    for idx, (name, path) in enumerate(ADMIN_PAGES, start=1):
        page.goto(f"{BACKEND}{path}", wait_until="networkidle")
        page.screenshot(path=str(OUT / f"{idx:02d}-{name}.png"), full_page=True)
        print(f"[后台] {path:12s} -> .verify/{idx:02d}-{name}.png")


def dump_client(page) -> None:
    page.goto(CLIENT, wait_until="networkidle")
    page.wait_for_timeout(1200)
    for idx, (safe, label) in enumerate(CLIENT_TABS):
        btn = page.get_by_role("button", name=label)
        if btn.count():
            btn.first.click()
            page.wait_for_timeout(800)
        page.screenshot(path=str(OUT / f"c{idx}-{safe}.png"), full_page=True)
        print(f"\n===== 客户端 / {label} =====")
        print(page.inner_text("body"))


def main() -> int:
    parser = argparse.ArgumentParser(description="界面自查")
    parser.add_argument("-u", "--username", default="admin")
    parser.add_argument("-p", "--password", default="")
    parser.add_argument("--skip-admin", action="store_true", help="只查客户端")
    parser.add_argument("--skip-client", action="store_true", help="只查后台")
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        # 用本机 Edge，省掉下载 chromium
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()

        if not args.skip_admin:
            password = args.password
            if not password:
                import getpass

                password = getpass.getpass(f"{args.username} 的后台密码：")
            shoot_admin(page, args.username, password)

        if not args.skip_client:
            dump_client(page)

        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
