"""唯一的页面渲染入口。

预览（前端 iframe）和导出（Playwright）都走这里，拿到同一份 HTML。
这是"预览与导出永不跑版"的根本保证 —— 因为根本只有一套模板。

如果哪天想让前端 Vue 组件来渲染页面内容，请先读 README 的「架构要点」：
那会引入第二套模板，也就引入了两边不一致的可能。
"""

from pathlib import Path
from typing import Literal

from jinja2 import Environment, FileSystemLoader

from app.models.book import Book, FreePage, ImageBlock, Page, TextBlock
from app.render import geometry, presets

RenderMode = Literal["preview", "print"]

# 屏幕上按 96dpi 铺开，150 已经够看清细节；印刷按 300dpi。
_MODE_DPI = {"preview": geometry.SCREEN_DPI, "print": geometry.PRINT_DPI}

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

# 默认转义：页面里会插入用户的故事文本，必须防止它被当成标签解析。
# 自己的 CSS / JS 在模板里用 |safe 显式放行。
_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=True,
    trim_blocks=True,
    lstrip_blocks=True,
)


def _read_static(name: str) -> str:
    """每次都读盘：开发时改 CSS 不必重启服务，本地 IO 开销可忽略。"""
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def image_url(
    book_id: str,
    image_id: str | None,
    box_mm: tuple | None = None,
    fit: str = "cover",
    dpi: int = geometry.PRINT_DPI,
) -> str:
    """图片的 URL。

    带上 box_mm 时改要 `.jpg?box=WxH&fit=...`：资源端点会按**实际放置尺寸**
    重采样到对应像素数再返回。这是"300dpi"唯一真正落地的地方 —— 生成图动辄
    2048² 的 PNG，20 页原样嵌进 PDF 就是 80MB+；按盒子反算后能压到十几 MB，
    而印出来看不出差别。

    不带 box_mm 时给原图（前端缩略图用）。
    """
    if not image_id:
        return ""
    if box_mm is None:
        return f"/api/assets/books/{book_id}/images/{image_id}.png"

    width_px, height_px = geometry.box_pixels(box_mm[0], box_mm[1], dpi)
    return f"/api/assets/books/{book_id}/images/{image_id}.jpg?box={width_px}x{height_px}&fit={fit}"


def _text_css(style) -> str:
    """把样式对象转成内联 CSS。

    接受 TextStyle 和 PresetPageOptions 两种输入：后者字段更少（预设版式
    把字重、字距、竖排收在版式里，不暴露给用户逐页调），所以用 getattr
    带默认值读取。
    """
    parts = [
        f"font-family:'{style.fontFamily}'",
        f"font-size:{style.fontSizePt}pt",
        f"font-weight:{getattr(style, 'fontWeight', 400)}",
        f"color:{style.color}",
        f"text-align:{style.align}",
        f"line-height:{style.lineHeight}",
        f"letter-spacing:{getattr(style, 'letterSpacing', 0)}px",
        f"writing-mode:{getattr(style, 'writingMode', 'horizontal-tb')}",
    ]
    background = getattr(style, "backgroundColor", None)
    if background:
        # 用 background 简写而不是 background-color：预设页的满版叠字底衬是
        # rgba() 或渐变，转成自由页时要原样带过来。简写两种都收。
        parts.append(f"background:{background}")

    padding = getattr(style, "paddingMm", 0)
    if padding:
        parts.append(f"padding:{padding}mm")

    # 预设版式（PresetPageOptions）没有这个字段，getattr 兜底成 0
    radius = getattr(style, "borderRadiusMm", 0)
    if radius:
        parts.append(f"border-radius:{radius}mm")

    return ";".join(parts)


_VERTICAL_ALIGN = {"top": "flex-start", "middle": "center", "bottom": "flex-end"}


def block_box_mm(block, book: Book, mode: RenderMode) -> tuple:
    """块**实际被渲染出来**的 (x, y, w, h)，mm。

    与 _block_context 里的盒子必须是同一个值，所以只在这里算一次 ——
    图片按它反算像素，两处各算一遍迟早会出现"按 100mm 重采样、实际画在
    106mm 上"这种图不清不楚的问题。
    """
    x, y, w, h = block.xMm, block.yMm, block.widthMm, block.heightMm

    # 满版块向四周溢出出血区。溢出量随模式变（预览 0、印刷 3mm），所以
    # 不能烘进存下来的坐标里 —— 见 ImageBlock.bleed 的注释。
    if getattr(block, "bleed", False):
        bleed = book.bleedMm if mode == "print" else 0.0
        x, y = x - bleed, y - bleed
        w, h = w + 2 * bleed, h + 2 * bleed

    return x, y, w, h


def _block_context(block, book: Book, mode: RenderMode) -> dict:
    x, y, w, h = block_box_mm(block, book, mode)

    ctx = {
        "id": block.id,
        "type": block.type,
        "box_css": (
            f"left:{x}mm;top:{y}mm;width:{w}mm;height:{h}mm;"
            f"transform:rotate({block.rotationDeg}deg);"
            f"z-index:{block.zIndex};opacity:{block.opacity}"
        ),
    }
    if isinstance(block, TextBlock):
        ctx["text_css"] = _text_css(block.style)
        ctx["content"] = block.content
        ctx["auto_fit"] = block.style.autoFit
        # justify-content 必须落在 .block-text 上（它是 flex 容器），
        # 写到 .text-content 上是无效的 —— 那是 flex 子项。
        justify = _VERTICAL_ALIGN.get(block.style.verticalAlign, "flex-start")
        ctx["box_css"] = f"{ctx['box_css']};justify-content:{justify}"
    elif isinstance(block, ImageBlock):
        ctx["src"] = image_url(
            book.id, block.imageId, box_mm=(w, h), fit=block.fit, dpi=_MODE_DPI[mode]
        )
        ctx["image_css"] = (
            f"object-fit:{block.fit};"
            f"object-position:{block.focusX * 100}% {block.focusY * 100}%;"
            f"border-radius:{block.borderRadiusMm}mm"
        )
    return ctx


def preset_image_box_mm(page, book: Book, mode: RenderMode) -> tuple:
    """预设页里图片**实际被渲染出来**的 (w, h)，mm。

    满版版式的图挂在 .page 上（盖住出血区），其余挂在 .page-inner 的
    槽位里。搞错这一点，满版图会按成品尺寸重采样，印刷时四周那 3mm 就
    被拉伸糊掉。
    """
    trim_w = book.trimSize.widthMm
    trim_h = book.trimSize.heightMm

    if page.preset in ("full-bleed-overlay", "image-only"):
        bleed = book.bleedMm if mode == "print" else 0.0
        return trim_w + 2 * bleed, trim_h + 2 * bleed

    _, _, w, h = presets.image_slot_mm(page.preset, trim_w, trim_h)
    return w, h


def _page_fragment(book: Book, page: Page, mode: RenderMode) -> str:
    """单个 .page 元素的 HTML 片段，不带 head/CSS —— 只给整本渲染内部用。"""
    if isinstance(page, FreePage):
        blocks = [_block_context(b, book, mode) for b in page.blocks]
        return _env.get_template("page_free.html.j2").render(
            book=book, page=page, mode=mode, blocks=blocks
        )

    return _env.get_template("page_preset.html.j2").render(
        book=book,
        page=page,
        mode=mode,
        options=page.options,
        text_css=_text_css(page.options),
        image_src=image_url(
            book.id,
            page.imageId,
            box_mm=preset_image_box_mm(page, book, mode),
            fit="cover",
            dpi=_MODE_DPI[mode],
        ),
        # 叠字带的几何与白板样式都来自 presets —— 与 preset_to_blocks 用的是
        # 同一个函数，转换后文字块才会落在白板原来的位置上
        zone_css=_overlay_zone_css(page, book),
        panel_css=presets.overlay_panel_css(page.options.overlayPanelOpacity),
        panel_radius_mm=presets.OVERLAY_PANEL_RADIUS_MM,
    )


def _overlay_zone_css(page, book: Book) -> str:
    """叠字带的内联定位。见 presets.overlay_zone_mm。"""
    x, y, w, h = presets.overlay_zone_mm(page, book)
    return f"left:{x}mm;top:{y}mm;width:{w}mm;height:{h}mm"


def _document(book: Book, mode: RenderMode, body_html: str) -> str:
    """把页面片段套进完整的 HTML 文档。

    单页预览和整本导出都走这里，用的是**同一个** book.html.j2 —— 样式表、
    字体、fit-text.js 全在一处。分成两套模板迟早会出现"单页预览好看、
    导出跑版"，那正是本项目最想避免的一类 bug。
    """
    bleed = book.bleedMm if mode == "print" else 0.0
    page_w = book.trimSize.widthMm + 2 * bleed
    page_h = book.trimSize.heightMm + 2 * bleed

    vars_css = (
        f":root{{--trim-w:{book.trimSize.widthMm}mm;"
        f"--trim-h:{book.trimSize.heightMm}mm;"
        f"--bleed:{bleed}mm;}}"
        # @page 里不写 var()：部分 Chromium 版本对自定义属性在该规则内的
        # 解析不稳定，直接注入具体数值最保险
        f"\n@page{{size:{page_w}mm {page_h}mm;margin:0}}"
    )

    return _env.get_template("book.html.j2").render(
        book=book,
        mode=mode,
        vars_css=vars_css,
        css=_read_static("book.css"),
        fit_js=_read_static("fit-text.js"),
        pages_html=body_html,
    )


def render_page_html(book: Book, page: Page, mode: RenderMode) -> str:
    """单页的完整 HTML 文档。编辑器把它塞进 iframe 做实时预览。"""
    return _document(book, mode, _page_fragment(book, page, mode))


def render_book_html(book: Book, mode: RenderMode) -> str:
    """整本 HTML。Playwright 打开这个端点再 page.pdf()。"""
    pages_html = "\n".join(_page_fragment(book, p, mode) for p in book.pages)
    return _document(book, mode, pages_html)
