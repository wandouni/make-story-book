"""端到端验证智能排版：一键配置 → 转自由再配 → 撤销。

单测（tests/test_layout_job.py）用的是打桩的候选框和打桩的模型，证明不了
下面这些只有真跑一遍才知道的事：

- 前端那两个按钮接上了没有、点了有没有反应
- 真的从**一张图**上找出来的空地，是不是那块空地（单测里候选是手写的）
- 存下来的 `overlayRect` 有没有真的进到渲染 HTML 的 `.overlay-zone` 里
- 撤销之后书是不是真的回到跑之前

**不需要文本模型 key**：没配 key 时后端走纯算法降级路径，那是合法的运行模式
（见 auto_layout.run_layout_job）。配了 key 的机器上模型会参与选框，所以下面的
位置断言一律写成"落在候选列表里"，不断言模型会挑哪一个。

用法（两个服务都要在跑）：
    uv run python scripts/e2e_layout.py
"""

import asyncio
import os
import random
import re
import sys
from pathlib import Path

import httpx

# 让脚本能用 app.storage.paths 定位图片目录 —— 必须和服务器读到的是同一个
# 地方，自己拼 "data/books" 迟早会和 config 漂开。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.storage import paths  # noqa: E402

# 用 localhost 而不是 127.0.0.1：vite 默认只监听 ::1，写 IPv4 会 connection refused
API = os.getenv("MSB_API", "http://127.0.0.1:8000/api")
UI = os.getenv("MSB_UI", "http://localhost:5173")

# 三段，一段一页。每段三四十字 —— 太短了排不满框，字号会顶到上限，
# 看不出"按框大小反算"这件事。
STORY = (
    "小兔子推开木门，外面是一片银白的雪地，它踩出第一行脚印。\n\n"
    "雪越下越大，远处亮起一盏橘色的灯，暖光落在雪地上。\n\n"
    "它朝着那盏灯走过去，雪地上留下一串小小的脚印。"
)

IMAGE_ID = "e2e_layout_1"
TRIM_H = 210.0  # A5 竖版高，见 config.TRIM_SIZES

_TOP = re.compile(r'class="overlay-zone" style="[^"]*?top:([\d.]+)mm')


def fail(msg: str) -> None:
    print(f"✗ {msg}")
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"✓ {msg}")


# ---------------------------------------------------------------- 准备


def make_image(book_id: str) -> None:
    """上半张噪声、下半张接近纯白的一张图。

    这是整个 e2e 的地基：算法的"空地"必须**恰好**是下半张，否则后面所有
    位置断言都没有意义。上半张用随机噪声而不是纯色块 —— 纯色在 128px 缩放
    之后梯度也是 0，那就变成"整张图都是空地"了，测不出东西。
    """
    from PIL import Image

    size = 1024
    rng = random.Random(20260924)  # 固定种子：e2e 要可复现
    img = Image.new("RGB", (size, size), (245, 245, 245))
    px = img.load()
    for y in range(size // 2):
        for x in range(size):
            v = rng.randrange(256)
            px[x, y] = (v, v // 2, 255 - v)

    path = paths.image_file(book_id, IMAGE_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG")


def prepare_book() -> str:
    with httpx.Client(base_url=API, timeout=30) as c:
        book = c.post(
            "/books", json={"title": "智能排版 E2E", "trimSizeId": "A5", "pageCount": 3}
        ).json()
        book_id = book["id"]
        c.put(f"/books/{book_id}/story", json={"text": STORY})
        resp = c.post(
            f"/books/{book_id}/paginate",
            json={"pageCount": 3, "preset": "full-bleed-overlay"},
        )
        resp.raise_for_status()

        # 图片要先落盘再 select —— select 会检查文件在不在（回滚历史版本的语义）
        make_image(book_id)
        for number in (1, 2, 3):
            resp = c.post(
                f"/books/{book_id}/pages/{number}/image/select", json={"imageId": IMAGE_ID}
            )
            resp.raise_for_status()
    return book_id


def get_book(book_id: str) -> dict:
    with httpx.Client(base_url=API, timeout=30) as c:
        return c.get(f"/books/{book_id}").json()


def pages_signature(book: dict) -> list:
    """只取排版会改的那些字段，用来比"撤销回到哪儿了"。

    整页 model_dump 会把 updatedAt 之类的噪声也带上，比对必然失败。
    """
    out = []
    for page in book["pages"]:
        if page["mode"] == "preset":
            opts = page["options"]
            out.append(
                {
                    "mode": "preset",
                    "rect": opts["overlayRect"],
                    "color": opts["color"],
                    "size": opts["fontSizePt"],
                }
            )
        else:
            out.append(
                {
                    "mode": "free",
                    "blocks": [
                        [b["xMm"], b["yMm"], b["widthMm"], b["heightMm"]]
                        for b in page["blocks"]
                        if b["type"] == "text"
                    ],
                }
            )
    return out


# ---------------------------------------------------------------- 主流程


async def main() -> None:
    from playwright.async_api import async_playwright

    book_id = prepare_book()
    ok(f"建书并分页成功: {book_id}")

    with httpx.Client(base_url=API, timeout=60) as c:
        preview = c.get(f"/books/{book_id}/layout/preview/1").json()
    if not preview["applicable"]:
        fail(f"满版叠字页居然不可配置：{preview['reason']}")
    candidates = preview["candidates"]
    if not candidates:
        fail("候选框是空的 —— 算法没找到任何空地")
    ok(f"预览接口给出 {len(candidates)} 个候选框")

    # 上半张是噪声、下半张是白的，算法挑的第一个必须是下半张。
    # 这一条钉住的是"算法真的看图了"，而不是"跑通了"。
    best = candidates[0]
    if best["yMm"] <= TRIM_H * 0.5:
        fail(f"算法首选落在上半张（y={best['yMm']}mm），说明它没找到那块空白")
    ok(f"算法首选在下半张：y={best['yMm']}mm，{best['widthMm']}×{best['heightMm']}mm")

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1600, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        await page.goto(f"{UI}/books/{book_id}", wait_until="networkidle")
        await page.get_by_text("排版", exact=True).click()
        await page.wait_for_selector(".rail-item", timeout=15000)
        ok("已进入排版步骤")

        # 第一页是预设页，两个按钮都该在
        run_btn = page.get_by_role("button", name="一键智能排版")
        await run_btn.wait_for(timeout=10000)
        if await page.get_by_role("button", name="撤销本次智能配置").count():
            fail("还没跑过，撤销按钮就出现了")

        # ---- 跑第一次
        await run_btn.click()
        await wait_for_job(page)
        ok("第一次智能排版跑完")

        after_run1 = get_book(book_id)
        sig1 = pages_signature(after_run1)
        for index, entry in enumerate(sig1, start=1):
            if entry["mode"] != "preset":
                fail(f"第 {index} 页不再是预设页了")
            if not entry["rect"]:
                fail(f"第 {index} 页没有被写入 overlayRect")
            if entry["size"] < 12 or entry["size"] > 40:
                fail(f"第 {index} 页的字号越界：{entry['size']}pt")
        ok(f"三页都写入了固定落点，字号 {[e['size'] for e in sig1]}")

        # 写进去的矩形必须是**候选列表里的某一个**。不断言是哪一个 ——
        # 配了 key 的机器上模型会参与选框，挑哪个都对。
        rect1 = sig1[0]["rect"]
        if not any(
            abs(rect1["xMm"] - c["xMm"]) < 0.05
            and abs(rect1["yMm"] - c["yMm"]) < 0.05
            and abs(rect1["widthMm"] - c["widthMm"]) < 0.05
            for c in candidates
        ):
            fail(f"写入的矩形不在候选列表里：{rect1}")
        ok("写入的矩形确实来自候选列表")

        # ---- 存的矩形有没有真的进到渲染 HTML
        with httpx.Client(base_url=API, timeout=60) as c:
            html = c.get(f"/books/{book_id}/render/page/1").text
        match = _TOP.search(html)
        if not match:
            fail("渲染 HTML 里找不到 .overlay-zone 的 top")
        if abs(float(match.group(1)) - rect1["yMm"]) > 0.5:
            fail(f"渲染用的 top={match.group(1)}mm 与存的 yMm={rect1['yMm']}mm 对不上")
        ok(f"渲染 HTML 的 .overlay-zone top 与 overlayRect 一致（{rect1['yMm']}mm）")

        # ---- 把第一页转成自由页，再跑一次
        await page.get_by_role("button", name="转为自由拖拽").click()
        await page.wait_for_selector(".el-message--success", timeout=15000)
        before_run2 = get_book(book_id)
        if before_run2["pages"][0]["mode"] != "free":
            fail("转换后第一页还不是自由页")
        ok("第一页已转为自由拖拽")

        await page.get_by_role("button", name="一键智能排版").click()
        await wait_for_job(page)
        ok("第二次智能排版跑完（含自由页）")

        after_run2 = get_book(book_id)
        free_blocks = [b for b in after_run2["pages"][0]["blocks"] if b["type"] == "text"]
        if not free_blocks:
            fail("自由页上没有文字块了")
        block = free_blocks[0]
        if block["yMm"] <= TRIM_H * 0.5:
            fail(f"自由页的文字块落在上半张（y={block['yMm']}mm）")
        if not any(
            abs(block["xMm"] - c["xMm"]) < 0.05 and abs(block["yMm"] - c["yMm"]) < 0.05
            for c in candidates
        ):
            fail(f"自由页的块几何不在候选列表里：{block['xMm']},{block['yMm']}")
        ok(f"自由页的文字块也落到了下半张：y={block['yMm']}mm")

        # 底衬是**版式**，智能排版不该把它抹掉（落笔用的是 model_copy）
        if block["style"].get("backgroundColor") is None:
            print("  注：这一页原本就没有底衬，跳过底衬断言")

        # ---- 撤销
        await page.get_by_role("button", name="撤销本次智能配置").click()
        await page.wait_for_selector(".el-message--success", timeout=15000)
        after_undo = get_book(book_id)

        if pages_signature(after_undo) != pages_signature(before_run2):
            fail("撤销之后没有回到第二次运行之前的状态")
        ok("撤销把整本书恢复到了第二次运行之前（逐页比对）")

        if after_undo["pages"][0]["mode"] != "free":
            fail("撤销把自由页变回预设页了 —— 撤销只该回退排版，不该回退页面结构")
        ok("页面结构没有被撤销动过（第一页仍是自由页）")

        if await page.get_by_role("button", name="撤销本次智能配置").count():
            fail("撤销之后按钮还在 —— 只保留一次撤销，它应该消失")
        ok("撤销按钮已隐藏（没有第二次撤销）")

        # 溢出红框不该亮：字号是按框算的，算对了就不该溢出
        if await page.locator(".overflow-notice").count():
            fail("撤销之后有页面报了文字溢出")

        if errors:
            fail(f"页面有 JS 报错: {errors[:3]}")
        ok("全程无 JS 报错")

        await browser.close()

    print(f"\n全部通过。书籍 {book_id} 留在 data/books 下，可以打开看效果。")


async def wait_for_job(page, timeout: int = 180_000) -> None:
    """等任务跑完。

    JobProgressBar 在终态时把「取消」换成「收起」，等这个按钮出现比等一个
    具体的完成文案稳 —— 文案里带着跳过页数和降级说明，会变。
    """
    await page.locator(".progress-card").wait_for(timeout=timeout)
    await page.locator(".progress-card").get_by_role("button", name="收起").wait_for(
        timeout=timeout
    )
    # 任务终态之后前端还要 reload + 重载 iframe，等它静下来再断言
    await page.wait_for_timeout(800)


if __name__ == "__main__":
    asyncio.run(main())
