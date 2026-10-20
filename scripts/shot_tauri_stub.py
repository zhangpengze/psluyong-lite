"""临时脚本：注入 __TAURI__ 桩，验证前端窗口三件套的接线逻辑。

桩会把所有 invoke 调用记录到 window.__calls，这样不需要真 Rust 外壳
就能验证：启动恢复位置/置顶/贴边、按钮切换、配置落盘。
"""

from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path("E:/psluyong-lite/.verify")

STUB = """
window.__calls = [];
window.__TAURI__ = {
  core: {
    invoke: (cmd, args) => {
      window.__calls.push({ cmd, args });
      if (cmd === 'get_window_rect') return Promise.resolve([100, 200, 520, 760]);
      return Promise.resolve(null);
    },
  },
  event: { listen: (ev, cb) => Promise.resolve(() => {}) },
};
"""


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_context(viewport={"width": 1100, "height": 820}).new_page()
        page.add_init_script(STUB)
        page.goto("http://127.0.0.1:5173", wait_until="networkidle")
        page.wait_for_timeout(1500)

        calls = page.evaluate("window.__calls")
        print("--- 启动时自动调用（应含 restore_window_rect / applyWindowPrefs）---")
        print(json.dumps(calls, ensure_ascii=False, indent=1))

        page.click("button.winbtn >> nth=0")  # 置顶
        page.click("button.winbtn >> nth=1")  # 贴边
        page.wait_for_timeout(600)
        calls = page.evaluate("window.__calls")
        print("--- 点击两个按钮后 ---")
        print(json.dumps(calls[-6:], ensure_ascii=False, indent=1))

        page.screenshot(path=str(OUT / "topbar.png"), clip={"x": 0, "y": 0, "width": 1100, "height": 70})
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
