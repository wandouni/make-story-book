"""字号反算与字色可读性 —— 智能排版里唯一"算得出对错"的部分。

这个模块的两件事都有**客观的正确答案**，所以这里测的是性质，不是"跑通了"：

- 字号必须是"放得下的最大那个"：既不能溢出（难看且会被裁），也不能偏小
  （一页字小得莫名其妙）。所以断言两头 —— 它放得下，加 0.5pt 就放不下。
- 字色必须和背景对比度达标：这是 WCAG 定义的比值，可以独立算。

**行数公式在这里重写了一遍**（用 `-(-a // b)` 这种写法），不是调模块里的私有
函数 —— 调它就成了"用被测对象验证被测对象"，算错了也照样绿。
"""

import pytest

from app import config
from app.services import auto_layout

STEP = 0.5


# ---------------------------------------------------------------- 独立复算


def _lines(text: str, width_pt: float, size_pt: float) -> int:
    """独立实现的行数：每行 floor(宽/字号) 个字，逐段向上取整。"""
    per_line = max(1, int(width_pt // size_pt))
    return sum(max(1, -(-len(para) // per_line)) for para in text.split("\n"))


def _used_pt(text: str, box_w_mm: float, size_pt: float, line_height: float, padding_mm: float) -> float:
    width_pt = (box_w_mm - 2 * padding_mm) / auto_layout.MM_PER_PT
    return _lines(text, width_pt, size_pt) * size_pt * line_height


def _goal_pt(box_h_mm: float, padding_mm: float) -> float:
    return (box_h_mm - 2 * padding_mm) / auto_layout.MM_PER_PT * config.LAYOUT_TARGET_FILL


# ---------------------------------------------------------------- 字号：最大可行


@pytest.mark.parametrize(
    "chars,box_w,box_h",
    [
        (5, 200.0, 60.0),
        (18, 180.0, 40.0),
        (30, 180.0, 40.0),
        (48, 120.0, 60.0),
        (85, 281.0, 29.0),
        (123, 281.0, 29.0),
        (195, 281.0, 29.0),
        (195, 281.0, 57.0),
        (7, 60.0, 25.0),
        (1, 40.0, 20.0),
    ],
)
def test_fitted_size_is_the_largest_that_still_fits(chars, box_w, box_h):
    """**一条断言同时钉死"是最大可行字号"和"没算错"。**

    只测"放得下"的话，永远返回 min_pt 也能过；只测"加一点就放不下"的话，永远
    返回 max_pt 也能过。两头一起测，答案才被唯一确定。
    """
    text = "字" * chars
    fitted = auto_layout.fit_text_in_box(text, box_w, box_h, padding_mm=0)
    goal = _goal_pt(box_h, 0)
    used = _used_pt(text, box_w, fitted.fontSizePt, fitted.lineHeight, 0)

    assert used <= goal + 1e-6, (
        f"{chars} 字在 {box_w}×{box_h} 里排出来占 {used:.1f}pt，超过目标 {goal:.1f}pt"
    )
    # 顶到 max_pt 时再大一点当然也放得下（被上限截住的），所以那种情况不比
    if fitted.fontSizePt + STEP <= config.LAYOUT_MAX_FONT_PT:
        bigger = _used_pt(text, box_w, fitted.fontSizePt + STEP, fitted.lineHeight, 0)
        assert bigger > goal, (
            f"字号还能再大：{fitted.fontSizePt + STEP}pt 只占 {bigger:.1f}pt，"
            f"目标 {goal:.1f}pt —— 返回的不是最大可行字号"
        )


def test_padding_shrinks_the_usable_area():
    """内边距是四边都有的，而且 CSS 里是 border-box —— 少减一边就是必然溢出。

    预设页的 paddingMm 默认 10，不是小数目：一个 40mm 高的框，两倍内边距吃掉
    一半，不减的话算出来的字号会大一倍。
    """
    text = "字" * 40
    tight = auto_layout.fit_text_in_box(text, 100.0, 40.0, padding_mm=10)
    loose = auto_layout.fit_text_in_box(text, 100.0, 40.0, padding_mm=0)

    assert tight.fontSizePt < loose.fontSizePt


def test_padding_larger_than_the_box_does_not_crash():
    """框比两倍内边距还小（窄框 + 默认 10mm 内边距）→ 夹到 1pt 的可用宽高，别除零。"""
    fitted = auto_layout.fit_text_in_box("字" * 20, 15.0, 15.0, padding_mm=10)

    assert fitted.fontSizePt == config.LAYOUT_MIN_FONT_PT
    assert fitted.overflow


# ---------------------------------------------------------------- 字号：单调与边界


def test_bigger_box_never_gets_a_smaller_font():
    """框变大，字号不该变小。反过来的话"换个宽点的位置"会得到更小的字。"""
    text = "字" * 60
    sizes = [
        auto_layout.fit_text_in_box(text, 120.0, h, padding_mm=4).fontSizePt
        for h in (20.0, 25.0, 30.0, 40.0, 50.0, 70.0, 100.0)
    ]

    assert sizes == sorted(sizes), f"字号没跟着框高单调：{sizes}"


def test_one_character_in_a_big_box_hits_the_ceiling():
    """一个字放在大框里，答案是上限而不是"占满整页"的荒谬字号。"""
    fitted = auto_layout.fit_text_in_box("雪", 200.0, 200.0, padding_mm=4)

    assert fitted.fontSizePt == config.LAYOUT_MAX_FONT_PT
    assert fitted.lines == 1


def test_anchor_100mm_wide_box_five_characters():
    """数值锚点：100mm 宽的框、5 个字，一个字能占 20mm ≈ 56.7pt，被上限截住。

    这条钉的是单位换算（pt ↔ mm）没写反 —— 写反了会得到一个差 2.8 倍的字号，
    而那个数字**看起来仍然合理**，只有拿具体数字对才看得出来。
    """
    fitted = auto_layout.fit_text_in_box("一二三四五", 100.0, 100.0, padding_mm=0)

    assert fitted.fontSizePt == config.LAYOUT_MAX_FONT_PT
    assert fitted.lines == 1
    # 一个字 20mm = 20 / (25.4/72) ≈ 56.7pt —— 确实超过 40pt 的上限
    assert 20.0 / auto_layout.MM_PER_PT == pytest.approx(56.69, abs=0.01)


def test_two_lines_stay_two_lines():
    """正文里带换行的，换行是硬换行，不能被当成可以拼起来的一整段。

    分页器往正文里插的正是这种换行；当成一整段算会低估行数、字号给大、然后
    在浏览器里溢出。
    """
    fitted = auto_layout.fit_text_in_box("啊啊啊\n啊啊啊", 60.0, 40.0, padding_mm=0)

    assert fitted.lines >= 2


def test_long_text_in_a_small_box_bottoms_out_at_the_minimum():
    """字多框小：给最小字号并**明说放不下**，而不是算出一个 5pt 的答案。

    5pt 在印刷上是看不见的，而且没有任何地方会提示用户 —— 那才是真正的坑。
    """
    fitted = auto_layout.fit_text_in_box("字" * 200, 40.0, 30.0, padding_mm=0)

    assert fitted.fontSizePt == config.LAYOUT_MIN_FONT_PT
    assert fitted.overflow


def test_short_text_in_a_big_box_is_not_overflowing():
    fitted = auto_layout.fit_text_in_box("短短一句。", 150.0, 60.0, padding_mm=4)

    assert not fitted.overflow
    assert fitted.fill < 0.5


def test_empty_text_does_not_crash():
    """空正文：不崩、字号落在合法区间里。任务编排那层本来就会跳过空正文页，
    所以这里只保证"万一被调到"不炸。"""
    fitted = auto_layout.fit_text_in_box("", 100.0, 50.0)

    assert config.LAYOUT_MIN_FONT_PT <= fitted.fontSizePt <= config.LAYOUT_MAX_FONT_PT
    assert fitted.lines == 0
    assert not fitted.overflow


def test_line_height_stays_in_range_and_is_looser_for_short_text():
    """行距算法给、模型不碰：排得满就收紧、字少就放松，且始终在 1.3~1.7 里。"""
    cramped = auto_layout.fit_text_in_box("字" * 120, 180.0, 60.0, padding_mm=4)
    airy = auto_layout.fit_text_in_box("短短一句。", 180.0, 60.0, padding_mm=4)

    for fitted in (cramped, airy):
        assert 1.3 <= fitted.lineHeight <= 1.7
    assert airy.lineHeight > cramped.lineHeight


# ---------------------------------------------------------------- 颜色


@pytest.mark.parametrize("bg", [0.0, 0.1, 0.3, 0.46, 0.5, 0.7, 0.9, 1.0])
def test_auto_color_is_always_readable(bg):
    """纯算法路径给的字色，在任何亮度的背景上都得看得清 —— 这是降级路径的底线，
    没配文本模型的书全靠它。"""
    color = auto_layout.auto_text_color(bg)
    rgb = auto_layout.parse_hex_color(color)
    assert rgb is not None
    ratio = auto_layout.contrast_ratio(
        auto_layout.relative_luminance(rgb), auto_layout._background_luminance(bg)
    )

    assert ratio >= config.LAYOUT_MIN_CONTRAST, f"背景 {bg} 上给了 {color}，对比度只有 {ratio:.2f}"


def test_auto_color_is_dark_on_light_and_light_on_dark():
    """浅底给深色、深底给浅色 —— 用户原话就是"浅色背景或者白色背景的空白位置"，
    字要压在那种地方，就必须是深色。"""
    light_bg = auto_layout.parse_hex_color(auto_layout.auto_text_color(0.95))
    dark_bg = auto_layout.parse_hex_color(auto_text := auto_layout.auto_text_color(0.1))

    assert light_bg is not None and dark_bg is not None
    assert sum(light_bg) < 200, f"浅色背景上给了浅色字：{auto_text}"
    assert sum(dark_bg) > 600, f"深色背景上给了深色字：{auto_text}"


@pytest.mark.parametrize("bg", [0.2, 0.5, 0.8, 0.95])
@pytest.mark.parametrize("color", ["#FFFFFF", "#000000", "#F5E6C8", "#8AA0B8", "#2C3E50"])
def test_ensure_readable_never_returns_an_unreadable_color(bg, color):
    """守卫的核心性质：**进去什么颜色都行，出来的一定达标**。

    这条覆盖的是模型"米色字压米色天空"那类猜测 —— 它看不见图，颜色本来就是猜的。
    """
    fixed = auto_layout.ensure_readable(color, bg)
    rgb = auto_layout.parse_hex_color(fixed)

    assert rgb is not None, f"{color} 被换成了认不出来的 {fixed!r}"
    ratio = auto_layout.contrast_ratio(
        auto_layout.relative_luminance(rgb), auto_layout._background_luminance(bg)
    )
    assert ratio >= config.LAYOUT_MIN_CONTRAST, f"{color} 在背景 {bg} 上仍是 {fixed}，对比度 {ratio:.2f}"


def test_ensure_readable_keeps_a_color_that_already_passes():
    """达标就原样返回，一个字节都不动 —— 守卫不是"统一换成黑白"。"""
    assert auto_layout.ensure_readable("#2C3E50", 0.95) == "#2C3E50"


def test_ensure_readable_moves_as_little_as_possible():
    """不达标时朝可读的方向**挪一点**，不是直接换成纯黑或纯白 ——
    模型给的颜色倾向还在，版面上不会突然多出一块死黑。"""
    fixed = auto_layout.ensure_readable("#F5E6C8", 0.95)

    assert fixed != "#000000"
    rgb = auto_layout.parse_hex_color(fixed)
    assert rgb is not None and rgb[0] > 40, f"挪过头了：{fixed}"


def test_ensure_readable_rejects_a_color_it_cannot_parse():
    """模型给了 `"white"` 这种认不出来的值 → 换成一个一定看得清的纯黑/纯白。

    原样放出去是最坏的选择：那个颜色可能和背景一模一样，而没有任何地方会报错。
    """
    assert auto_layout.ensure_readable("white", 0.95) == "#000000"
    assert auto_layout.ensure_readable("", 0.05) == "#FFFFFF"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("#FFFFFF", (255, 255, 255)),
        ("#fff", (255, 255, 255)),
        ("2C3E50", (44, 62, 80)),
        ("  #2c3e50  ", (44, 62, 80)),
        ("#GGGGGG", None),
        ("#12345", None),
        ("rgb(1,2,3)", None),
        ("", None),
    ],
)
def test_parse_hex_color(value, expected):
    assert auto_layout.parse_hex_color(value) == expected


def test_relative_luminance_matches_the_wcag_anchors():
    """黑白两端的相对亮度是定义出来的：0 和 1。中间错了的话对比度全线偏移。"""
    assert auto_layout.relative_luminance((0, 0, 0)) == pytest.approx(0.0, abs=1e-9)
    assert auto_layout.relative_luminance((255, 255, 255)) == pytest.approx(1.0, abs=1e-9)
    # 纯绿的相对亮度是 0.7152 —— 权重写反了（比如把 R 的权重给了 G）会差很远
    assert auto_layout.relative_luminance((0, 255, 0)) == pytest.approx(0.7152, abs=1e-4)
    # 黑白对比度恰好 21:1，这是 WCAG 的定标点
    assert auto_layout.contrast_ratio(1.0, 0.0) == pytest.approx(21.0, abs=1e-9)
