"""Playwright 导出 PDF。

核心：**打开的是前端 iframe 指向的同一个渲染端点**，拿到的是同一份 HTML。
没有第二套模板，也就不存在"预览好看、导出跑版"这个 bug 类别。

关于"300dpi"：page.pdf() 输出的是**矢量** PDF，文字是文字不是位图，
device_scale_factor 只影响 screenshot()，对 PDF 里的文字毫无作用。
真正需要 300dpi 的只有位图插图，而它落在 render/renderer.py 生成的
图片 URL 上（按放置尺寸反算像素，由 /api/assets 重采样）。见 geometry.py。
"""

import asyncio
from pathlib import Path
from typing import Optional

from PIL import Image

from app import config
from app.export.browser import get_browser
from app.logger import get_logger
from app.models.book import Book, Page
from app.render import geometry, renderer
from app.storage import paths

logger = get_logger()

# 导出用的视口。PDF 的物理尺寸由 @page 决定，视口只影响布局宽度 ——
# 给足宽度，别让页面被挤成移动端布局。
_VIEWPORT = {"width": 1400, "height": 2000}

# page.pdf() 内存峰值高，串行执行
_export_lock = asyncio.Lock()


def output_path(book_id: str, kind: str) -> Path:
    return paths.exports_dir(book_id) / f"{kind}.pdf"


async def export_pdf(book: Book, kind: str) -> Path:
    """把整本书导成 PDF，返回文件路径。kind 为 preview 或 print。"""
    mode = "print" if kind == "print" else "preview"
    url = f"{config.SELF_BASE_URL}/api/books/{book.id}/render/all?mode={mode}"
    out = output_path(book.id, kind)
    out.parent.mkdir(parents=True, exist_ok=True)

    async with _export_lock:
        browser = await get_browser()
        context = await browser.new_context(viewport=_VIEWPORT, device_scale_factor=1)
        try:
            page = await context.new_page()
            # 用 goto 而不是 set_content：set_content 下图片/字体的相对 URL
            # 没有 base，全都取不到
            await page.goto(url, wait_until="networkidle")

            # 字体没加载完就排版，度量会按 fallback 字体算，导出必然跑版
            await page.evaluate("document.fonts.ready")
            await page.wait_for_function(
                "Array.from(document.images).every(i => i.complete && i.naturalWidth > 0)",
                timeout=60_000,
            )

            # 与预览跑的是**同一份**算法，所以两边结果必然一致
            await page.evaluate("window.fitAllText()")

            await page.emulate_media(media="print")
            await page.pdf(
                path=str(out),
                # 少了这个，@page { size } 不生效，页面会退回 Letter
                prefer_css_page_size=True,
                # 少了这个，背景色和叠字底衬全没了
                print_background=True,
                margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
            )
        finally:
            await context.close()

    logger.info("导出 %s（%s）→ %s", book.title, kind, out)
    return out


# ---------------------------------------------------------------- 导出报告


def image_box_mm(book: Book, page: Page, mode: str) -> Optional[tuple]:
    """这一页的插图会被渲染成多大；这一页根本不显示图时返回 None。

    与 renderer 用同一套算法（block_box_mm / preset_image_box_mm）——
    另算一遍迟早会出现"按 100mm 重采样、实际画在 106mm 上"。
    """
    if page.mode == "free":
        block = next((b for b in page.blocks if b.type == "image"), None)
        if block is None or not block.imageId:
            return None
        _, _, width, height = renderer.block_box_mm(block, book, mode)
        return width, height

    # 这两种版式没有图片槽，即使 page.imageId 还留着也不显示
    if page.preset in ("text-only",):
        return None

    return renderer.preset_image_box_mm(page, book, mode)


def image_fit(page: Page) -> str:
    if page.mode == "free":
        block = next((b for b in page.blocks if b.type == "image"), None)
        return block.fit if block else "cover"
    # 预设版式的图片槽都是 object-fit: cover
    return "cover"


def _image_size(book: Book, image_id: str) -> Optional[tuple]:
    try:
        file = paths.image_file(book.id, image_id)
    except ValueError:
        return None
    if not file.exists():
        return None

    try:
        with Image.open(file) as img:
            return img.width, img.height
    except OSError as exc:
        logger.error("读取插图 %s 失败: %s", image_id, exc)
        return None


def build_report(book: Book, kind: str, path: Path) -> dict:
    """导出报告：如实说明每页插图的**实际**有效 dpi。

    生成图分辨率不足时明说"第 3 页有效 175dpi，建议重画"，让用户自己决定，
    而不是假装它是 300dpi。
    """
    mode = "print" if kind == "print" else "preview"
    bleed = book.bleedMm if mode == "print" else 0.0
    page_w, page_h = geometry.page_size_mm(
        book.trimSize.widthMm, book.trimSize.heightMm, bleed
    )

    warnings: list[str] = []
    images: list[dict] = []

    for number, page in enumerate(book.pages, start=1):
        if not page.imageId:
            continue

        box = image_box_mm(book, page, mode)
        if box is None or box[0] <= 0 or box[1] <= 0:
            continue

        source = _image_size(book, page.imageId)
        if source is None:
            warnings.append(f"第 {number} 页的插图文件读不出来，导出时会显示占位图。")
            continue

        dpi = round(geometry.placed_dpi(source[0], source[1], box[0], box[1], image_fit(page)))
        images.append(
            {
                "pageNumber": number,
                "imageId": page.imageId,
                "sourcePx": {"width": source[0], "height": source[1]},
                "placedMm": {"width": round(box[0], 2), "height": round(box[1], 2)},
                "effectiveDpi": dpi,
                "targetDpi": geometry.PRINT_DPI,
                "belowTarget": dpi < geometry.PRINT_DPI,
            }
        )

        if dpi < geometry.PRINT_DPI:
            warnings.append(
                f"第 {number} 页插图有效 {dpi}dpi（目标 {geometry.PRINT_DPI}），"
                "印刷会偏糊，建议重画这张图。"
            )

    return {
        "kind": kind,
        "pageCount": len(book.pages),
        "pageSizeMm": {"width": round(page_w, 2), "height": round(page_h, 2)},
        "bleedMm": bleed,
        "targetDpi": geometry.PRINT_DPI,
        "fileSizeBytes": path.stat().st_size if path.exists() else 0,
        "images": images,
        "warnings": warnings,
    }
