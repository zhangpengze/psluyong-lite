"""灌入演示数据，方便第一次打开后台就能看到完整界面

    python scripts/seed_demo.py --url http://127.0.0.1:8080 --password 你的密码

只在开发/演示环境用。重复执行会因唯一索引跳过重复记录。
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

EMPLOYEES = ["张三", "李四", "王五"]
PARTNERS = ["示例合作伙伴", "华东出版社", "南方文化"]
TEMPLATES = ["录用通知模板", "封面模板"]
AUTHORS = ["陈明", "刘洋", "赵敏", "周涛", "孙悦", "吴磊", "郑爽", "冯刚"]
TITLES = [
    "基于深度学习的图像分割方法研究",
    "面向边缘计算的任务调度优化",
    "多模态大模型的知识蒸馏",
    "城市轨道交通客流预测",
    "碳交易市场价格波动分析",
    "中小企业数字化转型路径",
]


def make_image(seed: int) -> bytes:
    """画一张像"录用通知"的图，而不是纯色块，便于肉眼判断缩略图是否正常"""
    width, height = 900, 600
    img = Image.new("RGB", (width, height), (250, 249, 245))
    draw = ImageDraw.Draw(img)

    draw.rectangle([0, 0, width, 84], fill=(37, 99, 235))
    draw.rectangle([40, 118, width - 40, height - 40], outline=(200, 205, 215), width=2)

    font_big = font_small = None
    for path in ("C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"):
        if Path(path).exists():
            try:
                font_big = ImageFont.truetype(path, 34)
                font_small = ImageFont.truetype(path, 20)
                break
            except OSError:
                continue

    if font_big and font_small:
        draw.text((60, 22), "录 用 通 知", font=font_big, fill=(255, 255, 255))
        draw.text((90, 170), f"作者：{AUTHORS[seed % len(AUTHORS)]}", font=font_small, fill=(40, 48, 64))
        draw.text((90, 215), f"标题：{TITLES[seed % len(TITLES)]}", font=font_small, fill=(40, 48, 64))
        draw.text((90, 470), "《示例期刊》编辑部", font=font_small, fill=(90, 98, 112))
        draw.text((90, 505), f"2026 年 {10 + seed % 3} 月", font=font_small, fill=(90, 98, 112))

    buf = BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", required=True)
    parser.add_argument("--days", type=int, default=7, help="灌几天的数据")
    parser.add_argument("--api-key", default="", help="不传则交互式输入")
    args = parser.parse_args()

    base = args.url.rstrip("/")
    session = requests.Session()

    resp = session.post(f"{base}/login", data={"username": args.username, "password": args.password})
    if resp.status_code not in (200, 302) or "/login" in resp.url:
        print("登录失败，请检查密码", file=sys.stderr)
        return 1
    print("登录成功")

    api_key = args.api_key or input("请粘贴 .env 里的 API_KEY（用于提交记录）: ").strip()
    if not api_key:
        print("未提供 API_KEY，无法灌记录", file=sys.stderr)
        return 1

    # 模板与合作伙伴名单都由员工客户端维护，服务端没有这两张表了，
    # 演示记录直接把名字带上即可。
    headers = {"X-API-Key": api_key}
    today = date.today()
    created = skipped = 0

    for offset in range(args.days):
        day = today - timedelta(days=offset)
        # 周末少一些，看起来更真实
        count = random.randint(2, 4) if day.weekday() >= 5 else random.randint(4, 8)
        for i in range(count):
            author = AUTHORS[(offset * 3 + i) % len(AUTHORS)]
            title = TITLES[(offset * 2 + i) % len(TITLES)]
            tpl = TEMPLATES[i % len(TEMPLATES)]

            fields = {
                "employee_name": EMPLOYEES[i % len(EMPLOYEES)],
                "template_id": f"{tpl}.psd",
                "template_name": tpl,
                "partner_name": PARTNERS[i % len(PARTNERS)],
                "author": author,
                "title": title,
                "work_date": day.isoformat(),
                "client_machine": f"DESKTOP-{1000 + i}",
                "ps_version": "25.0",
                "status": "success",
                "processing_ms": random.randint(4200, 15000),
                "warnings": "",
            }
            if i == 5 and offset == 0:
                fields["status"] = "failed"
                fields["error_message"] = "Photoshop 未运行，请先启动 Photoshop 再点执行"

            files = {"image": ("demo.jpg", make_image(offset + i), "image/jpeg")} if fields["status"] == "success" else None
            r = requests.post(f"{base}/api/v1/records", data=fields, files=files, headers=headers)
            if r.status_code == 201:
                created += 1
            else:
                skipped += 1

    print(f"完成：新建 {created} 条，跳过 {skipped} 条（重复或失败）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
