"""mm ↔ px 换算与 dpi 计算。

CSS 的 mm 单位在 Chromium 里固定映射为 96/25.4 px（约 3.7795），
预览和 PDF 用的是同一套数字，所以导出时不存在换算漂移 —— 这也是
全项目坚持用 mm 而不是 px 描述几何的原因。
"""

import math

MM_PER_INCH = 25.4
PX_PER_MM = 96 / MM_PER_INCH


def mm_to_px(mm: float) -> float:
    return mm * PX_PER_MM


def px_to_mm(px: float) -> float:
    return px / PX_PER_MM


def required_px(size_mm: float, dpi: int = 300) -> int:
    """物理尺寸在给定 dpi 下需要的像素数。"""
    return math.ceil(size_mm / MM_PER_INCH * dpi)


def effective_dpi(pixels: int, size_mm: float) -> float:
    """反算实际有效 dpi —— 图片像素不够时，用这个如实告诉用户。"""
    if size_mm <= 0:
        return 0.0
    return pixels / (size_mm / MM_PER_INCH)


def page_size_mm(trim_width_mm: float, trim_height_mm: float, bleed_mm: float) -> tuple:
    """含出血的整页物理尺寸。预览版 bleed 传 0。"""
    return (trim_width_mm + 2 * bleed_mm, trim_height_mm + 2 * bleed_mm)


# ---------------------------------------------------------------- 图片像素预算

# 印刷用的目标分辨率。**注意它落在图片上，不在页面上**：page.pdf() 出的是
# 矢量 PDF，文字是文字不是位图，提高 deviceScaleFactor 只会让截图更清晰，
# 对 PDF 里的文字毫无影响。真正需要 300dpi 的只有位图插图。
PRINT_DPI = 300
# 屏幕预览用的分辨率。屏幕上一英寸就是 96 个 CSS 像素，取 150 是留一点
# 余量给放大查看，同时把预览的图片体积压下来（首屏明显更快）。
SCREEN_DPI = 150


def box_pixels(width_mm: float, height_mm: float, dpi: int = PRINT_DPI) -> tuple:
    """一个盒子在给定 dpi 下需要的像素数。"""
    return required_px(width_mm, dpi), required_px(height_mm, dpi)


def resample_size(
    orig_width: int,
    orig_height: int,
    box_width_px: int,
    box_height_px: int,
    fit: str = "cover",
) -> tuple | None:
    """把图片缩到"刚好满足这个盒子"的像素数；不需要缩就返回 None。

    只会**缩小**，绝不放大 —— 放大既不会多出细节，又白白撑大 PDF。

    cover 与 contain 的判据不同，别写成一样：

    - cover：图要盖满盒子，所以两个方向都得够 —— 受限于相对不足的那一边，
      缩放系数取 min(原宽/盒宽, 原高/盒高)
    - contain：图整体放进盒子，渲染尺寸比盒子还小，只要**某一个**方向够
      就够清晰 —— 取 max
    """
    if orig_width <= 0 or orig_height <= 0 or box_width_px <= 0 or box_height_px <= 0:
        return None

    ratios = (orig_width / box_width_px, orig_height / box_height_px)
    scale = max(ratios) if fit == "contain" else min(ratios)
    if scale <= 1:
        return None

    return max(1, round(orig_width / scale)), max(1, round(orig_height / scale))


def placed_dpi(
    orig_width: int,
    orig_height: int,
    box_width_mm: float,
    box_height_mm: float,
    fit: str = "cover",
) -> float:
    """图片放进这个盒子后**实际**有多少 dpi。

    用于导出报告：图不够清晰时如实告诉用户"第 3 页有效 175dpi"，
    而不是假装它是 300dpi。

    与 resample_size 同一套判据：cover 取两方向中较差的那个（min），
    contain 取较好的那个（max）。
    """
    if orig_width <= 0 or orig_height <= 0 or box_width_mm <= 0 or box_height_mm <= 0:
        return 0.0

    dpi_w = effective_dpi(orig_width, box_width_mm)
    dpi_h = effective_dpi(orig_height, box_height_mm)
    return max(dpi_w, dpi_h) if fit == "contain" else min(dpi_w, dpi_h)


# 豆包硬下限：低于这个像素数直接报参数错误
DOUBAO_MIN_PIXELS = 3_686_400
# 通义 wan2.6-t2i 要求约 1280~1440 见方
TONGYI_MIN_SIDE = 1280
TONGYI_MAX_SIDE = 1440

# 尺寸取整到 16 的倍数，很多推理后端按 16 对齐
_ALIGN = 16


def _ratio_size(target_px: float, ratio: float) -> tuple:
    width = max(int(math.sqrt(target_px * ratio) // _ALIGN * _ALIGN), _ALIGN)
    height = max(int(math.sqrt(target_px / ratio) // _ALIGN * _ALIGN), _ALIGN)
    return width, height


def image_size_for_service(trim_width_mm: float, trim_height_mm: float, service: str) -> str:
    """把开本比例映射到生图服务能接受的尺寸。

    两家都有硬约束，传错直接报参数错误：
    - 豆包：至少 3,686,400 像素
    - 通义 wan2.6-t2i：约 1280×1280 ~ 1440×1440 之间，宽高比 1:4 ~ 4:1
    """
    if trim_width_mm <= 0 or trim_height_mm <= 0:
        raise ValueError(f"开本尺寸非法: {trim_width_mm}x{trim_height_mm}")

    ratio = trim_width_mm / trim_height_mm

    if service == "doubao":
        width, height = _ratio_size(DOUBAO_MIN_PIXELS, ratio)
        # 向下取整到 16 的倍数会掉到下限以下 —— A4/A5/A4L 竖版正好都中招，
        # 实测 1616×2272 = 3,671,552 就低于下限。每轮抬高 5% 重算，一般一两轮就够。
        target = float(DOUBAO_MIN_PIXELS)
        while width * height < DOUBAO_MIN_PIXELS:
            target *= 1.05
            width, height = _ratio_size(target, ratio)
        return f"{width}x{height}"

    return _tongyi_size(ratio)


def _tongyi_size(ratio: float) -> str:
    """通义只接受 1280~1440 的方框，能表达的比例被限制在 8:9 ~ 9:8。

    A4(0.707)、A5(0.705) 这类竖版开本的比例落不进这个区间，**无解**。
    所以先把它夹到最近的可行比例，再在方框里按这个比例取最大尺寸。

    不要改成"宽高各自 clamp 到区间"——那样 A5 会得到 1280×1440（比例 0.889），
    数字看着合法，但构图比开本扁了 26%，放进版式会被 object-fit: cover 裁掉
    一大块。宁可诚实地用最接近的比例，也不要一个歪掉的尺寸。
    """
    ratio = max(TONGYI_MIN_SIDE / TONGYI_MAX_SIDE, min(ratio, TONGYI_MAX_SIDE / TONGYI_MIN_SIDE))

    if ratio >= 1:
        width, height = TONGYI_MAX_SIDE, round(TONGYI_MAX_SIDE / ratio)
    else:
        width, height = round(TONGYI_MAX_SIDE * ratio), TONGYI_MAX_SIDE

    # 向下对齐到 16。1280 和 1440 本身都是 16 的倍数，所以夹回区间不会破坏对齐。
    width = max(TONGYI_MIN_SIDE, min(width // _ALIGN * _ALIGN, TONGYI_MAX_SIDE))
    height = max(TONGYI_MIN_SIDE, min(height // _ALIGN * _ALIGN, TONGYI_MAX_SIDE))
    return f"{width}x{height}"
