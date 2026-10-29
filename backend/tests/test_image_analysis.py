"""叠字自动定位：找出插图里最空的一条横带。

这个模块决定"文字落在哪里"，判错了就是文字压在主角脸上 —— 属于用户一眼
就能看见、但单元测试很容易漏掉的一类 bug（分析跑通了、返回了个数、就是位置
不对）。所以这里不测"函数有没有报错"，测**带子落在哪一半**。

用合成的图：一半噪声（"有东西"）一半纯色（"空"）。梯度能量对这两种输入的
差别是几十倍量级，所以断言可以写得很死。
"""

import random

import pytest
from PIL import Image

from app import config
from app.services import image_analysis
from app.storage import paths

BOOK_ID = "book_testanalysis"
IMAGE_ID = "p001_v1"
BOX = 400.0  # 分析用的盒子，正方形便于手算

# 噪声用固定种子。这个文件里的断言依赖的是"噪声 vs 纯色"的行能量差（几十倍
# 量级），实测 100 次没有飘过 —— 不像 test_layout_analysis.py 那条靠 IoU 的
# 断言。但"测试的红绿必须只由代码决定"没有理由在这里开例外：真随机意味着
# 哪天断言收紧了，它会以一个小概率间歇性变红，而那种红最难查。
NOISE_SEED = 20260924


@pytest.fixture(autouse=True)
def books_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BOOKS_DIR", tmp_path / "books")
    # 结果按 (路径, mtime, 盒子, 焦点) 缓存。同一个 book_id / image_id 在多个
    # 用例里复用，不清理的话第二条断言拿到的是上一条的图。
    image_analysis._band_for.cache_clear()
    yield
    image_analysis._band_for.cache_clear()


def _put(image_id: str, size: tuple, bands: list[tuple[int, int, bool]]) -> None:
    """生成一张图。bands 是 (起始行, 结束行, 是否噪声)，按顺序铺满整列。

    每次调用重新播种（而不是共用一个模块级 RNG）：共用的话结果就跟"这个用例
    之前跑过哪些用例、各铺了几行噪声"绑在一起，加一条用例就可能悄悄改掉另一条
    的图。逐次播种让每张图都只由 NOISE_SEED 决定。
    """
    width, height = size
    rng = random.Random(NOISE_SEED)
    img = Image.new("RGB", size, (128, 128, 128))
    px = img.load()
    for top, bottom, noisy in bands:
        for y in range(top, bottom):
            if noisy:
                for x in range(width):
                    px[x, y] = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
            else:
                for x in range(width):
                    px[x, y] = (128, 128, 128)
    paths.images_dir(BOOK_ID).mkdir(parents=True, exist_ok=True)
    img.save(paths.image_file(BOOK_ID, image_id))


def _band(image_id: str, box_w: float = BOX, box_h: float = BOX, focus_y: float = 0.5):
    return image_analysis.overlay_band(BOOK_ID, image_id, box_w, box_h, 0.5, focus_y)


def test_band_lands_on_the_empty_half():
    """上半张是噪声、下半张是纯色 → 带子必须在下半张。"""
    _put(IMAGE_ID, (400, 400), [(0, 200, True), (200, 400, False)])

    top, height = _band(IMAGE_ID)

    assert height == pytest.approx(image_analysis.BAND_RATIO, abs=0.02)
    assert top >= 0.5, f"带子跑到有细节的那一半去了（top={top:.2f}）"
    assert top + height <= 1.0 + 1e-6


def test_band_lands_on_the_empty_half_whichever_it_is():
    """反过来也一样：空的地方在上半张就该去上半张。

    只测一种朝向的话，"永远返回贴底"这种实现也能过 —— 而那正好是这个
    功能最想避免的行为（生图提示词已经要求下方留白了，分析要是也只会贴底，
    等于没分析）。
    """
    _put(IMAGE_ID, (400, 400), [(0, 200, False), (200, 400, True)])

    top, _ = _band(IMAGE_ID)

    assert top + image_analysis.BAND_RATIO <= 0.5 + 0.02, (
        f"空的地方在上半张，带子却跑下去了（top={top:.2f}）"
    )


def test_fully_empty_image_prefers_the_bottom():
    """整张图都一样空时贴底 —— 绘本的惯例是文字在下。

    这条同时钉住"并列时靠下赢"：实现里窗口是从下往上试、用严格小于比较的，
    改成 <= 就会悄悄翻到页顶。
    """
    _put(IMAGE_ID, (400, 400), [(0, 400, False)])

    top, height = _band(IMAGE_ID)

    assert top == pytest.approx(1 - image_analysis.BAND_RATIO, abs=0.03)
    assert top + height == pytest.approx(1.0, abs=0.03)


def test_analysis_respects_the_cover_crop():
    """只在**可见区域**里找留白，不能把裁掉的部分算进来。

    插图是 object-fit: cover 进页面的，宽高比不一致时上下会被裁掉。直接分析
    原图，找出来的"留白带"可能正好落在被裁掉的那一条里 —— 文字放上去就压在
    主体上了，而且量渲染结果也看不出来（位置本身是"对"的，只是基于错误的前提）。
    """
    # 源图 400×800，focus_y=1.0 → 只看得见下面 400 行（400~800）。
    # 源图 0~400 全是纯色，但它整个被裁掉了，不该影响判断。
    _put(IMAGE_ID, (400, 800), [(0, 400, False), (400, 600, True), (600, 800, False)])

    top, _ = _band(IMAGE_ID, box_w=400, box_h=400, focus_y=1.0)

    # 可见区（源图 400~800）里噪声在上半、纯色在下半 → 带子该落在可见区的下部
    assert top > 0.55, (
        f"带子落在 {top:.2f}，像是把裁掉的那半张也算进去了（可见区下部才是空的）"
    )


@pytest.mark.parametrize("image_id", [None, "p404_missing"])
def test_missing_image_returns_none(image_id):
    """没图 / 文件不在 → None，由调用方决定退路（目前是退回贴底）。

    不能抛异常：预览 iframe 每次存盘都重载一次，一张坏图不该让整页渲染 500。
    """
    assert _band(image_id) is None


def test_corrupt_image_returns_none():
    """截断的 PNG 不该把渲染带崩。"""
    paths.images_dir(BOOK_ID).mkdir(parents=True, exist_ok=True)
    paths.image_file(BOOK_ID, IMAGE_ID).write_bytes(b"\x89PNG\r\n\x1a\n" + b"garbage")

    assert _band(IMAGE_ID) is None
