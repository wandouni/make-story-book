"""渲染层测试。

核心不变量：单页预览和整本导出用的是**同一份** book.html.j2。曾经单页
渲染只返回一个裸 <div>（没有 head/CSS），塞进 iframe 就是一堆无样式元素 ——
test_page_html_is_a_full_document 就是为这个回归加的。
"""

import re

import pytest

from app.models.book import Book, PresetPage, TrimSize
from app.render import presets, renderer

_STYLE_OR_SCRIPT = re.compile(r"<(style|script)\b.*?</\1>", re.DOTALL)


def _markup(html: str) -> str:
    """只留 body 里的标签，去掉 <style>/<script>。

    断言"页面里有没有某个元素"时不能拿整份文档去 in —— CSS 里几乎什么
    选择器都有（`.crop-mark{display:none}` 会让"预览没有裁切标记"这条
    测试假通过）。必须只看真正会渲染的标签。
    """
    return _STYLE_OR_SCRIPT.sub("", html)


@pytest.fixture
def book() -> Book:
    return Book(
        title="测试绘本",
        trimSize=TrimSize.from_id("A5"),  # 148 × 210mm
        bleedMm=3,
        pages=[
            # 都带上 imageId：没有图时模板渲染的是占位符，测不到真正的 <img>
            PresetPage(text="第一页的文字。", preset="image-top", imageId="p001_v1"),
            PresetPage(
                text="第二页的文字。", preset="full-bleed-overlay", imageId="p002_v1"
            ),
            PresetPage(text="第三页。", preset="text-only"),
        ],
    )


# ---------------------------------------------------------------- 单页


def test_page_html_is_a_full_document(book: Book):
    """iframe 直接指向这个端点，必须是能独立渲染的完整文档。"""
    html = renderer.render_page_html(book, book.pages[0], "preview")

    assert html.lstrip().lower().startswith("<!doctype html")
    assert "<html" in html
    assert "<style>" in html
    # 开本尺寸靠 CSS 变量下发，缺了页面就没有物理尺寸
    assert "--trim-w" in html and "--trim-h" in html
    # 字体和自动缩字脚本也要带上，否则预览与导出观感不一致
    assert "@font-face" in html
    assert "fitAllText" in html


def test_page_html_contains_only_that_page(book: Book):
    """单页渲染不能把整本都塞进来 —— 否则 iframe 会显示 6 页叠在一起。"""
    html = renderer.render_page_html(book, book.pages[1], "preview")

    assert "第二页的文字。" in html
    assert "第一页的文字。" not in html
    assert html.count('<div class="page ') == 1


def test_page_html_escapes_story_text(book: Book):
    """用户的故事文本必须转义，否则 <script> 会被当成标签解析。"""
    page = PresetPage(text="<script>alert(1)</script>", preset="text-only")
    html = renderer.render_page_html(book, page, "preview")

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# ---------------------------------------------------------------- 整本


def test_book_html_has_one_page_element_per_page(book: Book):
    html = renderer.render_book_html(book, "preview")
    assert html.count('<div class="page ') == len(book.pages)


def test_preview_has_no_bleed(book: Book):
    html = renderer.render_book_html(book, "preview")
    assert "--bleed:0.0mm" in html
    assert "@page{size:148.0mm 210.0mm" in html


def test_print_page_size_includes_bleed(book: Book):
    """印刷版 @page 要等于裁切尺寸 + 两侧出血，否则印刷厂对不上裁切线。"""
    html = renderer.render_book_html(book, "print")
    assert "--bleed:3.0mm" in html
    assert "@page{size:154.0mm 216.0mm" in html


def test_crop_marks_only_in_print(book: Book):
    """裁切标记只该出现在印刷版的标签里 —— 预览版印出来是脏的。"""
    assert 'class="crop-mark' not in _markup(renderer.render_book_html(book, "preview"))
    assert 'class="crop-mark' in _markup(renderer.render_book_html(book, "print"))


def test_crop_marks_count(book: Book):
    """每页 8 个（四角各两条），页数不对印刷厂会缺裁切参考。"""
    html = _markup(renderer.render_book_html(book, "print"))
    assert html.count('class="crop-mark') == 8 * len(book.pages)


# ---------------------------------------------------------------- 版式


@pytest.mark.parametrize(
    "preset",
    [
        "image-top",
        "image-bottom",
        "split-lr",
        "full-bleed-overlay",
        "image-only",
        "text-only",
    ],
)
def test_every_preset_renders(book: Book, preset: str):
    page = PresetPage(text="正文。", preset=preset, imageId="p001_v1")
    markup = _markup(renderer.render_page_html(book, page, "preview"))

    assert f"preset-{preset}" in markup

    # 图片槽的有无由模板里的 has_image_slot 决定：满版叠字和整页插图都
    # 不用 slot-image（前者用 .page 层的 bleed-image，后者同样走满版）。
    has_image_slot = preset not in ("text-only", "full-bleed-overlay", "image-only")
    assert ('class="slot-image"' in markup) is has_image_slot

    # 满版叠字的文字走独立的 overlay-zone 层（带白板底衬），不是普通文字槽
    if preset == "image-only":
        assert 'class="slot-text"' not in markup
        assert "overlay-zone" not in markup
    elif preset == "full-bleed-overlay":
        assert 'class="slot-text"' not in markup
        assert 'class="overlay-zone"' in markup
        assert 'class="overlay-panel"' in markup
    else:
        assert 'class="slot-text"' in markup


def test_full_bleed_overlay_puts_image_outside_page_inner(book: Book):
    """满版图要挂在 .page 上而不是 .page-inner 里，否则盖不住出血区。"""
    page = PresetPage(text="正文。", preset="full-bleed-overlay", imageId="p001_v1")
    markup = _markup(renderer.render_page_html(book, page, "preview"))

    assert 'class="bleed-image"' in markup
    # 叠字层应该在 page-inner 内部
    assert 'class="overlay-zone"' in markup
    # 底衬是内联注入的（为了让预设页与自由页共用 render/presets.py 里的同一个
    # 字符串）。断言内联值本身，别去断言某个类名 —— 样式已经不在 CSS 里了。
    assert presets.overlay_panel_css(page.options.overlayPanelOpacity) in markup


def test_missing_image_renders_visible_placeholder(book: Book):
    """没图时必须是看得见的占位，不能是一块空白 —— 否则用户以为版式坏了。"""
    page = PresetPage(text="正文。", preset="image-top", imageId=None)
    markup = _markup(renderer.render_page_html(book, page, "preview"))

    assert "image-placeholder" in markup
    assert "暂无插图" in markup


def test_failed_image_placeholder_says_failed(book: Book):
    """生图失败的页要在页面上写明失败，不能悄悄留白。"""
    page = PresetPage(
        text="正文。", preset="image-top", imageId=None, imageStatus="failed"
    )
    markup = _markup(renderer.render_page_html(book, page, "preview"))

    assert "图片生成失败" in markup
