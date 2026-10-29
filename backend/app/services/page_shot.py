"""把一页渲染成 PNG，给 3D 翻页当纹理。

打开的是**和 iframe 预览、和 PDF 导出同一个渲染端点**（render/renderer.py）。
这一点是刻意的：纹理里的像素就是导出 PDF 里那些像素，所以 3D 翻页里看到的
书仍然是"印出来会是什么样"，不存在第二套模板 —— 前端自己用 canvas 重画一遍
页面内容的话，那才是真正会跑版的做法。

和 PDF 导出的两点不同：

- 不切 `emulate_media(media="print")`。截图要的是**屏幕**媒体，和 iframe 预览
  一致；切到 print 媒体会走 @page 那套，和用户在平面预览里看到的不是一回事。
- 截的是 `.page` 元素本身，不是整个视口。页面四周的留白、桌面背景都不该
  进纹理，否则贴到 3D 平面上会带一圈灰边。
"""

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Optional
from uuid import uuid4

from app import config
from app.export.browser import get_browser
from app.logger import get_logger
from app.models.book import Book, Page
from app.storage import paths

logger = get_logger()

# 截图用的视口。要装得下整页：A4 是 210×297mm = 794×1123 CSS px，含出血再多
# 6mm。给宽裕一点 —— 页面被视口挤窄了会按窄宽度重排，截出来的排版就不对了。
_VIEWPORT = {"width": 1400, "height": 2000}

# 纹理的像素倍率。3D 里一页在屏幕上最多几百 px 宽，2 倍已经够清晰；
# 再往上显存翻倍而肉眼几乎看不出差别。
DEFAULT_SCALE = 2
MAX_SCALE = 3

# 同时截几页。Playwright 单浏览器开多个 context 是可以的，但每张都是一次
# 完整页面渲染，压太多会互相拖慢。
_CONCURRENCY = 2
_semaphore: Optional[asyncio.Semaphore] = None


def _limiter() -> asyncio.Semaphore:
    # 信号量要绑在事件循环上，模块导入时创建会在测试里出问题
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_CONCURRENCY)
    return _semaphore


def fingerprint(book: Book, page: Page, mode: str, scale: int) -> str:
    """这一页当前内容的指纹，用来给缓存文件命名。

    任何会改变渲染结果的东西都要进指纹：页面自身的**全部**字段（文字、版式、
    块、插图 id、叠字位置）、书级的画风与角色描述、开本与出血，以及模式。
    漏掉任何一个，用户改了东西却看到旧图 —— 而这种错很难看出来，因为画面
    本身是"对的"，只是内容是上一版的。
    """
    payload = {
        "page": page.model_dump(mode="json"),
        "style": book.style,
        "character": book.characterDescription,
        "trim": book.trimSize.model_dump(mode="json"),
        "bleed": book.bleedMm,
        "mode": mode,
        "scale": scale,
        # 模板和 book.css 改了也该让旧图失效；版本号是这里最省事的代理
        "v": config.APP_VERSION,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


async def shot_page(book: Book, page: Page, number: int, mode: str, scale: int) -> Path:
    """渲染一页并落盘，返回 PNG 路径。已有缓存就直接返回。"""
    path = paths.page_shot_file(
        book.id, number, mode, scale, fingerprint(book, page, mode, scale)
    )
    if path.exists():
        return path

    async with _limiter():
        # 双检：对开的两页是并发请求的，等到信号量时另一张可能刚渲染完
        if path.exists():
            return path

        path.parent.mkdir(parents=True, exist_ok=True)
        browser = await get_browser()
        context = await browser.new_context(viewport=_VIEWPORT, device_scale_factor=scale)
        try:
            shot = await context.new_page()
            # 用真实 URL 而不是 set_content：页面里的插图和字体都是相对路径，
            # set_content 下没有 base，全都取不到
            url = (
                f"{config.SELF_BASE_URL}/api/books/{book.id}"
                f"/render/page/{number}?mode={mode}"
            )
            await shot.goto(url, wait_until="networkidle")

            # 字体没加载完就截，度量按 fallback 字体算，纹理上的排版是错的
            await shot.evaluate("document.fonts.ready")
            await shot.wait_for_function(
                "Array.from(document.images).every(i => i.complete && i.naturalWidth > 0)",
                timeout=60_000,
            )
            # 和 PDF 导出跑的是同一个函数，两边缩字结果一致
            await shot.evaluate("window.fitAllText()")

            element = await shot.query_selector(".page")
            if element is None:
                raise RuntimeError(f"第 {number} 页渲染出来没有 .page 元素")

            # 先写临时文件再原子替换：并发请求可能正在读这个路径，
            # 直接写会让它读到半张图。
            #
            # 临时名不带 .png 后缀，所以只能截成 bytes 自己写 —— 传 path 给
            # screenshot() 的话它按扩展名推断格式，`.png.part` 会直接报
            # "unsupported mime type"。
            data = await element.screenshot()
            tmp = path.with_name(f"{path.name}.{uuid4().hex[:8]}.part")
            tmp.write_bytes(data)
            tmp.replace(path)
        finally:
            await context.close()

    logger.info("第 %d 页截图完成（%s，%d 倍）→ %s", number, mode, scale, path.name)
    return path
