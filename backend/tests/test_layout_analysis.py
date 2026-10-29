"""空白候选框检测：在插图里找"可以放字"的矩形。

这个模块的产物会被大模型挑一个当落点，判错了就是文字压在主角脸上。所以这里
不测"函数有没有报错"，测**候选框落在哪一半**、以及"都平的时候挑哪个"。

用合成的图：一半噪声（"有东西"）一半纯色（"空"）。梯度能量对这两种输入的差别
是几十倍量级，所以断言可以写得很死。

`image_analysis` 那边只找**全宽横带**，这个模块要找**二维的任意矩形** —— 左半
有内容右半空，是横带算法做不到、这个模块存在的理由，所以单独钉一条。
"""

import random

import pytest
from PIL import Image

from app import config
from app.services import layout_analysis
from app.storage import paths

BOOK_ID = "book_testlayout"
IMAGE_ID = "p001_v1"
BOX = 400.0  # 分析用的盒子，正方形便于手算
TEXT_LEN = 30

# 噪声的种子。**必须固定** —— 噪声区的分数直接决定哪些框活过 NMS，用真随机
# （os.urandom）会让"活下来的框是哪些"每次都变，于是依赖这些框的断言就有了
# 随机的红/绿。这不是假想的风险：`test_a_better_box_...` 就这么以 3% 的概率
# 红过（60 次里红 2 次）。测试的红必须只由代码决定，不能由骰子决定。
NOISE_SEED = 20260924


@pytest.fixture(autouse=True)
def books_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BOOKS_DIR", tmp_path / "books")
    # 结果按 (路径, mtime, 盒子, 焦点, 字数桶) 缓存。同一个 book_id / image_id 在
    # 多个用例里复用，不清理的话第二条断言拿到的是上一条的图。
    layout_analysis._candidates_for.cache_clear()
    layout_analysis._luminance_for.cache_clear()
    yield
    layout_analysis._candidates_for.cache_clear()
    layout_analysis._luminance_for.cache_clear()


def _fill(px, x0, x1, y0, y1, color=None):
    """把 [x0,x1)×[y0,y1) 涂成纯色；color 为 None 时铺噪声（= "有东西"）。

    每次调用**重新播种**，而不是共用一个模块级 RNG：共用一个的话结果就跟
    "这个用例之前跑过哪些用例、各涂了几次"绑在一起，加一条用例就可能悄悄
    改掉另一条的图。逐次播种让每块噪声都只由 NOISE_SEED 决定。
    """
    rng = random.Random(NOISE_SEED)
    for y in range(y0, y1):
        if color is None:
            for x in range(x0, x1):
                px[x, y] = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
        else:
            for x in range(x0, x1):
                px[x, y] = color


def _put(image_id: str, size: tuple, paint) -> None:
    """生成一张图。paint(px) 负责往画布上涂东西。"""
    img = Image.new("RGB", size, (255, 255, 255))
    paint(img.load())
    paths.images_dir(BOOK_ID).mkdir(parents=True, exist_ok=True)
    img.save(paths.image_file(BOOK_ID, image_id))


def _cands(image_id, box_w=BOX, box_h=BOX, focus_x=0.5, focus_y=0.5, text_len=TEXT_LEN):
    return layout_analysis.blank_candidates(
        BOOK_ID, image_id, box_w, box_h, focus_x, focus_y, text_len=text_len
    )


def _best(image_id, **kwargs):
    cands = _cands(image_id, **kwargs)
    assert cands, "一个候选都没返回"
    return cands[0]


# ---------------------------------------------------------------- 落在哪


def test_candidate_lands_on_the_empty_half_sideways():
    """**左半有内容、右半纯色 → 最佳候选必须在右半。**

    这条是旧算法做不到、这个模块存在的理由：`overlay_band` 只找全宽横带，
    左右分的那一半它看不见。而绘本里"左边是主体、右边一片天"非常常见。
    """
    _put(IMAGE_ID, (400, 400), lambda px: _fill(px, 0, 200, 0, 400, None))

    best = _best(IMAGE_ID)

    assert best.kind == "box", f"挑中了横带（{best.kind}），横带是全宽的、必然压到左半"
    assert best.x >= 0.5, f"候选跑到有细节的那一半去了（x={best.x:.2f}）"


def test_candidate_lands_on_the_empty_half_sideways_whichever_it_is():
    """反过来也一样：空的在左半就该去左半。

    只测一种朝向的话，"永远返回右边"这种实现也能过。
    """
    _put(IMAGE_ID, (400, 400), lambda px: _fill(px, 200, 400, 0, 400, None))

    best = _best(IMAGE_ID)

    assert best.x + best.w <= 0.5 + 1e-6, (
        f"空的地方在左半，候选却跑右边去了（x={best.x:.2f}, w={best.w:.2f}）"
    )


def test_candidate_lands_on_the_empty_half_vertically():
    """上半噪声、下半纯色 → 最佳候选在下半张。"""
    _put(IMAGE_ID, (400, 400), lambda px: _fill(px, 0, 400, 0, 200, None))

    best = _best(IMAGE_ID)

    assert best.y >= 0.5, f"候选跑到有细节的那一半去了（y={best.y:.2f}）"


def test_candidate_does_not_touch_the_content_blocks():
    """四角各一块内容、其余全空 → 候选一块内容都不能压到。

    这条防的是"只要把框往空的地方推就赢"：白底图的细节量天生是 0，没有位置
    约束的话最优解会是一个横跨整幅、把四角都盖进去的大框（它的均值仍然很低，
    因为空的地方实在太多了）。所以要断言的是**不重叠**，不是"中心在中间" ——
    底部那条空当也是合法解，而且按"文字在下"的先验它还更好。
    """
    def paint(px):
        for x0, y0 in ((0, 0), (300, 0), (0, 300), (300, 300)):
            _fill(px, x0, x0 + 100, y0, y0 + 100, None)

    _put(IMAGE_ID, (400, 400), paint)

    best = _best(IMAGE_ID)

    # 四个噪声块在归一化坐标里各占 1/4 见方的角
    corners = [(0.0, 0.0, 0.25, 0.25), (0.75, 0.0, 1.0, 0.25),
               (0.0, 0.75, 0.25, 1.0), (0.75, 0.75, 1.0, 1.0)]
    for x0, y0, x1, y1 in corners:
        ox = min(best.x + best.w, x1) - max(best.x, x0)
        oy = min(best.y + best.h, y1) - max(best.y, y0)
        assert ox <= 1e-9 or oy <= 1e-9, (
            f"候选压到了角上的内容块 ({x0},{y0})-({x1},{y1})："
            f"框 x={best.x:.2f} y={best.y:.2f} w={best.w:.2f} h={best.h:.2f}"
        )


def test_fully_empty_image_prefers_the_bottom():
    """整张图都一样空时靠下 —— 绘本的惯例是文字在下。

    这条同时钉住"并列时靠下赢"：底部先验的系数改成 0 就会翻到页顶。
    """
    _put(IMAGE_ID, (400, 400), lambda px: _fill(px, 0, 400, 0, 400, (255, 255, 255)))

    best = _best(IMAGE_ID)

    assert best.y >= 0.6, f"整幅都空的时候候选该靠下，却在 y={best.y:.2f}"


def test_whiter_wins_when_both_are_empty():
    """都是平的，白的赢 —— 用户的原话是"浅色背景或者白色背景的空白位置"。

    梯度、彩度、标准差在这两张图上全都是 0，唯一能区分的只有亮度项。
    把 LAYOUT_COST 里的 "bright" 调成 0 这条就红。
    """
    _put("white", (400, 400), lambda px: _fill(px, 0, 400, 0, 400, (255, 255, 255)))
    _put("gray", (400, 400), lambda px: _fill(px, 0, 400, 0, 400, (128, 128, 128)))

    white = _best("white")
    gray = _best("gray")

    assert white.lum > 0.95
    assert gray.lum < 0.6
    assert white.score < gray.score, (
        f"中灰({gray.score:.4f}) 压过了全白({white.score:.4f})，亮度项没起作用"
    )


def test_analysis_respects_the_cover_crop():
    """只在**可见区域**里找留白，不能把裁掉的部分算进来。

    插图是 object-fit: cover 进页面的，宽高比不一致时上下会被裁掉。直接分析
    原图，找出来的"空地"可能整块都在被裁掉的那一条里 —— 文字放上去就压在主体
    上了，而且量渲染结果也看不出来（位置本身是"对"的，只是基于错误的前提）。
    """
    # 源图 400×800，focus_y=1.0 → 只看得见下面 400 行（400~800）。
    # 源图 0~400 全是噪声，但它整个被裁掉了，不该影响判断。
    _put(
        IMAGE_ID,
        (400, 800),
        lambda px: (_fill(px, 0, 400, 0, 400, None), _fill(px, 0, 400, 400, 800, (255, 255, 255))),
    )

    best = _best(IMAGE_ID, box_w=400, box_h=400, focus_y=1.0)

    assert best.lum > 0.9, (
        f"候选的亮度只有 {best.lum:.2f}，像是把裁掉的噪声那半张也算进去了"
    )


def test_dark_image_still_gets_candidates():
    """整幅都暗的插图（夜景）也要给出候选，不能一页都配不出来。

    亮度门槛是逐级放宽的，过不了 0.72 就试 0.55、0.40，最后不设限。放宽过这件事
    从候选自己的 lum 看得出来 —— 所以这里顺带断言 lum 确实低于第一档门槛。
    """
    _put("dark", (400, 400), lambda px: _fill(px, 0, 400, 0, 400, (30, 35, 60)))

    cands = _cands("dark")

    assert cands, "全暗的图一个候选都没有 —— 亮度门槛没有放宽"
    assert cands[0].lum < config.LAYOUT_MIN_BRIGHTNESS[0]


# ---------------------------------------------------------------- 形状与不变量


def test_candidates_are_within_bounds_and_not_overlapping():
    _put(IMAGE_ID, (400, 400), lambda px: _fill(px, 0, 200, 0, 400, None))

    cands = _cands(IMAGE_ID)

    assert len(cands) <= config.LAYOUT_MAX_CANDIDATES
    for c in cands:
        assert 0 <= c.x <= 1 and 0 <= c.y <= 1
        assert 0 < c.w <= 1 and 0 < c.h <= 1
        assert c.x + c.w <= 1 + 1e-6 and c.y + c.h <= 1 + 1e-6
        assert 0 <= c.lum <= 1 and 0 <= c.detail <= 1
    # 横带豁免去重（见 _candidates_for 里的说明），只查 box 两两不重叠
    boxes = [c for c in cands if c.kind == "box"]
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            assert layout_analysis._iou(a, b) < config.LAYOUT_NMS_IOU


def test_scores_are_sorted_best_first():
    _put(IMAGE_ID, (400, 400), lambda px: _fill(px, 0, 200, 0, 400, None))

    scores = [c.score for c in _cands(IMAGE_ID)]

    assert scores == sorted(scores), "候选没有按分数排 —— 提示词里的编号会失去意义"


def test_always_includes_the_current_band_behaviour():
    """横带候选永远在列表里 —— 新功能是旧行为的超集，不是把它换掉。

    长文字在横带里排得开、在任何 box 里都排不开，所以这一条不是凑数的。
    横带占固定名额、不参与去重，所以"永远"是字面意思：哪怕它分数最差、
    哪怕有更好的框整块压在它上面，它也还在。
    """
    _put(IMAGE_ID, (400, 400), lambda px: _fill(px, 0, 200, 0, 400, None))

    cands = _cands(IMAGE_ID)
    bands = [c for c in cands if c.kind == "band"]

    assert len(bands) == 1
    assert bands[0].x == 0.0 and bands[0].w == 1.0, "横带必须是全宽的"


def test_a_better_box_is_not_thrown_away_for_overlapping_the_band():
    """和横带重叠的好框必须留下，而且能排到横带前面。

    这条钉的是一个真实踩过的坑：早先的实现里横带优先，和它 IoU ≥ 0.35 的 box
    一律丢掉。横带是全宽 30% 高的一大条，而"左下角一小块"那种窄框恰好全在底边
    —— 真书上 6 页有 4 页退化成只剩横带。

    合成图刻意造成"一条干净的窄带夹在两条噪声之间"：横带高 30%、必然横跨噪声，
    而它下沿那条 14% 高的窄带是干净的。于是好框和横带**必然**重叠，而这正是
    旧实现丢掉它们的那种情况。

    **这条断言依赖"活过 NMS 的框里有够宽的"。** 框和横带的 IoU 强烈依赖框有多宽：
    0.66 宽的框落在横带里是 `0.66×0.14/0.30 ≈ 0.31` 起步，而 `_MIN_WIDTH_RATIO`
    那一档的 0.35 宽窄框只有 `≈ 0.16`，够不到 NMS 阈值。这段合成图里恰好有一个
    0.66 宽的框活下来（IoU 0.45）。所以噪声**必须**是固定种子的 —— 用
    `os.urandom` 时"活下来的是哪些框"每次重掷，这条断言就变成掷骰子：实测
    60 次里红 2 次。它红了先看 `NOISE_SEED` 还在不在，再怀疑算法。
    """
    def paint(px):
        _fill(px, 0, 400, 0, 280, None)       # 上 70% 噪声
        _fill(px, 0, 400, 280, 344, (255, 255, 255))  # 中间 14% 干净
        _fill(px, 0, 400, 344, 400, None)     # 下 14% 噪声

    _put(IMAGE_ID, (400, 400), paint)

    cands = _cands(IMAGE_ID)
    band = next(c for c in cands if c.kind == "band")
    boxes = [c for c in cands if c.kind == "box"]
    overlapping = [
        c for c in boxes if layout_analysis._iou(c, band) >= config.LAYOUT_NMS_IOU
    ]

    assert cands[0].kind == "box" and cands[0].score < band.score, (
        f"横带（{band.score:.3f}）压过了干净的框（{cands[0].score:.3f}）"
    )
    assert overlapping, (
        "和横带重叠的框一个都没留下 —— 干净的窄带被横带吃掉了。"
        f"横带 y={band.y:.2f} h={band.h:.2f}，留下的框：" +
        str([(round(c.x, 2), round(c.y, 2), round(c.h, 2)) for c in boxes])
    )


def test_same_input_gives_same_output():
    """同样的图跑两次要完全一样。

    NMS 里用 set/dict 迭代序、或者只按 score 排序（并列时靠 sort 的稳定性兜底）
    都会让结果随实现细节漂 —— 那时候"挑了个框"这件事就没法复现、也没法测。
    """
    _put(IMAGE_ID, (400, 400), lambda px: _fill(px, 0, 400, 0, 400, (200, 200, 200)))

    first = _cands(IMAGE_ID)
    layout_analysis._candidates_for.cache_clear()  # 绕开缓存，真的再算一遍
    second = _cands(IMAGE_ID)

    assert first == second


def test_candidates_mm_maps_normalized_to_the_trim_box():
    _put(IMAGE_ID, (400, 400), lambda px: _fill(px, 0, 200, 0, 400, None))

    cands = _cands(IMAGE_ID)
    mm = layout_analysis.candidates_mm(cands, 0.0, 0.0, 200.0, 200.0)

    assert len(mm) == len(cands)
    for cand, (x, y, w, h) in zip(cands, mm):
        assert x == pytest.approx(cand.x * 200.0)
        assert h == pytest.approx(cand.h * 200.0)


def test_candidates_mm_offsets_by_the_image_box_origin():
    """自由页的图片块不一定从页面左上角开始。

    候选是相对**插图盒子**归一化的，换算成页面坐标时不加原点，整块框会平移到
    左上角去 —— 于是"文字落在插图空白处"变成"文字落在页面左上角"。
    """
    _put(IMAGE_ID, (400, 400), lambda px: _fill(px, 0, 200, 0, 400, None))

    cands = _cands(IMAGE_ID)
    mm = layout_analysis.candidates_mm(cands, 30.0, 80.0, 200.0, 200.0)

    for cand, (x, y, w, h) in zip(cands, mm):
        assert x == pytest.approx(30.0 + cand.x * 200.0)
        assert y == pytest.approx(80.0 + cand.y * 200.0)
        assert w == pytest.approx(cand.w * 200.0)


# ---------------------------------------------------------------- 退路


@pytest.mark.parametrize("image_id", [None, "p404_missing"])
def test_missing_image_returns_empty(image_id):
    """没图 / 文件不在 → 空列表，由调用方决定退路。不能抛。"""
    assert _cands(image_id) == []


def test_corrupt_image_returns_empty():
    """截断的 PNG 不该把整个智能排版任务带崩。"""
    paths.images_dir(BOOK_ID).mkdir(parents=True, exist_ok=True)
    paths.image_file(BOOK_ID, IMAGE_ID).write_bytes(b"\x89PNG\r\n\x1a\n" + b"garbage")

    assert _cands(IMAGE_ID) == []


def test_region_luminance_reads_the_right_area():
    """对比度守卫要靠它判断"字压的这块地方有多亮"，量错地方就等于没守。"""
    def paint(px):
        _fill(px, 0, 400, 0, 200, (0, 0, 0))  # 上半涂黑
        _fill(px, 0, 400, 200, 400, (255, 255, 255))  # 下半留白

    _put(IMAGE_ID, (400, 400), paint)

    top = layout_analysis.region_luminance(BOOK_ID, IMAGE_ID, (0.0, 0.0, 1.0, 0.4), BOX, BOX)
    bottom = layout_analysis.region_luminance(BOOK_ID, IMAGE_ID, (0.0, 0.6, 1.0, 0.4), BOX, BOX)

    assert top is not None and bottom is not None
    assert top < 0.15, f"上半是黑的，读出来却是 {top:.2f}"
    assert bottom > 0.9, f"下半是白的，读出来却是 {bottom:.2f}"


def test_region_luminance_missing_image_returns_none():
    assert layout_analysis.region_luminance(BOOK_ID, "p404_missing", (0, 0, 1, 1), BOX, BOX) is None
