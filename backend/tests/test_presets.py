"""预设版式 → 自由拖拽 的等价性测试。

这是编辑器的硬验收标准：用户点"转自由拖拽"时，页面必须**看起来完全没变**。
差一点点都不行 —— 那等于逼用户为了获得拖拽能力接受一次他没要求的改版。

光比对代码里的数字不够（数字对不代表渲染出来对：flex 与绝对定位的差异、
padding 归属、object-fit 的盒子……都会让同样的数字渲染出不同的结果）。
所以这里用 Playwright 真的把两份 HTML 渲染出来，量元素的矩形再比。

Chromium 没装时整个文件跳过 —— 不能因为没装浏览器就让全量测试变红。
"""

import pytest

from app.models.book import Book, OverlayRect, PresetPage, TrimSize
from app.render import presets, renderer

PRESETS = [
    "image-top",
    "image-bottom",
    "split-lr",
    "full-bleed-overlay",
    "image-only",
    "text-only",
]

# 预设页与自由页里，承担同样角色的元素。
# 图片槽要连占位块一起选：测试里页面没有 imageId，满版版式渲染出来的是
# `.page > .image-placeholder` 而不是 `.bleed-image`（非满版则被 .slot-image
# 包着，所以用 > 限定层级，避免普通版式选中两个）。
PRESET_IMAGE = ".slot-image, .bleed-image, .page > .image-placeholder"
FREE_IMAGE = ".block-image"

# 文字只量**文字本身**（Range 包住文本节点），不量任何一层盒子。
#
# 两种模式的盒子结构天然不同：预设的 .slot-text 是 grid 单元、padding 挂在
# 它身上；自由页的 .block-text 是固定 mm 的盒子、padding 挂在 .text-content
# 身上。于是量哪一层都会差一个 padding（实测 20mm），而那不是视觉差异 ——
# 字还在原地。量墨迹就与盒子结构无关了，也正好是读者唯一看得见的东西。
PRESET_TEXT = ".slot-text .text-content, .overlay-panel"
FREE_TEXT = ".block-text .text-content"

# 允许 1px 的误差：mm→px 换算与亚像素取整不可避免
TOLERANCE_PX = 1.0


@pytest.fixture(scope="module")
def browser():
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright

    from app.api.health import chromium_status

    if not chromium_status()["ready"]:
        pytest.skip("Chromium 未安装，跳过渲染等价性测试")

    with sync_playwright() as p:
        instance = p.chromium.launch()
        yield instance
        instance.close()


_BOX_JS = """els => els.map(e => {
    const r = e.getBoundingClientRect();
    return {x: r.x, y: r.y, w: r.width, h: r.height};
})"""

# 文字的墨迹矩形：Range 圈住元素内容，拿到的是文字实际占的地方，
# 不含 padding、不受外层盒子结构影响。
_INK_JS = """els => els.map(e => {
    const range = document.createRange();
    range.selectNodeContents(e);
    const r = range.getBoundingClientRect();
    return {x: r.x, y: r.y, w: r.width, h: r.height};
})"""

_MEASURES = {"box": _BOX_JS, "ink": _INK_JS}


@pytest.fixture(scope="module")
def measure(browser):
    """把一份 HTML 渲染出来，按选择器量矩形。

    selectors 的值是 (选择器, 量法)，量法取 "box"（元素边框盒）或
    "ink"（文字墨迹）。图片量 box —— 图铺满盒子；文字量 ink —— 见上文。
    """
    page = browser.new_page(viewport={"width": 1400, "height": 2000})

    def _measure(html: str, selectors: dict) -> dict:
        page.set_content(html, wait_until="load")
        result = {}
        for name, (selector, kind) in selectors.items():
            result[name] = page.eval_on_selector_all(selector, _MEASURES[kind])
        return result

    yield _measure
    page.close()


def _book(preset: str, bleed_mm: float = 3) -> Book:
    # imageId=None：渲染成占位块，测试不依赖任何图片文件或网络
    return Book(
        title="等价性测试",
        trimSize=TrimSize.from_id("A5"),
        bleedMm=bleed_mm,
        pages=[PresetPage(text="从前有座山，山里有座庙。", preset=preset, imageId=None)],
    )


def _compare(boxes_a: list, boxes_b: list, label: str):
    assert len(boxes_a) == len(boxes_b), (
        f"{label}：元素数量不一致（预设 {len(boxes_a)} 个，自由 {len(boxes_b)} 个）"
    )
    for i, (a, b) in enumerate(zip(boxes_a, boxes_b)):
        for key in ("x", "y", "w", "h"):
            assert a[key] == pytest.approx(b[key], abs=TOLERANCE_PX), (
                f"{label} 第 {i} 个元素的 {key} 不一致："
                f"预设 {a[key]:.2f} vs 自由 {b[key]:.2f}"
            )


@pytest.mark.parametrize("mode", ["preview", "print"])
@pytest.mark.parametrize("preset", PRESETS)
def test_conversion_is_visually_identical(measure, preset: str, mode: str):
    book = _book(preset)
    preset_page = book.pages[0]
    free_page = presets.to_free_page(preset_page, book)

    preset_html = renderer.render_page_html(book, preset_page, mode)
    free_html = renderer.render_page_html(book, free_page, mode)

    a = measure(preset_html, {"image": (PRESET_IMAGE, "box"), "text": (PRESET_TEXT, "ink")})
    b = measure(free_html, {"image": (FREE_IMAGE, "box"), "text": (FREE_TEXT, "ink")})

    _compare(a["image"], b["image"], f"{preset}/{mode} 图片")
    _compare(a["text"], b["text"], f"{preset}/{mode} 文字")


@pytest.mark.parametrize("preset", PRESETS)
def test_conversion_preserves_content(measure, preset: str):
    """视觉一致还不够，文字和图片也得跟着过去，不能只剩个空盒子。"""
    book = _book(preset)
    page = book.pages[0]
    free = presets.to_free_page(page, book)

    assert free.text == page.text
    assert free.imageId == page.imageId
    assert free.id == page.id, "页面 id 必须保留，否则前端会丢失当前页"

    text_blocks = [b for b in free.blocks if b.type == "text"]
    if preset == "image-only":
        assert text_blocks == []
    else:
        assert text_blocks and text_blocks[0].content == page.text

    image_blocks = [b for b in free.blocks if b.type == "image"]
    assert (len(image_blocks) == 1) is (preset != "text-only")


@pytest.mark.parametrize("preset", PRESETS)
def test_image_block_uses_cover_fit(preset: str):
    """预设版的 .slot-image img / .bleed-image 都是 object-fit: cover。

    矩形一样但 fit 不同的话，图会被拉变形 —— 这属于"量矩形量不出来"的
    差异，得单独断言。
    """
    book = _book(preset)
    free = presets.to_free_page(book.pages[0], book)
    for block in free.blocks:
        if block.type == "image":
            assert block.fit == "cover"


def test_text_vertical_alignment_matches_slot_center():
    """预设的 .slot-text 是 justify-content:center。

    不把这一条翻成 verticalAlign='middle'，转换后文字会从居中跳到顶端 ——
    这是最容易漏、也最显眼的一处差异。
    """
    book = _book("image-top")
    free = presets.to_free_page(book.pages[0], book)
    text = next(b for b in free.blocks if b.type == "text")
    assert text.style.verticalAlign == "middle"


def test_overlay_keeps_panel_and_position():
    """满版叠字转成自由页后，底衬白板和落点都要跟着走。"""
    book = _book("full-bleed-overlay")
    page = book.pages[0]
    page.options.overlayPosition = "bottom"
    free = presets.to_free_page(page, book)
    text = next(b for b in free.blocks if b.type == "text")

    # 叠字区是整条横带、文字在带子里居中（.overlay-zone 是 justify-content:center）。
    # 写成 "bottom" 的话转自由后文字会贴到白板底边，看起来像换了个版式。
    assert text.style.verticalAlign == "middle"
    assert text.style.backgroundColor == presets.overlay_panel_css(
        page.options.overlayPanelOpacity
    )
    assert text.style.borderRadiusMm == presets.OVERLAY_PANEL_RADIUS_MM
    # 文字层必须压在图片之上
    image = next(b for b in free.blocks if b.type == "image")
    assert text.zIndex > image.zIndex


def test_full_bleed_is_flagged_not_baked_into_coordinates():
    """满版用 bleed 标记表达，坐标保持成品区尺寸。

    曾经把出血量直接减进 xMm/yMm（-3,-3,154,216），结果预览模式
    （--bleed:0）下块的盒子比页面大 3mm，object-fit: cover 的缩放基准
    跟着变，图的实际裁切与预设不一致 —— 而被 overflow:hidden 裁掉后
    肉眼看不出，只有量盒子才发现。
    """
    book = _book("image-only", bleed_mm=3)
    free = presets.to_free_page(book.pages[0], book)
    image = next(b for b in free.blocks if b.type == "image")

    assert image.bleed is True
    assert (image.xMm, image.yMm) == (0, 0)
    assert image.widthMm == book.trimSize.widthMm
    assert image.heightMm == book.trimSize.heightMm


@pytest.mark.parametrize("preset", ["image-top", "image-bottom", "split-lr"])
def test_non_bleed_presets_are_not_flagged(preset: str):
    book = _book(preset)
    free = presets.to_free_page(book.pages[0], book)
    image = next(b for b in free.blocks if b.type == "image")
    assert image.bleed is False


@pytest.mark.parametrize("mode,expected_overflow_mm", [("preview", 0.0), ("print", 3.0)])
def test_bleed_block_overflows_by_mode(mode: str, expected_overflow_mm: float):
    """溢出量由渲染模式决定：预览不溢出，印刷四周各溢出 3mm。"""
    book = _book("image-only", bleed_mm=3)
    free = presets.to_free_page(book.pages[0], book)
    html = renderer.render_page_html(book, free, mode)

    # 用 abs 避免 f-string 把 0.0 格式化成 "-0.0"
    offset = -expected_overflow_mm if expected_overflow_mm else 0.0
    assert f"left:{offset}mm" in html
    assert f"width:{book.trimSize.widthMm + 2 * expected_overflow_mm}mm" in html


def test_to_preset_round_trip_keeps_text_and_image():
    """自由 → 预设是有损的，但正文和图片必须留住。"""
    book = _book("split-lr")
    original = book.pages[0]
    free = presets.to_free_page(original, book)
    back = presets.to_preset_page(free, "text-only")

    assert back.text == original.text
    assert back.imageId == original.imageId
    assert back.id == original.id
    assert back.preset == "text-only"


def test_overlay_panel_opacity_is_adjustable():
    """底衬的不透明度是用户可调的，预设渲染与转自由必须用同一个值。

    两处各读一次 options 是最容易出现的写法，也是"转完底衬变了"的来源。
    """
    book = _book("full-bleed-overlay")
    page = book.pages[0]
    page.options.overlayPanelOpacity = 0.5
    expected = "rgba(255, 255, 255, 0.5)"

    free = presets.to_free_page(page, book)
    text = next(b for b in free.blocks if b.type == "text")
    assert text.style.backgroundColor == expected

    assert expected in renderer.render_page_html(book, page, "preview")


def test_auto_overlay_falls_back_to_bottom_without_image():
    """overlayPosition=auto 的位置来自插图分析；没图时退回贴底。

    "文字压在图上"这个前提在没图时不成立，这时贴底最稳 —— 也保证生图之前
    不会因为分析不出结果而把文字丢到页面顶上。
    """
    book = _book("full-bleed-overlay")  # imageId=None
    page = book.pages[0]
    assert page.options.overlayPosition == "auto"

    _, top, _, height = presets.overlay_zone_mm(page, book)
    assert top == pytest.approx(book.trimSize.heightMm - height - presets.OVERLAY_PANEL_INSET_MM)


@pytest.mark.parametrize("anchor", ["auto", "top", "center", "bottom"])
def test_overlay_zone_stays_inside_the_page(anchor: str):
    """白板必须整个落在成品区里 —— 跨过裁切线的那部分会被裁掉，文字就少一截。"""
    book = _book("full-bleed-overlay")
    book.pages[0].options.overlayPosition = anchor
    x, y, w, h = presets.overlay_zone_mm(book.pages[0], book)
    inset = presets.OVERLAY_PANEL_INSET_MM

    assert x == pytest.approx(inset), anchor
    assert w == pytest.approx(book.trimSize.widthMm - 2 * inset), anchor
    assert y >= -1e-6, f"{anchor}: 白板溢出页顶（top={y}）"
    assert y + h <= book.trimSize.heightMm + 1e-6, (
        f"{anchor}: 白板溢出页底（bottom={y + h}）"
    )


@pytest.mark.parametrize("anchor", ["top", "center", "bottom"])
def test_manual_overlay_positions_keep_clear_of_the_trim_line(anchor: str):
    """手动选的落点要离裁切线留出 inset —— 切偏 1mm 就会露出白边。

    auto **不**受这条约束：分析出来的空带本来就常常贴着页底，硬往页内推会
    把文字推到主体上（实测过，就是这么压到鱼身上的）。见 presets._auto_band_mm。
    """
    book = _book("full-bleed-overlay")
    book.pages[0].options.overlayPosition = anchor
    _, y, _, h = presets.overlay_zone_mm(book.pages[0], book)
    inset = presets.OVERLAY_PANEL_INSET_MM

    assert y >= inset - 1e-6, f"{anchor}: 白板压到上裁切线（top={y}）"
    assert y + h <= book.trimSize.heightMm - inset + 1e-6, (
        f"{anchor}: 白板压下裁切线（bottom={y + h}）"
    )


# ---------------------------------------------------------------- 智能配置的落点

# 智能排版写下的固定落点。**故意挑靠上的位置**：满版页没有插图时
# `overlayPosition` 的兜底是「贴底」（见 test_auto_overlay_falls_back_to_bottom
# _without_image），所以"文字跑到上半张"这件事本身就证明矩形生效了。
RECT = OverlayRect(xMm=20.0, yMm=20.0, widthMm=120.0, heightMm=45.0)


def _rect_book(preset: str = "full-bleed-overlay", bleed_mm: float = 3) -> Book:
    book = _book(preset, bleed_mm)
    book.pages[0].options.overlayRect = RECT
    return book


def test_overlay_rect_wins_over_position():
    """矩形压过 `overlayPosition` —— 这是「恢复自动」这个按钮存在的全部理由。

    不成立的话，用户点「叠字位置：上」屏幕上会毫无变化（矩形还在生效），
    而那必然会被报成 bug。
    """
    book = _rect_book()
    page = book.pages[0]
    # 挑一个跟矩形**完全相反**的锚点：bottom 会落到页面下缘，矩形在上缘
    page.options.overlayPosition = "bottom"

    assert presets.overlay_zone_mm(page, book) == pytest.approx(
        (RECT.xMm, RECT.yMm, RECT.widthMm, RECT.heightMm)
    )


def test_clearing_the_rect_restores_the_auto_rule():
    """「恢复自动」把它清成 None，就回到 overlayPosition 的规则。

    与上一条是一对：只测「矩形生效」的话，把 `overlay_zone_mm` 写死成永远
    返回矩形也能过。
    """
    book = _rect_book()
    page = book.pages[0]
    page.options.overlayPosition = "bottom"
    page.options.overlayRect = None

    x, y, w, h = presets.overlay_zone_mm(page, book)
    inset = presets.OVERLAY_PANEL_INSET_MM
    assert x == pytest.approx(inset)
    assert w == pytest.approx(book.trimSize.widthMm - 2 * inset)
    assert y + h == pytest.approx(book.trimSize.heightMm - inset)


@pytest.mark.parametrize(
    "rect,expected",
    [
        # 越界：右下角跑到页外，夹回来时 x/y 用的必须是**夹之后**的宽高。
        # 顺序写反（先夹 x/y 再夹 w/h）会算出负的 `trim_w - w`，x 被夹成负数、
        # 矩形跑到页面左边外面去 —— 文字整块不可见，比"位置不理想"严重得多。
        (OverlayRect(xMm=140, yMm=200, widthMm=60, heightMm=60), (88.0, 150.0, 60.0, 60.0)),
        # 宽高本身就超页（手改 book.json、或换开本后旧矩形没跟着变）
        (OverlayRect(xMm=10, yMm=10, widthMm=999, heightMm=999), (0.0, 0.0, 148.0, 210.0)),
        # 负的坐标/尺寸
        (OverlayRect(xMm=-30, yMm=-30, widthMm=80, heightMm=40), (0.0, 0.0, 80.0, 40.0)),
        (OverlayRect(xMm=10, yMm=10, widthMm=-5, heightMm=-5), (10.0, 10.0, 0.0, 0.0)),
    ],
)
def test_out_of_bounds_rect_is_clamped_into_the_trim_area(rect, expected):
    """越界的矩形夹进成品区，**不抛**。

    pydantic 上不加 ge/le 约束就是为了这个：手改 book.json 打错一个负号，
    代价不该是"这本书打不开了"。A5 = 148×210。
    """
    book = _book("full-bleed-overlay")
    book.pages[0].options.overlayRect = rect
    assert presets.overlay_zone_mm(book.pages[0], book) == pytest.approx(expected)


@pytest.mark.parametrize("mode", ["preview", "print"])
def test_conversion_is_visually_identical_with_overlay_rect(measure, mode: str):
    """**智能配置之后转自由页，位置一格不差。**

    上面那条 test_conversion_is_visually_identical 用的是默认的自动落点，
    碰不到 `overlayRect` 这条新路径；而这条路径恰恰是"跑完智能排版再点转自由
    拖拽"时走的 —— 两边都从 `overlay_zone_mm` 取矩形，所以必须等价。
    """
    book = _rect_book()
    page = book.pages[0]
    free = presets.to_free_page(page, book)

    a = measure(
        renderer.render_page_html(book, page, mode),
        {"image": (PRESET_IMAGE, "box"), "text": (PRESET_TEXT, "ink")},
    )
    b = measure(
        renderer.render_page_html(book, free, mode),
        {"image": (FREE_IMAGE, "box"), "text": (FREE_TEXT, "ink")},
    )

    _compare(a["image"], b["image"], f"rect/{mode} 图片")
    _compare(a["text"], b["text"], f"rect/{mode} 文字")

    # 等价性两边都忽略矩形时**也成立**，所以补一条"矩形真的生效了"：文字落在
    # 图片的上半张。矩形被忽略的话，没插图的满版页会退回贴底，文字在下半张。
    image, text = a["image"][0], a["text"][0]
    assert text["y"] + text["h"] / 2 < image["y"] + image["h"] / 2, (
        f"{mode}: 文字落在图片下半张（{text['y']:.1f} vs 图片中线 "
        f"{image['y'] + image['h'] / 2:.1f}）—— overlayRect 没被用上"
    )
