"""大模型智能排版：按插图的空白区自动配置文字。

分工是**刻意的**，别把边界搞糊：

- 空白**在哪**（`layout_analysis.blank_candidates`）+ 挑哪块、什么颜色（大模型）
- 字号多大、行距多少（**这个模块算**）

字号不让模型碰，因为它看不见字体度量：同一个"18pt"在楷体和宋体里占的宽度不同，
在 297mm 宽的页和 148mm 宽的页上更不是一回事。模型猜出来的数字在浏览器里必然
要么溢出被裁、要么留一大块白。而框的尺寸和文字长度都是已知的，反算是闭式解。

文件分三段，边界是有意的：

1. **纯函数**（字号反算、字色可读性、目标页选择）—— 无 IO、无状态，单独可测
2. **模型决策层**（提示词、回复解析）—— 纯函数，模型调用本身在 `_ask_model`
3. **任务编排**（`run_layout_job`、快照与撤销）—— 唯一碰 IO 的地方

前两段在"没配文本模型"的降级路径上原样跑，那是**合法的运行模式**而不是错误
（见 `run_layout_job` 里的说明）。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable

from pydantic import TypeAdapter, ValidationError

from app import config
from app.logger import get_logger
from app.models.book import (
    Book,
    ImageBlock,
    OverlayRect,
    Page,
    PresetPage,
    TextBlock,
    now_iso,
)
from app.services import job_manager, layout_analysis, text_generator
from app.storage import book_store, paths

logger = get_logger()

# 只有满版叠字的文字是"压在图上"的，也只有它需要智能排版。
# 其余版式的文字在 CSS grid 槽里，跟"图上哪儿空"没关系。
LAYOUT_PRESET = "full-bleed-overlay"

# 1pt = 25.4/72 mm。CSS 的 pt 与 mm 在 Chromium 里都是固定比例，没有 DPI 问题，
# 所以这里直接换算，不需要问浏览器一个字到底多宽。
MM_PER_PT = 25.4 / 72.0

# 退让步长（pt）。20pt 上下 0.5pt 是 2.5%，肉眼看不出来，但足够让离散校验收敛。
_STEP_PT = 0.5

# 行距的上下界与基准，见 _line_height()。
_LINE_HEIGHT_MIN = 1.3
_LINE_HEIGHT_MAX = 1.7
_LINE_HEIGHT_BASE = 1.35
_LINE_HEIGHT_SLACK = 0.30

# 浅底上的墨色（与 TextStyle.color 的默认值同一个）、深底上的白色。
_DARK_INK = "#2C3E50"
_LIGHT_INK = "#FFFFFF"

# 朝可读端点插值的步数。24 步在 8 位色深下差不多是每步 10 个色阶，
# 够细到"尽量少动模型给的颜色"，又不至于把守卫变成一次逐色阶搜索。
_BLEND_STEPS = 24


@dataclass(frozen=True)
class Fitted:
    """一个框里排下这段文字的结果。"""

    fontSizePt: float
    lineHeight: float
    lines: int
    # 实际占用高 / 可用高。远小于 1 说明这个框对这段字来说太大了。
    fill: float
    # 到了 MIN_FONT_PT 还是放不下 —— 浏览器会裁掉一部分（fit-text.js 还能再缩
    # 到 55%，但那时已经小到不好看了）。调用方要把这件事告诉用户。
    overflow: bool


# ---------------------------------------------------------------- 字号反算


def _char_count(text: str) -> int:
    """等效字数。换行不算字 —— 它占的是一整行，不是一个字。"""
    return sum(1 for ch in text if ch != "\n")


def _line_count(text: str, width_pt: float, size_pt: float) -> int:
    """按浏览器的换行规则估行数。

    中文没有空格、`word-break: break-word`，所以断点就是"排满一行就换"：每行
    `floor(宽 / 字号)` 个字（中文一字宽 = 一个字号 = 1em）。

    **逐段算**：`\\n` 在 `white-space: pre-wrap` 下是硬换行，每段的最后一行不会
    和下一段拼起来 —— 按总字数算会低估行数，字号就会给大。分页器往正文里插的
    换行正是这种。

    拉丁字母实际比一个字号窄，按 1em 算会高估行数 —— 方向是安全的（字号偏小
    而不是溢出），所以不做字符宽度表。
    """
    if size_pt <= 0:
        return 0
    per_line = max(1, int(width_pt // size_pt))
    return sum(max(1, math.ceil(len(para) / per_line)) for para in text.split("\n"))


def _used_height_pt(text: str, width_pt: float, size_pt: float, line_height: float) -> float:
    """这段文字排完占多高（pt）。CSS 的行盒高就是 字号 × line-height。"""
    return _line_count(text, width_pt, size_pt) * size_pt * line_height


def _fit_size(
    text: str,
    width_pt: float,
    goal_pt: float,
    line_height: float,
    min_pt: float,
    max_pt: float,
) -> float:
    """放得进 `goal_pt` 高的最大字号。

    闭式解只是**起点**：把"每行都排满"当假设，占用高 ≈ (字数/每行字数) × 字号 ×
    行距 = 字数 × 字号² × 行距 / 宽，解出 字号 ≤ sqrt(goal × 宽 / (字数 × 行距))。
    但真实的每段最后一行是排不满的，行数只会比连续近似更多 —— 所以拿到起点之后
    按真实公式逐步退让。多退的那几轮正是"别信闭式解"这件事本身。
    """
    count = _char_count(text)
    if count == 0:
        return max_pt
    size = min(max_pt, math.sqrt(goal_pt * width_pt / (count * line_height)))
    # 起点**向下**取到步长的整数倍，之后每一步都落在格子上。这样返回的数字是
    # 干净的 0.5 的整数倍，而且"再加一步就是上一个被否掉的候选"严格成立 ——
    # 闭式解是 s 的真上界（行数只会比连续近似更多，见上），所以比它大的字号
    # 一定放不下，取整不会把答案缩到"其实还能再大"。
    size = math.floor(size / _STEP_PT) * _STEP_PT
    # 闭式解可能已经低于下限（字特别多、框特别小）。这时**用下限**而不是把它当
    # 答案返回：字号的下限是产品决定，不是算出来的 —— 真放不下是 overflow 的事，
    # 由调用方告诉用户"文字较多，已用最小字号"。
    #
    # 上下限都取步长的整数倍（config 里就是 12 / 40），上面那句"再加一步一定
    # 放不下"才在贴着下限时也成立。
    size = max(size, min_pt)
    while size > min_pt and _used_height_pt(text, width_pt, size, line_height) > goal_pt:
        size = max(min_pt, size - _STEP_PT)
    return size


def _line_height(fill: float) -> float:
    """行距：排得满就收紧，字少就放松。

    字少的页面（一页两行）用 1.7 会显得疏朗，字多的页面用 1.7 会把行挤到框外，
    所以让它跟着"排得满不满"走。上下界收在 1.3~1.7 —— 绘本的正文不该比这更密。
    """
    return min(
        _LINE_HEIGHT_MAX,
        max(_LINE_HEIGHT_MIN, _LINE_HEIGHT_BASE + _LINE_HEIGHT_SLACK * (1 - fill)),
    )


def fit_text_in_box(
    text: str,
    box_w_mm: float,
    box_h_mm: float,
    *,
    padding_mm: float = 4.0,
    min_pt: float = config.LAYOUT_MIN_FONT_PT,
    max_pt: float = config.LAYOUT_MAX_FONT_PT,
    target_fill: float = config.LAYOUT_TARGET_FILL,
) -> Fitted:
    """算出"这段文字放进这个框"该用多大字号、多大行距。

    `box_w_mm` / `box_h_mm` 是**框**的尺寸；`padding_mm` 是框的内边距（预设页是
    `PresetPageOptions.paddingMm`，自由页是 `TextStyle.paddingMm`）。内边距是
    四边都有的，而且 CSS 里是 `box-sizing: border-box` —— 文字真正能用的地方是
    框减掉两倍内边距，不是整个框。
    """
    width_pt = max(1.0, (box_w_mm - 2 * padding_mm) / MM_PER_PT)
    height_pt = max(1.0, (box_h_mm - 2 * padding_mm) / MM_PER_PT)
    goal_pt = height_pt * target_fill

    if _char_count(text) == 0:
        return Fitted(
            fontSizePt=max_pt,
            lineHeight=_LINE_HEIGHT_MAX,
            lines=0,
            fill=0.0,
            overflow=False,
        )

    # 两趟，不是循环。第一趟用**最大**行距解 —— 那是最保守的解，一定放得下 ——
    # 拿它估"排得满不满"；再按估出来的行距重解一次。
    #
    # 第二趟的解一定还放得下：行距只会 ≤ 第一趟的 1.7，而 _fit_size 自带真实公式
    # 的校验。省掉的是一次迭代收敛的循环，代价只是多算一遍行数。
    coarse = _fit_size(text, width_pt, goal_pt, _LINE_HEIGHT_MAX, min_pt, max_pt)
    fill_est = _used_height_pt(text, width_pt, coarse, _LINE_HEIGHT_MAX) / height_pt
    # **先取整再拿去算字号**，不是算完再取整：否则 f.lineHeight 和真正用过的行距
    # 差那 0.005，"算出来放得下"这句话就没法被逐字复算了。
    line_height = round(_line_height(fill_est), 2)

    size = round(_fit_size(text, width_pt, goal_pt, line_height, min_pt, max_pt), 2)
    used = _used_height_pt(text, width_pt, size, line_height)
    return Fitted(
        fontSizePt=size,
        lineHeight=round(line_height, 2),
        lines=_line_count(text, width_pt, size),
        fill=min(1.0, used / height_pt),
        # 判据是**满高**不是 goal：goal 只是目标，超出它但没超出框不算溢出
        overflow=used > height_pt,
    )


# ---------------------------------------------------------------- 字色可读性


def _srgb_to_linear(channel: float) -> float:
    """sRGB 传输函数的逆。WCAG 的相对亮度是在**线性**空间里算的。"""
    return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4


def parse_hex_color(value: str) -> tuple[int, int, int] | None:
    """`#RRGGBB` / `#RGB` / 不带 `#` 都收。认不出来返回 None。"""
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in text):
        return None
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def _to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*(max(0, min(255, c)) for c in rgb))


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG 相对亮度，0（黑）~1（白）。"""
    r, g, b = (c / 255.0 for c in rgb)
    return (
        0.2126 * _srgb_to_linear(r)
        + 0.7152 * _srgb_to_linear(g)
        + 0.0722 * _srgb_to_linear(b)
    )


def contrast_ratio(a: float, b: float) -> float:
    """两个相对亮度之间的对比度，1（一样）~21（黑白）。"""
    high, low = max(a, b), min(a, b)
    return (high + 0.05) / (low + 0.05)


def _background_luminance(bg_luminance: float) -> float:
    """把 `region_luminance` 的 0~1 感知亮度换成 WCAG 相对亮度。

    两者**不是一回事**：那个是 `(0.299R+0.587G+0.114B)/255`，算在伽马编码值上；
    WCAG 要的是线性化之后的。直接拿它当相对亮度用，会把浅灰算得比实际亮得多
    —— 守卫就会在该拦的时候放行。

    中间隔一层"等亮度的灰"是有依据的近似：守卫要判断的是"这块地方有多亮"，
    不是它的色相，而感知亮度本来就是把色相平均掉的结果。
    """
    return _srgb_to_linear(max(0.0, min(1.0, bg_luminance)))


def _readable_endpoint(bg: float) -> str:
    """黑还是白：哪个跟背景的对比度高。交叉点落在相对亮度 0.179 附近。

    用"哪个更高"而不是写一个阈值：两端对比度相等的地方就是分界，写死阈值迟早
    会和这个等式漂开。
    """
    return "#000000" if contrast_ratio(0.0, bg) >= contrast_ratio(1.0, bg) else "#FFFFFF"


def ensure_readable(
    color_hex: str,
    bg_luminance: float,
    min_ratio: float = config.LAYOUT_MIN_CONTRAST,
) -> str:
    """把字色拉到和背景对比度达标，**尽量少动它**。

    模型看不见图，颜色是凭候选框的亮度数字猜的，猜出"米色字压米色天空"很正常。
    不达标时朝黑或白线性插值（在 sRGB 空间，肉眼看就是"往那个方向加深/变淡"），
    取第一个达标的色 —— 这样模型给的颜色倾向还在，而不是一上来就换成纯黑。

    认不出来的颜色（模型给了 `"white"` 这种）没法判对比度，直接给一个一定看得清
    的纯黑或纯白：宁可换个颜色，也不能把一个可能看不见的颜色原样放出去。
    """
    bg = _background_luminance(bg_luminance)
    endpoint = _readable_endpoint(bg)
    rgb = parse_hex_color(color_hex)
    if rgb is None:
        return endpoint
    if contrast_ratio(relative_luminance(rgb), bg) >= min_ratio:
        return _to_hex(rgb)

    target = parse_hex_color(endpoint)
    assert target is not None  # endpoint 是上面自己写的字面量
    for step in range(1, _BLEND_STEPS + 1):
        t = step / _BLEND_STEPS
        mixed = tuple(round(a + (b - a) * t) for a, b in zip(rgb, target))
        if contrast_ratio(relative_luminance(mixed), bg) >= min_ratio:
            return _to_hex(mixed)
    # 纯黑对任何背景的对比度都 ≥ 4.58、纯白对称，所以 4.5 这条线一定到得了这里
    # 之前就返回。真走到这一行只可能是 min_ratio 被调得更高了。
    return endpoint


def auto_text_color(bg_luminance: float) -> str:
    """没有模型给色（或降级到纯算法）时的字色：浅底给墨色、深底给白色。"""
    bg = _background_luminance(bg_luminance)
    ink = _DARK_INK if _readable_endpoint(bg) == "#000000" else _LIGHT_INK
    return ensure_readable(ink, bg_luminance)


# ---------------------------------------------------------------- 目标页选择


@dataclass(frozen=True)
class Target:
    """一页要智能配置的页，以及"在哪配"。"""

    number: int  # 页码，从 1 开始，与前端展示一致
    text: str
    image_id: str
    # 插图被 cover 进去的盒子 (x, y, w, h)，mm，相对成品区。满版页就是整个
    # 成品区；自由页是那个图片块的位置 —— 候选框是相对**这个盒子**归一化的。
    box: tuple[float, float, float, float]
    focus_x: float
    focus_y: float
    # 文字框的内边距。字号反算要减掉它，否则算出来的字号一定放不下。
    padding_mm: float
    # 自由页才有的：要改的那个文字块。预设页是 None。
    block_id: str | None = None


def _largest(blocks: Iterable, cls: type) -> Any | None:
    """面积最大的那种块，**等面积取第一个**。

    `max` 在并列时返回最先遇到的那个，所以这个函数是确定性的 —— 同一本书
    跑两次必须得到同一个结果，否则"撤销再跑一次"会得到不一样的书。
    """
    return max(
        (b for b in blocks if isinstance(b, cls)),
        key=lambda b: b.widthMm * b.heightMm,
        default=None,
    )


def _classify(book: Book, page: Page, number: int) -> tuple[Target | None, str]:
    """(目标, "") 或 (None, 跳过原因)。"""
    trim_w = book.trimSize.widthMm
    trim_h = book.trimSize.heightMm

    if isinstance(page, PresetPage):
        # 顺序有讲究：先说版式，再说内容，最后说插图 —— 用户从最外层的问题
        # 开始解决。一页同时"版式不对"和"没插图"时，告诉他版式不需要就够了。
        if page.preset != LAYOUT_PRESET:
            return None, "版式不需要"
        if not page.text.strip():
            return None, "没有文字"
        if not page.imageId:
            return None, "还没有插图"
        return (
            Target(
                number=number,
                text=page.text,
                image_id=page.imageId,
                box=(0.0, 0.0, trim_w, trim_h),
                focus_x=page.options.imageFocusX,
                focus_y=page.options.imageFocusY,
                padding_mm=page.options.paddingMm,
            ),
            "",
        )

    block = _largest(page.blocks, TextBlock)
    if block is None or not block.content.strip():
        return None, "没有文字块"

    image = _largest(page.blocks, ImageBlock)
    if image is not None and image.imageId:
        box = (image.xMm, image.yMm, image.widthMm, image.heightMm)
        image_id, focus_x, focus_y = image.imageId, image.focusX, image.focusY
    elif page.imageId:
        # 页面级 imageId（预设页转过来的自由页就是这样，图片块被用户删掉了
        # 也走这里）。按整页算 —— 退一步总比这一页没得配强。
        box = (0.0, 0.0, trim_w, trim_h)
        image_id, focus_x, focus_y = page.imageId, 0.5, 0.5
    else:
        return None, "还没有插图"

    return (
        Target(
            number=number,
            text=block.content,
            image_id=image_id,
            box=box,
            focus_x=focus_x,
            focus_y=focus_y,
            padding_mm=block.style.paddingMm,
            block_id=block.id,
        ),
        "",
    )


def applicable_pages(book: Book) -> tuple[list[Target], list[tuple[int, str]]]:
    """这本书里哪些页要智能配置，哪些页跳过、为什么。

    「没有插图就跳过」是有意的：功能的前提是"图上有空白"，在占位图上跑没有
    意义；而且这让"跳过 N 页"的提示**可操作**（用户知道该去配图）。

    自由页只改**面积最大**的那个文字块（等面积取第一个，见 `_largest`）——
    副标题、页码这类小块的相对位置是用户精心摆的，把它搬走比不搬更糟。
    """
    targets: list[Target] = []
    skips: list[tuple[int, str]] = []
    for number, page in enumerate(book.pages, start=1):
        target, reason = _classify(book, page, number)
        if target is None:
            skips.append((number, reason))
        else:
            targets.append(target)
    return targets, skips


def skip_summary(skips: list[tuple[int, str]]) -> str:
    """「版式不需要 3 页，还没有插图 2 页」。按首次出现的顺序分组。"""
    counts: dict[str, int] = {}
    for _, reason in skips:
        counts[reason] = counts.get(reason, 0) + 1
    return "，".join(f"{reason} {count} 页" for reason, count in counts.items())


# ---------------------------------------------------------------- 每页的计划


@dataclass(frozen=True)
class Plan:
    """一页的全部计算材料。**在调模型之前就算好**，模型只负责挑编号。"""

    target: Target
    candidates: list[layout_analysis.Candidate]
    # 候选框在页面坐标里的 (x, y, w, h)，mm。与 candidates 一一对应。
    rects: list[tuple[float, float, float, float]]
    # 每个候选排下这段文字的结果，与 candidates 一一对应。
    fitted: list[Fitted]


def _plan(book_id: str, target: Target) -> Plan:
    box_x, box_y, box_w, box_h = target.box
    candidates = layout_analysis.blank_candidates(
        book_id,
        target.image_id,
        box_w,
        box_h,
        target.focus_x,
        target.focus_y,
        text_len=len(target.text),
        max_candidates=config.LAYOUT_MAX_CANDIDATES,
    )
    rects = layout_analysis.candidates_mm(candidates, box_x, box_y, box_w, box_h)
    fitted = [
        fit_text_in_box(target.text, rect[2], rect[3], padding_mm=target.padding_mm)
        for rect in rects
    ]

    # **可行性筛选**：候选尺寸是按"中文一字 = 1em"的理想情况反推的，而预设页
    # 默认有 10mm 内边距 —— 一条 29.4mm 高的薄框扣掉两边只剩 9.4mm 可用高，
    # 189 个字怎么排都放不下。这种框不能给模型选：它选中的那页会得到 12pt 的
    # 字号，浏览器再按 MIN_SCALE 缩到 ~6.7pt，小到看不见而界面不报警。
    #
    # 一个可行的都没有时**保留全部**：那时"最不坏的框"仍然比不配强，调用方
    # 会把 overflow 写进 item.error 告诉用户。
    keep = [i for i, f in enumerate(fitted) if not f.overflow]
    if keep and len(keep) < len(candidates):
        candidates = [candidates[i] for i in keep]
        rects = [rects[i] for i in keep]
        fitted = [fitted[i] for i in keep]

    return Plan(target=target, candidates=candidates, rects=rects, fitted=fitted)


# ---------------------------------------------------------------- 模型决策层

_LAYOUT_SYSTEM = (
    "你是一个儿童绘本的排版助手，负责在插画上为正文挑一个不挡主体的位置，"
    "并给一个看得清的文字颜色。"
)


def build_prompt(plans: list[Plan], trim_w: float, trim_h: float) -> str:
    """把每页的候选框报给模型，让它挑编号。

    **只给编号，不给自由坐标。** 文本模型没有视觉 grounding，让它自由输出
    坐标，得到的数字看起来合理但完全任意，而且**无法校验**（任何 mm 值都能被
    clamp 成"合法"的，于是错误不可检测）。约束成编号之后，失败模式从"给出了
    一个不可能的框"退化成"选了个不够好的框"，可处置性差一个量级。

    每一页的编号都从 1 开始，并且写明了 —— 不然 30 页连着编号，模型抄一个
    两位数都容易抄错，而抄错是**静默**的（落在合法范围内，看着像正常选择）。
    """
    lines = [
        "请为下面每一页的正文挑一个放文字的位置。",
        "",
        f"页面尺寸：宽 {trim_w:.0f} 毫米，高 {trim_h:.0f} 毫米。",
    ]
    for plan in plans:
        target = plan.target
        excerpt = target.text[: config.LAYOUT_TEXT_EXCERPT_CHARS]
        ellipsis = "……" if len(target.text) > config.LAYOUT_TEXT_EXCERPT_CHARS else ""
        lines.append("")
        lines.append(
            f"第 {target.number} 页，正文 {_char_count(target.text)} 个字："
            f"「{excerpt}{ellipsis}」"
        )
        lines.append("  可选位置：")
        for index, (x, y, w, h) in enumerate(plan.rects, start=1):
            lines.append(
                f"   {index} 号：左 {x:.0f} 毫米，上 {y:.0f} 毫米，"
                f"宽 {w:.0f} 毫米，高 {h:.0f} 毫米"
            )
    lines += [
        "",
        "请为每一页挑一个编号，并给一个适合压在插画上的文字颜色。",
        "颜色要和那块地方的明暗相反：那块地方浅就用深色，那块地方深就用浅色。",
        "每一页的位置都从 1 号开始编号，挑编号最大的也不要超过该页列出的个数。",
        "",
        "只输出一个 JSON 对象，不要解释：",
        '{"页":[{"页号":1,"框号":3,"颜色":"#FFFFFF"}]}',
    ]
    return "\n".join(lines)


@dataclass(frozen=True)
class Decision:
    """模型对一页的决定：第几个候选框、什么颜色。"""

    index: int  # **0 基**，已经减过 1
    color: str  # 可能是空串（模型没给颜色）


# 模型给的中文键和英文键都收。**中文键是主推的**（提示词里就是中文），
# 别名只是为了让"用中文键"这个决定不承重 —— 换个模型/换个版本就开始回
# `page/index/color` 是常事，那时不该整本降级。
_PAGE_KEYS = ("页号", "页码", "page", "pageNumber", "index", "序号")
_BOX_KEYS = ("框号", "位置", "编号", "box", "boxIndex", "position", "candidate")
_COLOR_KEYS = ("颜色", "字色", "color", "colour")
_LIST_KEYS = ("页", "pages", "结果", "results", "data")

_INT = re.compile(r"-?\d+")


def _to_int(value: Any) -> int | None:
    """`1` / `"1"` / `"1 号"` / `"第 1 页"` 都认。认不出来返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        match = _INT.search(value)
        return int(match.group()) if match else None
    return None


def _pick(row: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row:
            return row[key]
    lowered = {str(k).lower(): v for k, v in row.items()}
    for key in keys:
        if key.lower() in lowered:
            return lowered[key.lower()]
    return None


def parse_decisions(reply: Any) -> dict[int, Decision]:
    """模型回复 → `{页码: Decision}`。

    认不出来的条目**直接丢掉**，不抛：丢掉的页会走算法兜底，而"整本解析失败"
    才是真的灾难。越界的框号也在这里就挡掉 —— **不 clamp 到边界**，越界说明
    模型在瞎猜，clamp 只会把文字贴到页角上。
    """
    if isinstance(reply, dict):
        rows = next(
            (reply[key] for key in _LIST_KEYS if isinstance(reply.get(key), list)), None
        )
        # 只有一页时模型可能不套外层，直接给 {"页号":1,"框号":2,...}
        rows = [reply] if rows is None else rows
    elif isinstance(reply, list):
        rows = reply
    else:
        return {}

    out: dict[int, Decision] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        number = _to_int(_pick(row, _PAGE_KEYS))
        box = _to_int(_pick(row, _BOX_KEYS))
        if number is None or box is None or box < 1:
            continue
        color = _pick(row, _COLOR_KEYS)
        out[number] = Decision(index=box - 1, color=str(color).strip() if color else "")
    return out


async def _ask_model(plans: list[Plan], trim_w: float, trim_h: float) -> tuple[dict[int, Decision], str]:
    """让模型挑每页的框。返回 (决策表, 降级说明)。

    三级降级，照抄 `generate_prompts_batch` 的结构：

    1. 批量问一次
    2. 返回的页号集合与请求**不完全一致**（少页、多页、条数不符）→ 逐页问
    3. 单页也问不出来 → 那一页用算法第一选择，说明写进 item

    第 2 级存在的理由和第 1 级一样：模型少给一页是常事，而少给的那页如果直接
    用算法兜底，用户看到的是"大部分页智能配了、零星几页是老的自动横带"，
    归因不到模型身上。
    """
    if not config.API_KEY:
        # 纯算法路径是**合法的降级模式**，不是错误：没配 key 的机器上功能
        # 也应该能跑（e2e 也依赖这一点）。
        return {}, "未配置文本模型，已按算法最优位置配置"

    wanted = {plan.target.number for plan in plans}
    reply = await text_generator.chat_json(
        _LAYOUT_SYSTEM, build_prompt(plans, trim_w, trim_h)
    )
    decisions = parse_decisions(reply)
    if set(decisions) == wanted:
        return decisions, ""

    logger.warning(
        "模型返回的页号 %s 与请求 %s 不一致，降级为逐页调用",
        sorted(decisions),
        sorted(wanted),
    )
    out: dict[int, Decision] = {}
    for plan in plans:
        number = plan.target.number
        single = await text_generator.chat_json(
            _LAYOUT_SYSTEM, build_prompt([plan], trim_w, trim_h)
        )
        picked = parse_decisions(single).get(number)
        if picked is not None:
            out[number] = picked
    return out, ""


# ---------------------------------------------------------------- 落笔


def _inner_norm(
    cand: layout_analysis.Candidate, padding_mm: float, box_w: float, box_h: float
) -> tuple[float, float, float, float]:
    """候选框**减去内边距**之后的那块，归一化。

    对比度要按"字形压在哪"来算，不是按整个框：框里可能一大半是内边距，而内边距
    压住的地方是插图的主体 —— 用整个框的均值会让守卫在该拦的时候放行。
    """
    pad_x = min(0.45, padding_mm / box_w) if box_w > 0 else 0.0
    pad_y = min(0.45, padding_mm / box_h) if box_h > 0 else 0.0
    w = max(0.02, cand.w - 2 * pad_x)
    h = max(0.02, cand.h - 2 * pad_y)
    x = min(cand.x + pad_x, cand.x + cand.w - w)
    y = min(cand.y + pad_y, cand.y + cand.h - h)
    return x, y, w, h


def _background_lum(book_id: str, plan: Plan, index: int) -> float:
    """文字真正压住的那块地方有多亮。读不出来就退回候选框自己的均值。"""
    cand = plan.candidates[index]
    _, _, box_w, box_h = plan.target.box
    rect = _inner_norm(cand, plan.target.padding_mm, box_w, box_h)
    lum = layout_analysis.region_luminance(
        book_id,
        plan.target.image_id,
        rect,
        box_w,
        box_h,
        plan.target.focus_x,
        plan.target.focus_y,
    )
    return cand.lum if lum is None else lum


def _choose(plan: Plan, decision: Decision | None) -> tuple[int, str, str]:
    """(用第几个候选, 模型给的颜色, 说明)。越界/没给就退回算法第一选择。"""
    if decision is None:
        return 0, "", "模型未响应，已按算法最优位置放置"
    if decision.index >= len(plan.candidates):
        # **不 clamp**：越界说明模型在瞎猜，夹到最后一个只会把文字贴到页角
        return 0, "", "模型给的位置越界，已按算法最优位置放置"
    return decision.index, decision.color, ""


def _apply(book_id: str, book: Book, plan: Plan, decision: Decision | None) -> tuple[int, str]:
    """把一页写进 book。返回 (页码, 说明)。**调用方负责加锁和落盘。**"""
    index, model_color, note = _choose(plan, decision)
    rect = plan.rects[index]
    fitted = plan.fitted[index]

    lum = _background_lum(book_id, plan, index)
    # 模型看不见图，颜色是凭亮度数字猜的，猜出"米色字压米色天空"很正常。
    # ensure_readable 是**守卫不是夺权**：达标就原样保留模型给的颜色。
    color = ensure_readable(model_color or auto_text_color(lum), lum)

    if fitted.overflow:
        note = "；".join(p for p in (note, "文字较多，已用最小字号") if p)

    page = book.pages[plan.target.number - 1]
    if isinstance(page, PresetPage):
        page.options.overlayRect = OverlayRect(
            xMm=rect[0], yMm=rect[1], widthMm=rect[2], heightMm=rect[3]
        )
        page.options.fontFamily = config.DEFAULT_FONT_FAMILY
        page.options.fontSizePt = fitted.fontSizePt
        page.options.lineHeight = fitted.lineHeight
        page.options.color = color
        # 居中：框是按这段文字反推的，字正好填满它，最后一行排不满时居中比
        # 左对齐更像有意为之。
        page.options.align = "center"
        return plan.target.number, note

    block = next(
        (b for b in page.blocks if b.id == plan.target.block_id and isinstance(b, TextBlock)),
        None,
    )
    if block is None:
        # 计划和落笔之间用户删了这一页的块。跳过这一页，别写坏别的东西。
        return plan.target.number, "文字块已不存在，这一页保持原样"

    block.xMm, block.yMm, block.widthMm, block.heightMm = rect
    # 用 model_copy 而不是新建 TextStyle：底衬（backgroundColor / borderRadiusMm）、
    # 垂直居中这些是**版式**，得原样留着 —— 新建会把它们全部重置成默认值，
    # 满版页的白板就没了。
    block.style = block.style.model_copy(
        update={
            "fontFamily": config.DEFAULT_FONT_FAMILY,
            "fontSizePt": fitted.fontSizePt,
            "lineHeight": fitted.lineHeight,
            "color": color,
            "align": "center",
            "autoFit": True,
        }
    )
    return plan.target.number, note


# ---------------------------------------------------------------- 任务编排


async def run_layout_job(job: job_manager.Job, book_id: str) -> None:
    """整本书一次智能配置。

    和 `run_illustrate_job` 的两处结构性差异，都是有意的：

    - **最后一次原子落盘**，不是逐页存。整件事是一次 LLM 调用加本地计算，
      逐页存 30 次没有收益，反而让"崩溃时写了一半"成为可能；一次性写 +
      一份快照，语义就是"要么全变要么全不变"，和撤销天然配对。
    - **没配 key 不 failed**。纯算法路径是合法的降级模式。
    """
    await job_manager.set_status(job, "running")

    book = book_store.load_or_none(book_id)
    if book is None:
        await job_manager.set_status(job, "failed", "书籍不存在")
        return

    targets, skips = applicable_pages(book)
    # 跳过的先标出来：进度条要显示**整本书**，否则"15/15 完成"而 5 页悄悄
    # 没动，用户会以为功能坏了。
    for number, reason in skips:
        await job_manager.mark(job, number, "skipped", error=reason)

    plans: list[Plan] = []
    for target in targets:
        plan = _plan(book_id, target)
        if not plan.candidates:
            # 有 imageId 但文件读不出来（被删了/截断了）。这一页没得配，但
            # 它是**跳过**不是失败 —— 其余页照配。
            skips.append((target.number, "插图读不出来"))
            await job_manager.mark(job, target.number, "skipped", error="插图读不出来")
            continue
        plans.append(plan)

    if not plans:
        await job_manager.set_status(job, "done", _done_message(0, skips))
        return

    try:
        decisions, note = await _ask_model(plans, book.trimSize.widthMm, book.trimSize.heightMm)
    except text_generator.TextGeneratorError as exc:
        # 传输层失败是**全局**问题：一个字节都还没写，整单失败是诚实的。
        # 静默降级会把"模型挂了"伪装成"模型没配"。
        await job_manager.set_status(job, "failed", f"文本模型调用失败：{exc}")
        return

    # 取消检查放在**写之前**：没写就不需要撤销。
    if job_manager.is_cancelled(job):
        await job_manager.set_status(job, "cancelled", "已取消")
        return

    async with book_store.lock_for(book_id):
        fresh = book_store.load_or_none(book_id)
        if fresh is None:
            await job_manager.set_status(job, "failed", "书籍不存在")
            return
        # 快照与写入在**同一个锁里**完成，杜绝"快照存了、书没写"的中间态。
        write_snapshot(book_id, fresh)
        results = [
            _apply(book_id, fresh, plan, decisions.get(plan.target.number))
            for plan in plans
        ]
        book_store.save(fresh)

    for number, item_note in results:
        await job_manager.mark(job, number, "done", error=item_note)

    message = _done_message(len(results), skips)
    if note:
        message = f"{message}。{note}"
    await job_manager.set_status(job, "done", message)


def _done_message(applied: int, skips: list[tuple[int, str]]) -> str:
    if not skips:
        return f"已智能配置 {applied} 页"
    return f"已智能配置 {applied} 页，跳过 {len(skips)} 页（{skip_summary(skips)}）"


# ---------------------------------------------------------------- 快照与撤销


def write_snapshot(book_id: str, book: Book) -> None:
    """把这本书**当前**的 pages 存一份，供「撤销本次智能配置」用。

    存 pages 而不是整本 book：撤销只该回退排版，不该顺手把用户期间改的标题、
    作者也回滚掉。

    **每次跑都覆盖**（只留一个槽）。按钮文案是「撤销**本次**智能配置」，语义
    就是回到最近一次运行之前：跑一次 → 手动调一页 → 再跑一次 → 撤销，应当
    恢复"手动调过的那一页"，而不是抹掉它回到最初。将来要做多级栈，把顶层从
    对象改成数组即可。
    """
    payload = {
        "createdAt": now_iso(),
        "pageCount": len(book.pages),
        "pages": [page.model_dump(mode="json") for page in book.pages],
    }
    book_store.atomic_write(
        paths.layout_undo_file(book_id), json.dumps(payload, ensure_ascii=False, indent=2)
    )


def read_snapshot(book_id: str) -> dict | None:
    """读快照。文件不在或坏掉都返回 None —— 撤销按钮据此隐藏。"""
    try:
        return json.loads(paths.layout_undo_file(book_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def snapshot_info(book_id: str) -> dict:
    """给 `GET /layout/undo` 用。

    这个端点的全部意义是让按钮**刷新页面后仍然正确** —— 按钮永远可见然后
    404 是很糟的体验。
    """
    snapshot = read_snapshot(book_id)
    if not snapshot:
        return {"available": False, "createdAt": None, "pages": 0}
    return {
        "available": True,
        "createdAt": snapshot.get("createdAt"),
        "pages": len(snapshot.get("pages") or []),
    }


def delete_snapshot(book_id: str) -> None:
    """撤销之后删掉。

    用户要的是"**一次**撤销"，不是 redo 栈：第二次点必然 404，而按钮此时
    已经隐藏了（`GET /layout/undo` 说没有了）。
    """
    try:
        paths.layout_undo_file(book_id).unlink()
    except OSError:
        pass


def restore_snapshot(book: Book, snapshot: dict) -> tuple[int, int]:
    """按 **page id** 把快照里的页贴回去，返回 (恢复了几页, 跳过了几页)。

    **绝不整体替换 `book.pages`**：用户可能在跑完之后删了页、加了页、重新
    分页、把某页转成了自由页 —— 整体替换会**删掉用户新建的页**，那是不可
    接受的数据丢失。快照里有同 id 的页就替换，没有就保留当前的。

    返回值里的"跳过"不是错误，是"这一页在快照里没有"。
    """
    saved = {
        page.get("id"): page
        for page in (snapshot.get("pages") or [])
        if isinstance(page, dict)
    }
    adapter = TypeAdapter(Page)
    restored = 0
    skipped = 0
    for index, page in enumerate(book.pages):
        raw = saved.get(page.id)
        if raw is None:
            skipped += 1
            continue
        try:
            book.pages[index] = adapter.validate_python(raw)
        except ValidationError as exc:
            # 快照是上个版本写的、模型变了 —— 宁可少恢复一页，也不能让整本书
            # 打不开。
            logger.warning("快照里的第 %d 页恢复失败: %s", index + 1, exc)
            skipped += 1
            continue
        restored += 1
    return restored, skipped
