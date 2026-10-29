"""插图资源端点：按放置尺寸重采样。

这是"300dpi"唯一真正落地的地方 —— PDF 是**矢量**的，提高
deviceScaleFactor 对里面的文字毫无作用，真正需要像素的只有位图插图。
所以这条链路值得单独钉死：

- **只缩不放**。放大不会多出细节，只会把 PDF 撑大
- **cover 取两轴较小者**：图要盖满盒子，最差的那一轴说了算
- **contain 取较大者**：图整体放进盒子，够清晰的那一轴就够
- 缓存键要带盒子尺寸和 fit：同一张图在不同版式下要的像素数不同
  （满版 A5 要 1749×2501，半页只要 1749×1365），只按 image_id 存会串
"""

import io
import os

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import config
from app.main import app
from app.storage import paths

BOOK_ID = "book_testassets"
IMAGE_ID = "p001_v1"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BOOKS_DIR", tmp_path / "books")
    with TestClient(app) as c:
        yield c


@pytest.fixture
def image(client) -> str:
    """放一张真图进去。用噪声而不是纯色 —— 纯色会被 JPEG 压成几百字节，
    任何"体积变小了"的断言都会变得毫无意义。"""
    return _put_image(BOOK_ID, IMAGE_ID, (2000, 1000))


def _put_image(book_id: str, image_id: str, size: tuple) -> str:
    """放一张真图进去。

    必须是**真噪声**：纯色或规则条纹会被 PNG 压到比缩放后的 JPEG 还小，
    "重采样让体积变小"这类断言就变成了在测压缩算法的运气。
    """
    paths.images_dir(book_id).mkdir(parents=True, exist_ok=True)
    img = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    img.save(paths.image_file(book_id, image_id))
    return image_id


def _fetch(client, box: str, fit: str = "cover", image_id: str = IMAGE_ID):
    return client.get(
        f"/api/assets/books/{BOOK_ID}/images/{image_id}.jpg", params={"box": box, "fit": fit}
    )


def _size(resp) -> tuple:
    with Image.open(io.BytesIO(resp.content)) as img:
        return img.size


# ---------------------------------------------------------------- 缩放方向


def test_resizes_down_to_the_box(client, image):
    """图比盒子大 → 缩到刚够盒子。

    2000×1000 放进 500×500，cover 要求两个方向都盖住：
    宽要缩 4 倍、高要缩 2 倍，取小的那个（2 倍）才能保证高度也盖住。
    """
    resp = _fetch(client, "500x500")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/jpeg"
    assert _size(resp) == (1000, 500)


def test_contain_keeps_the_whole_image(client, image):
    """contain 只要一个方向够清晰 —— 取较大的比例，图整体放进盒子。

    同一个盒子同一个源图，contain 得到的结果必须比 cover 小。
    """
    resp = _fetch(client, "500x500", fit="contain")
    assert _size(resp) == (500, 250)


def test_never_upscales(client, image):
    """盒子比图还大时原样返回。

    放大不会多出任何细节，只会让 PDF 里塞一张插值出来的糊图 ——
    这种情况要如实让用户看到"有效 dpi 不够"，而不是悄悄补像素。
    """
    resp = _fetch(client, "4000x4000")
    assert resp.status_code == 200
    assert _size(resp) == (2000, 1000)


def test_square_box_on_wide_image(client, image):
    """两轴比例差距大时，cover 的判据是"最差的那一轴"。

    2000×1000 → 800×800：宽缩 2.5 倍、高缩 1.25 倍，取 1.25 才盖得住高度，
    所以结果是 1600×800（比盒子大是正常的，浏览器再裁）。
    """
    assert _size(_fetch(client, "800x800")) == (1600, 800)


# ---------------------------------------------------------------- 缓存


def test_cache_key_includes_box_and_fit(client, image):
    """同一个 image_id 在不同版式下要的像素数不同，缓存不能互相覆盖。"""
    _fetch(client, "500x500", "cover")
    _fetch(client, "500x500", "contain")
    _fetch(client, "300x300", "cover")

    cached = sorted(p.name for p in paths.images_dir(BOOK_ID, ).joinpath(".resized").glob("*.jpg"))
    assert len(cached) == 3, cached
    assert any("500x500_cover" in n for n in cached)
    assert any("500x500_contain" in n for n in cached)
    assert any("300x300_cover" in n for n in cached)


def test_second_request_hits_the_cache(client, image):
    """重采样是导出路径上最贵的一步，不能每次请求都重算。"""
    first = _fetch(client, "500x500")
    cache = paths.image_cache_file(BOOK_ID, IMAGE_ID, 500, 500, "cover")
    stamp = cache.stat().st_mtime_ns

    second = _fetch(client, "500x500")
    assert second.content == first.content
    assert cache.stat().st_mtime_ns == stamp, "缓存文件被重写了"


def test_resized_is_smaller_than_the_source(client, image):
    source = paths.image_file(BOOK_ID, IMAGE_ID)
    assert len(_fetch(client, "500x500").content) < source.stat().st_size


# ---------------------------------------------------------------- 坏输入


@pytest.mark.parametrize("box", ["abc", "500", "500x", "x500", "0x100", "100x0", "999999x100"])
def test_rejects_bad_box(client, image, box):
    """畸形 box 要在解析阶段挡住，不能拿去做尺寸计算。

    999999 那条走的是 _MAX_SIDE 上限 —— 不挡的话一次请求就能把内存吃光。
    """
    assert _fetch(client, box).status_code == 400


def test_rejects_bad_fit(client, image):
    assert _fetch(client, "500x500", fit="stretch").status_code == 422


def test_missing_image_is_404(client):
    assert _fetch(client, "500x500").status_code == 404


def test_corrupt_image_falls_back_to_the_original(client):
    """图坏了不该让整本书导不出来 —— 退回原图，PDF 里至少有个东西。"""
    paths.images_dir(BOOK_ID).mkdir(parents=True, exist_ok=True)
    paths.image_file(BOOK_ID, "broken").write_bytes(b"this is not a png")

    resp = _fetch(client, "500x500", image_id="broken")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"


# ---------------------------------------------------------------- 原图端点


def test_original_png_endpoint_serves_full_size(client, image):
    """缩略图走原图 —— 前端的小图不需要重采样，让它自己去缩放。"""
    resp = client.get(f"/api/assets/books/{BOOK_ID}/images/{IMAGE_ID}.png")
    assert resp.status_code == 200
    assert _size(resp) == (2000, 1000)


@pytest.mark.parametrize("image_id", ["..", ".hidden", "bad%20id", "x" * 65])
def test_illegal_image_id_is_rejected(client, image_id):
    """挡住 ../ 之类的输入 —— safe_id 只认字母数字和 -_。

    注意带 %2F 的路径（`..%2F..%2Fbook.json.png`）压根匹配不到这条路由：
    路径参数不含 /，所以它在更前面就 404 了。这里测的是**能进到处理函数**
    的那一类，两处防线各管一段。
    """
    resp = client.get(f"/api/assets/books/{BOOK_ID}/images/{image_id}.png")
    assert resp.status_code == 400, resp.text
