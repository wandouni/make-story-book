"""生图尺寸映射测试。

这里守的是一条会直接炸在生产路径上的不变量：给生图服务传了越界尺寸，
API 会报参数错误而不是返回一张差一点的图。所以每个开本、每个服务都要
逐个验，不能只测一个 A4。
"""

import pytest

from app.config import TRIM_SIZES
from app.render import geometry

# 通义 wan2.6-t2i 文档写的是宽高比 1:4~4:1，但那是**单独**看比例时的限制。
# 边长同时被夹在 1280~1440，两条约束叠起来，真正能表达的比例只有 8:9~9:8。
# 下面这两个是叠加后的实际可行带 —— 拿 1/4 去断言会得到错误的期望值。
TONGYI_MIN_RATIO = geometry.TONGYI_MIN_SIDE / geometry.TONGYI_MAX_SIDE
TONGYI_MAX_RATIO = geometry.TONGYI_MAX_SIDE / geometry.TONGYI_MIN_SIDE


def _parse(size: str) -> tuple:
    width, height = size.split("x")
    return int(width), int(height)


@pytest.mark.parametrize("trim_id", list(TRIM_SIZES))
def test_doubao_meets_minimum_pixels(trim_id: str):
    """豆包下限是硬约束，取整到 16 的倍数很容易掉下去 —— 这就是本测试的由来。"""
    trim = TRIM_SIZES[trim_id]
    width, height = _parse(
        geometry.image_size_for_service(trim["widthMm"], trim["heightMm"], "doubao")
    )
    assert width * height >= geometry.DOUBAO_MIN_PIXELS, (
        f"{trim_id} 得到 {width}x{height} = {width * height:,}，"
        f"低于豆包下限 {geometry.DOUBAO_MIN_PIXELS:,}"
    )


@pytest.mark.parametrize("trim_id", list(TRIM_SIZES))
def test_tongyi_within_side_limits(trim_id: str):
    trim = TRIM_SIZES[trim_id]
    width, height = _parse(
        geometry.image_size_for_service(trim["widthMm"], trim["heightMm"], "tongyi")
    )
    assert geometry.TONGYI_MIN_SIDE <= width <= geometry.TONGYI_MAX_SIDE
    assert geometry.TONGYI_MIN_SIDE <= height <= geometry.TONGYI_MAX_SIDE


@pytest.mark.parametrize("trim_id", list(TRIM_SIZES))
def test_doubao_aspect_ratio_preserved(trim_id: str):
    """豆包只限像素总数不限比例，所以必须精确复现开本比例。"""
    trim = TRIM_SIZES[trim_id]
    width, height = _parse(
        geometry.image_size_for_service(trim["widthMm"], trim["heightMm"], "doubao")
    )
    expected = trim["widthMm"] / trim["heightMm"]
    assert width / height == pytest.approx(expected, rel=0.02), f"{trim_id} 比例跑偏"


@pytest.mark.parametrize("trim_id", list(TRIM_SIZES))
def test_tongyi_aspect_ratio_is_clamped_not_distorted(trim_id: str):
    """通义的比例必须落在可行区间内，且是**最接近**开本的那一个。

    注意这里比的是夹取后的期望值，不是开本原始比例 —— A4/A5 的竖版比例
    在 1280~1440 的方框里无解，能做到的最好结果就是贴着边界。
    """
    trim = TRIM_SIZES[trim_id]
    width, height = _parse(
        geometry.image_size_for_service(trim["widthMm"], trim["heightMm"], "tongyi")
    )
    ratio = width / height

    assert TONGYI_MIN_RATIO <= ratio <= TONGYI_MAX_RATIO

    raw = trim["widthMm"] / trim["heightMm"]
    clamped = max(TONGYI_MIN_RATIO, min(raw, TONGYI_MAX_RATIO))
    assert ratio == pytest.approx(clamped, rel=0.02), f"{trim_id} 没有取最接近的可行比例"


@pytest.mark.parametrize("trim_id", list(TRIM_SIZES))
def test_dimensions_aligned_to_16(trim_id: str):
    """尺寸按 16 对齐是推理后端的普遍要求。"""
    trim = TRIM_SIZES[trim_id]
    for service in ("doubao", "tongyi"):
        width, height = _parse(
            geometry.image_size_for_service(trim["widthMm"], trim["heightMm"], service)
        )
        assert width % 16 == 0 and height % 16 == 0


def test_rejects_degenerate_trim():
    """0 或负数开本要当场报错，不能算出个莫名其妙的比例发给 API。"""
    with pytest.raises(ValueError):
        geometry.image_size_for_service(0, 297, "doubao")
    with pytest.raises(ValueError):
        geometry.image_size_for_service(210, -1, "doubao")


def test_extreme_ratios_still_valid_for_doubao():
    """极端长条开本也不能跌破下限 —— while 循环必须收敛。"""
    for width_mm, height_mm in ((100, 400), (400, 100), (50, 500)):
        px = _parse(geometry.image_size_for_service(width_mm, height_mm, "doubao"))
        assert px[0] * px[1] >= geometry.DOUBAO_MIN_PIXELS
