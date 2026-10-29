"""找出插图里最"空"的一条横带，给满版叠字落脚。

生图提示词里已经要求"画面下方三分之一留白"了，但模型不一定听；用户换一张图，
留白的位置也跟着变。所以不猜，真去量一下：把图缩到很小，逐行算梯度能量，
再找一段最安静的横带。文字放进去，图还看得见，字也读得清。

**为什么要模拟 object-fit: cover 的裁切**：插图是 cover 进页面盒子的，宽高比
不一样时上下（或左右）会被裁掉。直接分析原图，找出来的"留白带"可能正好落在
被裁掉的那一条里 —— 文字放上去就压在主体上了。所以先按同样的规则裁一次，
再在**可见区域**里找。

这个函数每次渲染都会跑（预览 iframe 每存一次就重载一次），必须便宜：缩到
64px 宽之后一次不到 2ms。结果按图片路径缓存，图片文件是不可变的。
"""

from __future__ import annotations

import functools
from pathlib import Path

from PIL import Image

from app.storage import paths

# 分析用的缩略图宽度。64 足够看出"哪一片细节少"，再大只是更贵。
ANALYSIS_WIDTH = 64

# 横带占页面高度的比例。这就是文字带（白板）最终的高度 —— 板子撑满整条带子、
# 文字在里面居中。太窄塞不下几行字，太宽会盖住主体。
BAND_RATIO = 0.30

# 同样安静时优先靠下：绘本的惯例是文字在下，视线从图上移到字上也更顺。
# 只是很轻的先验（最多 10%），真有明显更空的区域时压不过它。
BOTTOM_PRIOR = 0.10


def _cover_crop(
    img_w: int, img_h: int, box_w: float, box_h: float, focus_x: float, focus_y: float
) -> tuple[int, int, int, int]:
    """object-fit: cover + object-position 下，图片**可见**的那块源区域。

    返回 (left, top, right, bottom)，单位是源图像素。
    """
    scale = max(box_w / img_w, box_h / img_h)
    visible_w = box_w / scale
    visible_h = box_h / scale
    # object-position 决定溢出量怎么分配：0% 全裁右边，100% 全裁左边
    left = (img_w - visible_w) * focus_x
    top = (img_h - visible_h) * focus_y
    return (
        round(left),
        round(top),
        round(left + visible_w),
        round(top + visible_h),
    )


def _row_energy(gray: Image.Image) -> list[float]:
    """每一行的"细节量"：横向与纵向相邻像素差的平均绝对值。

    梯度能量比方差更贴近"这里有没有东西"：一大片纯色天空梯度为零，而主体、
    纹理、泡泡的边缘都会把它顶起来。方差会被整体明暗误导。
    """
    width, height = gray.size
    px = gray.tobytes()  # 8 位灰度，行优先，比 getdata() 快一个数量级
    scores: list[float] = []

    for y in range(height):
        row = y * width
        total = 0
        count = 0
        for x in range(width - 1):
            total += abs(px[row + x + 1] - px[row + x])
            count += 1
        if y + 1 < height:
            below = (y + 1) * width
            for x in range(width):
                total += abs(px[below + x] - px[row + x])
                count += 1
        scores.append(total / count if count else 0.0)

    return scores


def _quietest_band(scores: list[float], band_ratio: float) -> tuple[float, float]:
    """在逐行分数里找最安静的窗口，返回归一化的 (top, height)。"""
    rows = len(scores)
    if rows == 0:
        return 0.0, band_ratio

    band = max(1, min(rows, round(rows * band_ratio)))
    # 位置先验：越靠下越"便宜"。只是轻微倾斜，压不过真实的细节差异。
    adjusted = [
        score * (1 - BOTTOM_PRIOR * (y / (rows - 1))) if rows > 1 else score
        for y, score in enumerate(scores)
    ]

    # 前缀和求滑动窗口最小值。窗口从下往上试、用严格小于，所以并列时靠下的赢。
    prefix = [0.0]
    for value in adjusted:
        prefix.append(prefix[-1] + value)

    best_top = 0
    best_total: float | None = None
    for top in range(rows - band, -1, -1):
        total = prefix[top + band] - prefix[top]
        if best_total is None or total < best_total:
            best_total = total
            best_top = top

    return best_top / rows, band / rows


@functools.lru_cache(maxsize=256)
def _band_for(image_path: str, mtime: float, box_w: float, box_h: float, fx: float, fy: float):
    del mtime  # 只用来让图片被改写时缓存失效
    with Image.open(image_path) as raw:
        gray = raw.convert("L")
        crop = _cover_crop(gray.width, gray.height, box_w, box_h, fx, fy)
        # 裁完可能只剩一条缝（极端宽高比），兜一下底
        if crop[2] - crop[0] < 2 or crop[3] - crop[1] < 2:
            return None
        gray = gray.crop(crop)

        height = max(8, round(ANALYSIS_WIDTH * gray.height / gray.width))
        gray = gray.resize((ANALYSIS_WIDTH, height), Image.BILINEAR)
        return _quietest_band(_row_energy(gray), BAND_RATIO)


def overlay_band(
    book_id: str,
    image_id: str | None,
    box_w: float,
    box_h: float,
    focus_x: float = 0.5,
    focus_y: float = 0.5,
) -> tuple[float, float] | None:
    """最安静的横带，归一化的 (top, height)，相对**成品区**。

    box_w / box_h 是插图实际被 cover 进去的盒子（成品尺寸，不含出血）——
    和渲染时用的是同一个，否则裁切对不上。

    没有插图、文件缺失或读不出来时返回 None，由调用方决定退路
    （目前是退回 bottom）。
    """
    if not image_id:
        return None

    path: Path = paths.image_file(book_id, image_id)
    try:
        stat = path.stat()
    except OSError:
        return None

    try:
        return _band_for(str(path), stat.st_mtime, box_w, box_h, focus_x, focus_y)
    except Exception:  # noqa: BLE001 —— 坏图/截断的 PNG 不该让整页渲染失败
        return None
