"""预设版式 → 自由拖拽块 的展开。

这是整个编辑器里最容易出错的一步，因为它有一个硬性验收标准：
**转换后视觉零变化**。用户点"转自由拖拽"时，看到的必须还是同一张页面，
只是从此可以拖了。差一点点都不行 —— 那意味着用户为了获得拖拽能力，
被迫接受一次他并没要求的改版。

所以这里的几何比例必须与 `static/book.css` 里的 grid/flex 定义逐条对齐，
并且把预设里那些"藏在 CSS 类里的效果"（满版图的出血溢出、叠字的渐变底衬、
文字槽的垂直居中）显式翻译成块的属性。改 book.css 时务必回来改这里，
`tests/test_presets.py` 会对比两边的渲染结果。

这些比例同时被 `services/paginator.py` 的 text_box_mm 使用（用于容量估算），
三者要保持一致。
"""

from typing import List, Tuple

from app.models.book import (
    Block,
    Book,
    FreePage,
    ImageBlock,
    PresetPage,
    TextBlock,
    TextStyle,
    new_id,
)
from app.services import image_analysis

# 图片槽占页高的比例（image-top / image-bottom）
IMAGE_ROW_RATIO = 0.55
TEXT_ROW_RATIO = 0.45

# 叠字底衬：半透明白色圆角板。
#
# 之前用的是一条白色渐变。渐变在浅色图上是够的，图一花就压不住；而且它横跨
# 整页、从下往上淡出，会把插图底部一整片洗白。换成板子之后只挡文字底下那一块，
# 图还看得见，字也更清楚。
#
# 这些数字定义在这里而不是 CSS 里，是为了让预设页和自由页共用同一份 —— 转自由
# 拖拽时要把底衬原样变成块的样式，两处各写一份迟早会出现"转完底衬变了"。
OVERLAY_PANEL_RADIUS_MM = 4
# 板子离成品区边缘的距离。留一点，别让白板顶到裁切线。
OVERLAY_PANEL_INSET_MM = 8
# 非 auto 时叠字带的高度占页高的比例。auto 用的是分析出来的带高
# （见 services/image_analysis.py 的 BAND_RATIO），这里只是给固定位置一个
# 说得过去的默认值。
OVERLAY_BAND_RATIO = 0.30


def overlay_panel_css(opacity: float) -> str:
    """叠字底衬的内联 background。"""
    return f"rgba(255, 255, 255, {opacity:g})"


def _auto_band_mm(page: PresetPage, book: Book) -> Tuple[float, float] | None:
    """分析插图得到的横带 (top, height)，mm。仅 overlayPosition == auto 时有值。"""
    if page.options.overlayPosition != "auto":
        return None

    trim_w = book.trimSize.widthMm
    trim_h = book.trimSize.heightMm
    # 用成品尺寸做裁切基准：块的坐标也是相对成品区记的（见 _text_slot_mm）
    band = image_analysis.overlay_band(
        book.id,
        page.imageId,
        trim_w,
        trim_h,
        page.options.imageFocusX,
        page.options.imageFocusY,
    )
    if band is None:
        return None

    # 分析结果**照用，只保证不出页**，不套用 OVERLAY_PANEL_INSET_MM。
    #
    # 这里踩过坑：一开始为了让白板离裁切线远点，把带子往页内推了 8mm。但插画的
    # 主体多半在上半张，分析出来的空带常常本来就贴着页底（实测那条带的下沿正好
    # 是页底），一推就整条上移、文字压到主体上 —— 恰恰是自动定位想避免的事。
    #
    # 贴到裁切线上的白板没问题：它在成品区**之内**，只是边缘与裁切线重合，
    # 视觉上就是一条压底的色带。真正会出问题的"跨过裁切线"由这里的 clamp 挡住。
    height = min(band[1] * trim_h, trim_h)
    top = min(max(band[0] * trim_h, 0.0), trim_h - height)
    return top, height


def _clamp_rect_mm(rect, book: Book) -> Tuple[float, float, float, float]:
    """把显式矩形夹进成品区，返回 (x, y, w, h)。

    **必须先夹 w/h 再夹 x/y**：顺序反了的话，x 的右界用的是夹之前的宽度，会算出
    负的 `trim_w - w` —— 于是 x 被夹成负数，矩形跑到页面左边外面去，文字整块
    不可见。越界的矩形比"位置不理想"严重得多，所以宁可夹。

    宽高本身也可能超页（手改 book.json、或者换开本之后旧矩形没跟着变）。
    """
    trim_w = book.trimSize.widthMm
    trim_h = book.trimSize.heightMm
    w = min(max(rect.widthMm, 0.0), trim_w)
    h = min(max(rect.heightMm, 0.0), trim_h)
    x = min(max(rect.xMm, 0.0), trim_w - w)
    y = min(max(rect.yMm, 0.0), trim_h - h)
    return x, y, w, h


def overlay_zone_mm(page: PresetPage, book: Book) -> Tuple[float, float, float, float]:
    """叠字**区**的 (x, y, w, h)，mm，相对成品区。

    **预设页和自由页都用这一个矩形** —— 转自由拖拽时文字块就是它。

    注意这条带子不等于那块看得见的白板：白板按内容收缩，在带子里垂直居中
    （预设页是 `.overlay-panel` + `justify-content:center`，自由页是
    `.text-content` + `vertical-align:middle`，两边行为一致）。带子只是
    "文字可以占据的范围"——它的高度必须在 Python 侧就定下来，因为自由页的块
    矩形是死的，而文字排完才知道有多高。

    智能排版写下的 `overlayRect` 在这里**短路**。短路点选在这里是有讲究的：
    这个函数被两个消费者共用（`renderer._overlay_zone_css` 渲染预设页、
    `preset_to_blocks` 展开成自由页），在它开头短路，两条路自动一致 ——
    「智能配置之后转自由页，位置一格不差」这条性质不需要额外维护。
    """
    if page.options.overlayRect is not None:
        return _clamp_rect_mm(page.options.overlayRect, book)

    trim_w = book.trimSize.widthMm
    trim_h = book.trimSize.heightMm
    inset = OVERLAY_PANEL_INSET_MM

    band = _auto_band_mm(page, book)
    if band is not None:
        top, height = band
    else:
        height = trim_h * OVERLAY_BAND_RATIO
        # auto 但分析不出来（还没生图）时按 bottom 走：没有图的时候"文字压在
        # 图上"这个前提本身就不成立，按老规矩贴底最稳
        anchor = page.options.overlayPosition
        if anchor == "top":
            top = inset
        elif anchor == "center":
            top = (trim_h - height) / 2
        else:
            top = trim_h - height - inset

    return inset, top, trim_w - 2 * inset, height


# ---------------------------------------------------------------- 槽位几何


def image_slot_mm(preset: str, trim_w: float, trim_h: float) -> Tuple[float, float, float, float]:
    """图片槽的 (x, y, w, h)，单位 mm。坐标相对成品区（不含出血）。"""
    if preset == "image-top":
        return 0.0, 0.0, trim_w, trim_h * IMAGE_ROW_RATIO
    if preset == "image-bottom":
        return 0.0, trim_h * TEXT_ROW_RATIO, trim_w, trim_h * IMAGE_ROW_RATIO
    if preset == "split-lr":
        return 0.0, 0.0, trim_w * 0.5, trim_h
    # 满版图与整页插图都铺满整页
    return 0.0, 0.0, trim_w, trim_h


def _text_slot_mm(preset: str, trim_w: float, trim_h: float) -> Tuple[float, float, float, float]:
    """文字槽的 (x, y, w, h)，单位 mm。"""
    if preset == "image-top":
        return 0.0, trim_h * IMAGE_ROW_RATIO, trim_w, trim_h * TEXT_ROW_RATIO
    if preset == "image-bottom":
        return 0.0, 0.0, trim_w, trim_h * TEXT_ROW_RATIO
    if preset == "split-lr":
        return trim_w * 0.5, 0.0, trim_w * 0.5, trim_h
    # text-only 与 full-bleed-overlay 的文字层都是整页
    return 0.0, 0.0, trim_w, trim_h




# ---------------------------------------------------------------- 展开


def preset_to_blocks(page: PresetPage, book: Book) -> List[Block]:
    """把预设页展开成等价的绝对定位块。

    块的坐标相对成品区，单位 mm —— 与 page_free.html.j2 的渲染方式一致。
    """
    trim_w = book.trimSize.widthMm
    trim_h = book.trimSize.heightMm
    options = page.options

    blocks: List[Block] = []

    if page.preset != "text-only":
        # 满版版式：坐标按成品区记，溢出出血区交给渲染时处理（见 ImageBlock.bleed）
        full_bleed = page.preset in ("full-bleed-overlay", "image-only")
        x, y, w, h = image_slot_mm(page.preset, trim_w, trim_h)
        blocks.append(
            ImageBlock(
                id=new_id("b"),
                xMm=x,
                yMm=y,
                widthMm=w,
                heightMm=h,
                zIndex=0,
                imageId=page.imageId,
                # 预设版的 .slot-image img / .bleed-image 都是 object-fit: cover
                fit="cover",
                focusX=options.imageFocusX,
                focusY=options.imageFocusY,
                bleed=full_bleed,
            )
        )

    if page.preset != "image-only":
        overlay = page.preset == "full-bleed-overlay"
        # 满版叠字的矩形来自 overlay_zone_mm —— 渲染器那边用的是同一个函数，
        # 所以"转自由拖拽"之后文字块落在白板原来的位置上，一格都不差。
        if overlay:
            x, y, w, h = overlay_zone_mm(page, book)
        else:
            x, y, w, h = _text_slot_mm(page.preset, trim_w, trim_h)

        blocks.append(
            TextBlock(
                id=new_id("b"),
                xMm=x,
                yMm=y,
                widthMm=w,
                heightMm=h,
                zIndex=1,
                content=page.text,
                style=TextStyle(
                    fontFamily=options.fontFamily,
                    fontSizePt=options.fontSizePt,
                    color=options.color,
                    align=options.align,
                    lineHeight=options.lineHeight,
                    paddingMm=options.paddingMm,
                    autoFit=True,
                    # 两种版式的文字都是垂直居中：.slot-text 是 justify-content:
                    # center，叠字带也是。不带上这一条，转换后文字会跳到顶端。
                    verticalAlign="middle",
                    # 叠字的白板。不带上它，浅色图上的字会看不清。
                    backgroundColor=(
                        overlay_panel_css(options.overlayPanelOpacity) if overlay else None
                    ),
                    borderRadiusMm=OVERLAY_PANEL_RADIUS_MM if overlay else 0,
                ),
            )
        )

    return blocks


def to_free_page(page: PresetPage, book: Book) -> FreePage:
    """预设页 → 自由页。保留页面 id，前端不需要重新定位当前页。"""
    return FreePage(
        id=page.id,
        text=page.text,
        imageId=page.imageId,
        imagePrompt=page.imagePrompt,
        imageStatus=page.imageStatus,
        background=page.background,
        blocks=preset_to_blocks(page, book),
    )


def to_preset_page(page: FreePage, preset: str) -> PresetPage:
    """自由页 → 预设页。

    这是一次**有损**转换：块的位置、旋转、逐块样式都会被丢弃，
    只剩正文和图片。前端必须二次确认后再调用。
    """
    text_block = next((b for b in page.blocks if isinstance(b, TextBlock)), None)
    image_block = next((b for b in page.blocks if isinstance(b, ImageBlock)), None)

    return PresetPage(
        id=page.id,
        text=text_block.content if text_block else page.text,
        imageId=image_block.imageId if image_block else page.imageId,
        imagePrompt=page.imagePrompt,
        imageStatus=page.imageStatus,
        background=page.background,
        preset=preset,
    )
