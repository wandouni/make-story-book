"""端到端验证导出步骤（Phase 7）。

单测（tests/test_export.py）证明了后端能出正确的 PDF，但证明不了前端那条路
是通的 —— 步骤有没有接上、按钮点得动、报告里的 dpi 有没有渲染出来、下载
链接指向的文件是不是真的存在。这些只有真开浏览器点一遍才知道。

用法（两个服务都要在跑）：
    uv run python scripts/e2e_export.py
"""

import asyncio
import os
import sys

import httpx

# 用 localhost 而不是 127.0.0.1：vite 默认只监听 ::1，写 IPv4 会 connection refused
API = os.getenv("MSB_API", "http://127.0.0.1:8000/api")
UI = os.getenv("MSB_UI", "http://localhost:5173")
STORY = (
    "小兔子推开木门，外面是一片银白的雪地。\n\n"
    "它踩出第一行脚印，回头看了看，又继续往前走。\n\n"
    "雪越下越大，远处亮起一盏橘色的灯。"
)


def fail(msg: str) -> None:
    print(f"✗ {msg}")
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"✓ {msg}")


def prepare_book() -> str:
    """建书 + 写故事 + 分页。不配图 —— 这一版测的是导出链路本身，
    插图那条路已经在 tests/test_export.py 里覆盖了。"""
    with httpx.Client(base_url=API, timeout=30) as c:
        book = c.post(
            "/books", json={"title": "导出 E2E", "trimSizeId": "A5", "pageCount": 3}
        ).json()
        book_id = book["id"]
        c.put(f"/books/{book_id}/story", json={"text": STORY})
        resp = c.post(f"/books/{book_id}/paginate", json={"pageCount": 3, "preset": "image-top"})
        resp.raise_for_status()
    return book_id


async def main() -> None:
    from playwright.async_api import async_playwright

    book_id = prepare_book()
    ok(f"建书并分页成功: {book_id}")

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1600, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        await page.goto(f"{UI}/books/{book_id}", wait_until="networkidle")

        # ---- 进导出步骤
        await page.get_by_text("导出", exact=True).click()
        await page.wait_for_selector(".card", timeout=15000)
        if await page.locator(".card").count() != 2:
            fail("应该有预览版和印刷版两张卡片")
        ok("导出步骤已打开，两张卡片就位")

        # 规格要在**导出之前**就写清楚
        specs = await page.locator(".specs").first.inner_text()
        if "148.0 × 210.0 mm" not in specs:
            fail(f"预览版尺寸不对：{specs}")
        print(f"  预览版规格: {' / '.join(specs.split())}")

        print_cards = page.locator(".card").nth(1)
        print_specs = " ".join((await print_cards.locator(".specs").inner_text()).split())
        if "154.0 × 216.0 mm" not in print_specs:
            fail(f"印刷版尺寸没算出出血：{print_specs}")
        if "300 dpi" not in print_specs:
            fail(f"印刷版 dpi 没从后端取到：{print_specs}")
        print(f"  印刷版规格: {print_specs}")

        # ---- 导出预览版
        await page.locator(".card").first.get_by_role("button", name="开始导出").click()
        await page.locator(".card").first.locator(".result").wait_for(timeout=120000)
        ok("预览版导出完成，结果区已出现")

        preview_file = await page.locator(".card").first.locator(".file").inner_text()
        print(f"  预览版产物: {' '.join(preview_file.split())}")

        # ---- 导出印刷版
        await print_cards.get_by_role("button", name="开始导出").click()
        await print_cards.locator(".result").wait_for(timeout=120000)
        ok("印刷版导出完成")

        # 报告里的插图表：这一版没配图，不该出现
        if await print_cards.locator(".dpi").count():
            fail("没配图却渲染出了 dpi 表格")

        # ---- 下载链接指向的文件真的存在，且是真的 PDF
        href = await print_cards.locator("a.download").get_attribute("href")
        if not href:
            fail("下载链接没有 href")
        with httpx.Client(base_url=UI, timeout=60) as c:
            resp = c.get(href)
        if resp.status_code != 200:
            fail(f"下载链接取不到文件：{href} → {resp.status_code}")
        if not resp.content.startswith(b"%PDF"):
            fail("下载下来的不是 PDF")
        ok(f"下载链接可用：{href.split('?')[0]}（{len(resp.content) // 1024} KB）")

        # 预览版和印刷版必须是**两个不同的文件**
        preview_href = await page.locator(".card").first.locator("a.download").get_attribute("href")
        if preview_href == href:
            fail("两个版本指向同一个文件")
        with httpx.Client(base_url=UI, timeout=60) as c:
            preview_bytes = c.get(preview_href).content
        if preview_bytes == resp.content:
            fail("预览版与印刷版的 PDF 内容完全一样 —— 出血没生效")
        ok("预览版与印刷版是两个不同的文件")

        if errors:
            fail(f"页面有 JS 报错: {errors[:3]}")
        ok("全程无 JS 报错")

        await browser.close()

    print("\n全部通过。")


if __name__ == "__main__":
    asyncio.run(main())
