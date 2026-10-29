"""在插图里找"可以放字"的候选框，交给大模型挑一个。

和 `image_analysis.py` 的分工是**性能契约不同**，不是洁癖：

- `image_analysis.overlay_band()` 找的是**全宽横带**，每次渲染都要跑（预览 iframe
  每存一次盘就重载一次），所以缩到 64px 宽、逐行算标量、必须 <2ms。
- 这个模块找的是**二维的任意矩形**，只在智能排版任务里跑一次，量级 20ms。

**别把它接进渲染路径。** `renderer` / `presets` / `overlay_zone_mm` 一个都不许调
它 —— 那边只允许读**已经存下来的** `overlayRect`。这条一破，预览就会开始卡，
而且很难归因到是这里。

裁切复用 `image_analysis._cover_crop`：插图是 object-fit: cover 进页面的，宽高比
不一样时上下（或左右）会被裁掉。不复用就会出现"分析区域和实际可见区域不一致"
—— 找出来的空地可能整块都在被裁掉的那一条里。
"""

from __future__ import annotations

import functools
import math
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from app import config
from app.services import image_analysis
from app.services.image_analysis import _cover_crop, _quietest_band, _row_energy
from app.storage import paths

# 候选框高度的三档（占成品区高）。宽度由文字长度反推（见 _sizes），所以这里
# 只定高：同样的字数，矮框排得多行、宽框排得少行，三档给出"扁/中/方"三种选择。
_HEIGHT_RATIOS = (0.14, 0.20, 0.27)

# 用来反推宽度的行数档。按 2/3/4 行排，模型据此挑"文字排几行好看"。
_LINE_CHOICES = (2, 3, 4)

# 候选框宽度的上下限（占成品区宽）。太窄了放不下几个字。
_MIN_WIDTH_RATIO = 0.35

# text_len 为 0 时按多少个字算。调用方（智能排版任务）总是有正文的，
# 这个兜底只是让函数单独调用时也能返回点像样的东西。
_DEFAULT_TEXT_LEN = 30


@dataclass(frozen=True)
class Candidate:
    """一块"可以放字"的矩形。

    坐标是**归一化的 0~1，相对成品区**（和 `overlay_band` 的返回同一套），
    换算成 mm 是调用方的事 —— 见 `candidates_mm`。
    """

    x: float
    y: float
    w: float
    h: float
    # 越小越空。只在候选之间比较有意义，绝对值没有单位。
    score: float
    # "box" = 二维滑窗找出来的；"band" = 现有 overlay_band 那条全宽横带，
    # 塞进来是为了让"退化成今天的行为"永远是可选之一。
    kind: str
    # 平均亮度 0~1。给模型描述"这块地方是亮是暗"，它据此挑字色。
    lum: float
    # 归一化的平均细节量 0~1（1 = 全图最热闹的那 5%）。
    detail: float


# ---------------------------------------------------------------- 图像特征


def _p95(values: list[float], percentile: int) -> float:
    """分位数。空列表返回 0，全零也返回 0（调用方要防除零）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(len(ordered) * percentile / 100) - 1))
    return ordered[index]


def _feature_maps(img: Image.Image) -> tuple[list[float], list[float], list[float]]:
    """把 RGB 图拆成三张逐像素的归一化图：细节量、彩度、亮度。

    都归一到 0~1，这样下面那个加权和里的权重才可比、跨图也才可比
    （否则换一张整体对比度高的图，同一个权重会变成完全不同的意思）。
    """
    width, height = img.size
    rgb = img.tobytes()  # RGB，行优先，每像素 3 字节。比 getdata() 快一个数量级
    count = width * height

    lum = [0] * count
    sat = [0] * count
    for i in range(count):
        r = rgb[i * 3]
        g = rgb[i * 3 + 1]
        b = rgb[i * 3 + 2]
        # 整数版 Rec.601 亮度，和 PIL 的 convert("L") 口径一致
        lum[i] = (r * 299 + g * 587 + b * 114) // 1000
        sat[i] = max(r, g, b) - min(r, g, b)

    # 细节量：与右邻、下邻的亮度差之和。和 _row_energy 同一个口径，只是这里
    # 保留了二维 —— 横带的算法只需要逐行标量，找矩形必须逐像素。
    grad = [0] * count
    for y in range(height):
        row = y * width
        last = y == height - 1
        for x in range(width):
            i = row + x
            total = 0
            if x + 1 < width:
                total += abs(lum[i + 1] - lum[i])
            if not last:
                total += abs(lum[i + width] - lum[i])
            grad[i] = total

    g_scale = _p95(grad, config.LAYOUT_NORM_PERCENTILE) or 1.0
    s_scale = _p95(sat, config.LAYOUT_NORM_PERCENTILE) or 1.0
    return (
        [min(1.0, v / g_scale) for v in grad],
        [min(1.0, v / s_scale) for v in sat],
        [v / 255.0 for v in lum],
    )


def _integral(values: list[float], width: int, height: int) -> list[float]:
    """二维前缀和，返回 (width+1)×(height+1) 的扁平数组。

    建一次 O(像素数)，之后**任意矩形**的均值都是 O(1) —— 九种尺寸 × 几十个
    位置的穷举才不会变成几千次逐像素求和。
    """
    stride = width + 1
    acc = [0.0] * (stride * (height + 1))
    for y in range(height):
        row = y * width
        above = y * stride
        below = (y + 1) * stride
        running = 0.0
        for x in range(width):
            running += values[row + x]
            acc[below + x + 1] = acc[above + x + 1] + running
    return acc


def _rect_sum(acc: list[float], width: int, x0: int, y0: int, x1: int, y1: int) -> float:
    """[x0, x1) × [y0, y1) 的和。"""
    stride = width + 1
    return (
        acc[y1 * stride + x1]
        - acc[y0 * stride + x1]
        - acc[y1 * stride + x0]
        + acc[y0 * stride + x0]
    )


# ---------------------------------------------------------------- 候选尺寸


def _margins(box_w: float, box_h: float) -> tuple[float, float]:
    """候选框离成品区边缘的最小距离，归一化。

    上限卡在 0.45：自由页里插图块可能只有十几毫米高，8mm 的边距按比例算出来
    超过一半 —— 那会让可用区域变成负的、一个候选都生不出来。这种情况下降级成
    "留 10% 的边"总比没有强。
    """
    return (
        min(0.45, config.LAYOUT_MARGIN_MM / box_w),
        min(0.45, config.LAYOUT_MARGIN_MM / box_h),
    )


def _sizes(
    text_len: int, box_w: float, box_h: float, margin_x: float, margin_y: float
) -> list[tuple[float, float]]:
    """候选框尺寸，归一化相对成品区。

    **由文字长度反推**，不是拍脑袋定几档固定比例：同样 40 个字，排 2 行需要很宽
    的框，排 4 行需要接近方形的框。固定档位会出现"所有候选都放不下这段字"，
    然后只能挑一个最不坏的 —— 而按字数反推，每个候选天生就是"这段字排得下"
    的形状，模型才是在**都放得下**的框里挑好看的那个。
    """
    n = text_len if text_len > 0 else _DEFAULT_TEXT_LEN

    out: list[tuple[float, float]] = []
    for lines in _LINE_CHOICES:
        # 每行几个字 → 盒子的宽高比（中文一字宽 = 一个字号 = 1em）
        aspect = math.ceil(n / lines) / lines
        for h_ratio in _HEIGHT_RATIOS:
            h_ratio = min(h_ratio, 1 - 2 * margin_y)
            if h_ratio <= 0.02:
                continue
            h_mm = box_h * h_ratio
            w_mm = min(max(h_mm * aspect, _MIN_WIDTH_RATIO * box_w), box_w * (1 - 2 * margin_x))
            w_ratio = w_mm / box_w
            if (w_ratio, h_ratio) not in out:
                out.append((w_ratio, h_ratio))
    return out


def _score(mean_grad: float, mean_grad2: float, mean_sat: float, mean_lum: float, cy: float) -> float:
    """越小越空。每一项都在对付一种具体的误判，改权重前先看这里的理由。

    cy 是框中心的归一化纵坐标（0 = 顶，1 = 底）。
    """
    w = config.LAYOUT_COST
    # 方差 = E[x²] - E[x]²。数值误差可能让它掉到 -1e-12 这种，兜一下。
    spread = math.sqrt(max(0.0, mean_grad2 - mean_grad * mean_grad))
    return (
        w["detail"] * mean_grad
        + w["saturation"] * mean_sat
        + w["spread"] * spread
        + w["bright"] * (1 - mean_lum)
        + w["bottom"] * (1 - cy)
    )


def _iou(a: Candidate, b: Candidate) -> float:
    x0 = max(a.x, b.x)
    y0 = max(a.y, b.y)
    x1 = min(a.x + a.w, b.x + b.w)
    y1 = min(a.y + a.h, b.y + b.h)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    union = a.w * a.h + b.w * b.h - inter
    return inter / union if union > 0 else 0.0


def _slide(
    sums: tuple[list[float], list[float], list[float], list[float]],
    width: int,
    height: int,
    sizes: list[tuple[float, float]],
    margin_x: float,
    margin_y: float,
) -> list[Candidate]:
    """按尺寸档滑窗穷举，返回所有候选（未去重、未排序）。

    sums 是四张积分图 (grad, grad², sat, lum)，由调用方建好传进来 —— 横带那个
    候选也要用，建两遍是白费。

    窗口位置限制在 `LAYOUT_MARGIN_MM` 之内：贴着裁切线放字，印刷裁切一晃就
    切到笔画。字号被夹在 `[margin, 1-margin]` 里，所以框永远不越界。
    """
    i_grad, i_grad2, i_sat, i_lum = sums
    x_min = round(margin_x * width)
    y_min = round(margin_y * height)
    x_max = width - x_min  # 排他
    y_max = height - y_min

    out: list[Candidate] = []
    for w_ratio, h_ratio in sizes:
        bw = max(1, round(w_ratio * width))
        bh = max(1, round(h_ratio * height))
        if bw >= x_max - x_min or bh >= y_max - y_min:
            continue
        # 步长取盒子的一半：相邻窗口至少重叠一半，不会从缝里漏掉一块空地。
        step_x = max(1, bw // 2)
        step_y = max(1, bh // 2)
        for y in range(y_min, y_max - bh + 1, step_y):
            for x in range(x_min, x_max - bw + 1, step_x):
                area = bw * bh
                mean_grad = _rect_sum(i_grad, width, x, y, x + bw, y + bh) / area
                mean_grad2 = _rect_sum(i_grad2, width, x, y, x + bw, y + bh) / area
                mean_sat = _rect_sum(i_sat, width, x, y, x + bw, y + bh) / area
                mean_lum = _rect_sum(i_lum, width, x, y, x + bw, y + bh) / area
                cy = (y + bh / 2) / height
                out.append(
                    Candidate(
                        x=x / width,
                        y=y / height,
                        w=bw / width,
                        h=bh / height,
                        score=_score(mean_grad, mean_grad2, mean_sat, mean_lum, cy),
                        kind="box",
                        # 积分图是浮点累加，均值可能算出 1.0000000000000056 这种。
                        # 夹一下，别让"0~1"这条不变量在下游（提示词、JSON）里破掉。
                        lum=min(1.0, mean_lum),
                        detail=min(1.0, mean_grad),
                    )
                )
    return out


def _band_candidate(
    sums: tuple[list[float], list[float], list[float], list[float]],
    width: int,
    height: int,
    row_energy: list[float],
) -> Candidate:
    """现有 `overlay_band` 那条全宽横带，包成一个候选。

    直接复用 `_quietest_band`（逐行能量 → 最安静的窗口）而不是另写一套：这一条
    的意义正是"和今天渲染出来的一模一样"，自己实现一份就有漂移的余地了。
    横带是**全宽**的，比任何 box 候选都宽 —— 文字长的时候它反而更好用，
    所以它不是凑数的。
    """
    i_grad, i_grad2, i_sat, i_lum = sums
    top, band_h = _quietest_band(row_energy, image_analysis.BAND_RATIO)
    y0 = min(height - 1, max(0, round(top * height)))
    y1 = min(height, max(y0 + 1, round((top + band_h) * height)))
    area = width * (y1 - y0)
    mean_grad = _rect_sum(i_grad, width, 0, y0, width, y1) / area
    mean_grad2 = _rect_sum(i_grad2, width, 0, y0, width, y1) / area
    mean_sat = _rect_sum(i_sat, width, 0, y0, width, y1) / area
    mean_lum = _rect_sum(i_lum, width, 0, y0, width, y1) / area
    return Candidate(
        x=0.0,
        y=y0 / height,
        w=1.0,
        h=(y1 - y0) / height,
        score=_score(mean_grad, mean_grad2, mean_sat, mean_lum, (y0 + y1) / 2 / height),
        kind="band",
        lum=mean_lum,
        detail=mean_grad,
    )


def _nms(cands: list[Candidate]) -> list[Candidate]:
    """按分数升序去重。返回排好序的列表。

    排序用 `(score, x, y)` 而不是只按 score：分数并列时（大片均匀色块里很常见）
    只按 score 排的话，Python 的 sort 是稳定的、顺序取决于滑窗的遍历次序 ——
    结果能跑出来，但换个实现细节就变了。把坐标也编进键里，同样的输入永远给出
    同样的输出，测试才敢断言具体位置。
    """
    kept: list[Candidate] = []
    for cand in sorted(cands, key=lambda c: (c.score, c.x, c.y)):
        if all(_iou(cand, k) < config.LAYOUT_NMS_IOU for k in kept):
            kept.append(cand)
    return kept


# ---------------------------------------------------------------- 入口


@functools.lru_cache(maxsize=128)
def _candidates_for(
    image_path: str,
    mtime: float,
    box_w: float,
    box_h: float,
    fx: float,
    fy: float,
    text_bucket: int,
):
    del mtime  # 只用来让图片被改写时缓存失效

    with Image.open(image_path) as raw:
        img = raw.convert("RGB")
        crop = _cover_crop(img.width, img.height, box_w, box_h, fx, fy)
        # 裁完可能只剩一条缝（极端宽高比），兜一下底
        if crop[2] - crop[0] < 2 or crop[3] - crop[1] < 2:
            return []
        img = img.crop(crop)

        width = config.LAYOUT_ANALYSIS_WIDTH
        height = max(16, round(width * img.height / img.width))
        img = img.resize((width, height), Image.BILINEAR)

        grad, sat, lum = _feature_maps(img)
        sums = (
            _integral(grad, width, height),
            _integral([v * v for v in grad], width, height),
            _integral(sat, width, height),
            _integral(lum, width, height),
        )

        margin_x, margin_y = _margins(box_w, box_h)
        sizes = _sizes(text_bucket, box_w, box_h, margin_x, margin_y)

        # 亮度硬门槛逐级放宽：夜景那种整幅都暗的插图不该一页都配不出来。
        # 放宽过这件事不用额外标记 —— 候选自己的 lum 字段就是证据。
        boxes: list[Candidate] = []
        for min_bright in config.LAYOUT_MIN_BRIGHTNESS:
            boxes = [
                c
                for c in _slide(sums, width, height, sizes, margin_x, margin_y)
                if c.lum >= min_bright
            ]
            if boxes:
                break

        # 全宽横带：现有 overlay_band 的结果。它占一个**固定名额**，不参与 NMS
        # 的淘汰 —— 它是今天渲染出来的那一条，塞进来是为了让"退化成今天的
        # 行为"永远是可选之一（新功能严格是它的超集）。让 box 把它挤掉，就等于
        # 这个功能悄悄换掉了旧行为。
        band = _band_candidate(sums, width, height, _row_energy(img.convert("L")))

        # 反过来，横带也**不淘汰 box**。早先的版本是横带优先、和它 IoU 过半的
        # box 一律丢掉 —— 横带是全宽 30% 高的一大条，底部任何一条 box 跟它的
        # IoU 都在 0.45 以上，等于整条底边只剩横带一个候选。拿真书实测过：
        # 6 页里 4 页只剩横带，而其中两页有分数明显更好的窄框（0.28 vs 0.37），
        # 全被丢掉了 —— 那正是这个功能要解决的"文字压在主体上"。
        out = _nms(boxes)[: config.LAYOUT_MAX_CANDIDATES - 1]
        out.append(band)
        # 排序键带上坐标，理由见 _nms：分数并列时结果必须只由输入决定。
        return sorted(out, key=lambda c: (c.score, c.x, c.y))


def blank_candidates(
    book_id: str,
    image_id: str | None,
    box_w: float,
    box_h: float,
    focus_x: float = 0.5,
    focus_y: float = 0.5,
    *,
    text_len: int = 0,
    max_candidates: int | None = None,
) -> list[Candidate]:
    """插图上"可以放字"的候选框，按分数从好到差排。

    box_w / box_h 是插图实际被 cover 进去的盒子（成品尺寸，不含出血）—— 和渲染
    时用的是同一个，否则裁切对不上。

    没有插图、文件缺失或读不出来时返回**空列表**，绝不抛：与 `overlay_band`
    同纪律，由调用方决定退路。
    """
    if not image_id:
        return []

    path: Path = paths.image_file(book_id, image_id)
    try:
        stat = path.stat()
    except OSError:
        return []

    # 字数**必须分桶**再进缓存键：不分桶的话 text_len 是连续值，键空间无限大，
    # LRU 永远不命中，等于没缓存。
    try:
        out = list(
            _candidates_for(str(path), stat.st_mtime, box_w, box_h, focus_x, focus_y, text_len // 10 * 10)
        )
    except Exception:  # noqa: BLE001 —— 坏图/截断的 PNG 不该让整个任务崩掉
        return []

    if max_candidates is not None:
        out = out[:max_candidates]
    return out


def candidates_mm(
    cands: list[Candidate], box_x: float, box_y: float, box_w: float, box_h: float
) -> list[tuple[float, float, float, float]]:
    """归一化候选 → (x, y, w, h)，mm，**相对成品区**。

    给模型看的是 mm 不是 0~1：它对"页宽 200 毫米、框在 y=140 毫米"的推理，
    远好于对一串小数的推理。

    候选是相对**插图被 cover 进去的那个盒子**归一化的，所以除了尺寸还要加
    原点：满版页的盒子就是成品区（原点是 0,0），而自由页的图片块可能只占
    页面的一块，它的原点不是 0 —— 不加就会把框整体挪到左上角去。
    """
    return [
        (box_x + c.x * box_w, box_y + c.y * box_h, c.w * box_w, c.h * box_h)
        for c in cands
    ]


def region_luminance(
    book_id: str,
    image_id: str | None,
    rect_norm: tuple[float, float, float, float],
    box_w: float,
    box_h: float,
    focus_x: float = 0.5,
    focus_y: float = 0.5,
) -> float | None:
    """某个矩形区域（归一化，相对成品区）的平均亮度 0~1。

    字色的对比度守卫要它：模型看不见图，颜色是凭候选框的亮度数字猜的。
    读不出来返回 None。
    """
    if not image_id:
        return None
    path: Path = paths.image_file(book_id, image_id)
    try:
        stat = path.stat()
        return _luminance_for(str(path), stat.st_mtime, rect_norm, box_w, box_h, focus_x, focus_y)
    except Exception:  # noqa: BLE001
        return None


@functools.lru_cache(maxsize=128)
def _luminance_for(
    image_path: str,
    mtime: float,
    rect_norm: tuple[float, float, float, float],
    box_w: float,
    box_h: float,
    fx: float,
    fy: float,
) -> float | None:
    del mtime
    with Image.open(image_path) as raw:
        img = raw.convert("RGB")
        crop = _cover_crop(img.width, img.height, box_w, box_h, fx, fy)
        if crop[2] - crop[0] < 2 or crop[3] - crop[1] < 2:
            return None
        img = img.crop(crop)

        width = config.LAYOUT_ANALYSIS_WIDTH
        height = max(16, round(width * img.height / img.width))
        img = img.resize((width, height), Image.BILINEAR)
        _, _, lum = _feature_maps(img)

        x, y, w, h = rect_norm
        x0 = max(0, min(width - 1, int(x * width)))
        y0 = max(0, min(height - 1, int(y * height)))
        x1 = max(x0 + 1, min(width, int((x + w) * width)))
        y1 = max(y0 + 1, min(height, int((y + h) * height)))
        acc = _integral(lum, width, height)
        return _rect_sum(acc, width, x0, y0, x1, y1) / ((x1 - x0) * (y1 - y0))
