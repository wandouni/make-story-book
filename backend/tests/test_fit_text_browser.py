"""字号反算 vs 真 Chromium：预测和真值对不对得上。

`test_auto_layout.py` 测的是"这个函数按它自己的公式算得对"—— 用的是同一套行数
模型，所以单位换算写反了、内边距少减一边、pt↔px 记错，它全都照样绿。
**只有真的渲染一遍才知道。**

这里把算出来的字号塞进真 book.css + 真 fit-text.js（按 page_preset.html.j2 的
同一段 markup 搭），然后看 fit-text.js 有没有被迫缩字：

- Python 说放得下 → 浏览器必须原样接受，一个字都不许缩
- Python 说放不下（已到最小字号）→ 浏览器必须缩

两边判据其实是同一件事（内容高 > 可用高），所以这是一条**双向**断言：
只测一个方向的话，"永远返回最小字号"和"永远返回最大字号"各能骗过一边。
"""

from pathlib import Path

import pytest

from app import config
from app.models.book import PresetPageOptions
from app.render import presets, renderer
from app.services import auto_layout

TRIM_W, TRIM_H = 297.0, 210.0
INSET = presets.OVERLAY_PANEL_INSET_MM

SENT = "鲁班跟着师傅学手艺，每天天不亮就起来磨刀。"

# (说明, 文字, 框宽 mm, 框高 mm, 内边距 mm)
CASES = [
    ("很短", "小兔子。", 281.0, 29.4, 10),
    ("一句话", "小兔子推开木门，外面是一片银白的雪地。", 281.0, 29.4, 10),
    ("两句话", SENT * 2, 281.0, 29.4, 10),
    ("中等长度", SENT * 3, 281.0, 56.7, 10),
    ("较长", SENT * 5, 281.0, 56.7, 10),
    ("一整页", SENT * 9, 281.0, 56.7, 10),
    ("硬换行", "换行\n也要算\n硬换行", 140.0, 40.0, 10),
    ("窄框短句", "小兔子。", 104.0, 42.0, 10),
    ("窄框长句", "小兔子推开木门，外面是雪地。", 104.0, 42.0, 10),
    ("无内边距", "一二三四五", 100.0, 60.0, 0),
    # 字多框扁 + 默认 10mm 内边距：可用高只剩 9.4mm，怎么排都不够。
    # 这条是**故意**要它放不下的 —— 它同时也是"候选框必须先过一遍可行性筛选"
    # 这个结论的来源（见 run_layout_job）：薄框 + 默认内边距能把可用高吃掉七成。
    ("必然放不下", SENT * 15, 281.0, 29.4, 10),
]


def _html() -> str:
    css = (Path(config.BASE_DIR) / "app" / "render" / "static" / "book.css").read_text("utf-8")
    fit_js = (Path(config.BASE_DIR) / "app" / "render" / "static" / "fit-text.js").read_text("utf-8")
    panels = []
    for i, (_, text, box_w, box_h, padding) in enumerate(CASES):
        options = PresetPageOptions(paddingMm=padding)
        fitted = auto_layout.fit_text_in_box(text, box_w, box_h, padding_mm=padding)
        options.fontSizePt = fitted.fontSizePt
        options.lineHeight = fitted.lineHeight
        panels.append(
            f'<div class="page mode-preview"><div class="page-inner preset-full-bleed-overlay">'
            f'<div class="overlay-zone" style="left:{INSET}mm;top:20mm;'
            f'width:{box_w}mm;height:{box_h}mm">'
            f'<div class="overlay-panel" data-autofit="1" data-case="{i}" '
            f'style="{renderer._text_css(options)};'
            f" background:{presets.overlay_panel_css(0.0)};"
            f'border-radius:{presets.OVERLAY_PANEL_RADIUS_MM}mm">{text}</div>'
            f"</div></div></div>"
        )
    return (
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='UTF-8'>"
        f"<style>:root{{--trim-w:{TRIM_W}mm;--trim-h:{TRIM_H}mm;--bleed:0mm}}</style>"
        f"<style>{css}</style></head><body class='mode-preview'>"
        + "".join(panels)
        + f"<script>{fit_js}</script></body></html>"
    )


@pytest.fixture(scope="module")
def browser():
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright

    from app.api.health import chromium_status

    if not chromium_status()["ready"]:
        pytest.skip("Chromium 未安装，跳过字号实测")

    with sync_playwright() as p:
        instance = p.chromium.launch()
        yield instance
        instance.close()


@pytest.fixture(scope="module")
def measured(browser):
    """渲染一次，量回每个面板的字号和溢出状态。

    **字体必须真的加载进来**：book.css 里 @font-face 的地址是 `/api/assets/...`，
    页面本身是 `set_content` 出来的、没有源，字体请求会直接失败、静默回退到系统
    字体 —— 那时量的是另一个字体的度量，这条测试就白写了。所以这里用一个假域名
    导航 + 拦截请求把 woff2 从磁盘喂回去。
    """
    fonts_dir = Path(config.FONTS_DIR)
    if not (fonts_dir / "lxgw-wenkai-400.woff2").exists():
        pytest.skip("自托管字体不在，跳过字号实测")

    page = browser.new_page(viewport={"width": 1400, "height": 2000})
    html = _html()

    def handler(route):
        url = route.request.url
        if "/api/assets/fonts/" in url:
            path = fonts_dir / url.rsplit("/", 1)[-1]
            if path.exists():
                route.fulfill(status=200, content_type="font/woff2", body=path.read_bytes())
            else:
                route.fulfill(status=404, body="")
            return
        route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

    page.route("**/*", handler)
    page.goto("http://make-story-book.test/check")
    # fit-text.js 在 DOMContentLoaded 和 fonts.ready 各跑一次，字体没到位时量的
    # 是回退字体的结果 —— 等它真加载完
    page.wait_for_function("() => document.fonts.status === 'loaded'")
    page.wait_for_timeout(200)

    result = page.evaluate(
        """() => [...document.querySelectorAll('.overlay-panel')].map(el => ({
            id: Number(el.dataset.case),
            computedPx: parseFloat(getComputedStyle(el).fontSize),
            overflow: el.dataset.overflow,
            scrollH: el.scrollHeight,
            zoneH: el.parentElement.clientHeight,
            font: getComputedStyle(el).fontFamily,
        }))"""
    )
    page.close()
    assert len(result) == len(CASES)
    return result


def _fitted(case) -> auto_layout.Fitted:
    _, text, box_w, box_h, padding = case
    return auto_layout.fit_text_in_box(text, box_w, box_h, padding_mm=padding)


def test_the_real_font_is_actually_loaded(measured):
    """先证明这条测试量的是对的字体。

    字体没加载进来时，页面会静默回退到系统字体、渲染结果**看起来也正常** ——
    唯一能发现的办法就是直接问一句。这条挂了，下面两条的结论全部作废。
    """
    assert "LXGW WenKai" in measured[0]["font"], (
        f"渲染用的不是自托管字体（{measured[0]['font']}）—— 量到的度量不是生产环境的"
    )


def test_a_size_python_says_fits_is_never_shrunk_by_the_browser(measured):
    """**这条是整套字号反算的验收标准。**

    Python 是预测、浏览器是真值。预测偏大 → 文字在浏览器里被缩（甚至被裁），
    用户看到的是"智能排版排出来的字忽大忽小"；偏小 → 一页字小得莫名其妙。
    `target_fill=0.88` 留的那 12% 余量就是为这一条服务的，所以这里必须是
    **一个字都没缩**（不是"缩得不多"）。
    """
    for row in measured:
        case = CASES[row["id"]]
        fitted = _fitted(case)
        if fitted.overflow:
            continue  # 放不下的情况见下一条
        expected_px = fitted.fontSizePt * 96 / 72

        assert row["overflow"] == "0", f"case{row['id']} {case[0]}：浏览器标了溢出"
        assert row["computedPx"] == pytest.approx(expected_px, abs=0.6), (
            f"case{row['id']} {case[0]}（{case[1][:12]}… {case[2]:.0f}×{case[3]:.0f}mm）："
            f"预测 {fitted.fontSizePt}pt = {expected_px:.1f}px，"
            f"浏览器实到 {row['computedPx']}px —— 被 fit-text.js 缩了"
        )


def test_python_flags_overflow_exactly_when_the_browser_must_shrink(measured):
    """反方向：Python 说放不下时，浏览器**必须**缩。

    只测上面那一条的话，`overflow` 恒为 True 也能过 —— 而那意味着每一页都被
    标成"文字较多，已用最小字号"，提示就成了噪音。
    """
    for row in measured:
        fitted = _fitted(CASES[row["id"]])
        if not fitted.overflow:
            continue
        expected_px = fitted.fontSizePt * 96 / 72
        assert row["computedPx"] < expected_px - 0.6, (
            f"case{row['id']}：Python 说 {fitted.fontSizePt}pt 放不下，"
            f"浏览器却原样接受了 —— overflow 是误报"
        )


def test_at_least_one_case_actually_overflows(measured):
    """上一条如果一条 overflow 都没有，它会**空转通过**。

    所以钉一条"这批用例里确实有放不下的"，让上一条的断言永远有东西可查。
    """
    assert any(_fitted(c).overflow for c in CASES), (
        "用例里没有一条放不下的 —— 上一条测试在空转"
    )
