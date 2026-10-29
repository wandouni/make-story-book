"""3D 翻页纹理的缓存键测试。

`shot_page` 本身要开 Chromium，归 `scripts/e2e_preview.py` 管；这里测的是
**缓存键**（`fingerprint` 和 `page_shot_file`），因为它的失效是静默的：

文件名只按页码取的话，用户改完文字再进 3D 预览，看到的还是上一版的画面 ——
而那一版画面本身是"对"的（排版正常、字也清楚），只是内容是旧的。这种错
不报异常、不崩溃，肉眼还容易以为是缓存没刷新，所以每一条都值得钉住。
"""

import pytest

from app.models.book import Book, FreePage, PresetPage, TextBlock, TrimSize
from app.services import page_shot
from app.storage import paths


@pytest.fixture
def book() -> Book:
    return Book(
        title="测试绘本",
        trimSize=TrimSize.from_id("A5"),
        bleedMm=3,
        style="水彩",
        characterDescription="一只戴红围巾的小兔子",
        pages=[
            PresetPage(text="第一页的文字。", preset="image-top"),
            FreePage(blocks=[TextBlock(text="自由页", xMm=10, yMm=20, wMm=60, hMm=30)]),
        ],
    )


def _fp(book: Book, index: int = 0, mode: str = "preview", scale: int = 2) -> str:
    return page_shot.fingerprint(book, book.pages[index], mode, scale)


# ---------------------------------------------------------------- 指纹


def test_fingerprint_is_deterministic(book: Book):
    """同样的内容必须算出同样的指纹，否则缓存永远命中不了。"""
    assert _fp(book) == _fp(book)


def test_fingerprint_changes_with_page_text(book: Book):
    """改一个字就得换一张图 —— 这是这个缓存最容易错的地方。"""
    before = _fp(book)
    book.pages[0].text = "改过的文字。"
    assert _fp(book) != before


def test_fingerprint_changes_with_page_blocks(book: Book):
    """自由页的正文全在 blocks 里，text 是空的 —— 只看 text 会漏掉整页改动。"""
    before = _fp(book, index=1)
    book.pages[1].blocks[0].xMm = 99
    assert _fp(book, index=1) != before


def test_fingerprint_changes_with_illustration(book: Book):
    """换一张插图，画面完全不同。"""
    before = _fp(book)
    book.pages[0].imageId = "p001_v2"
    assert _fp(book) != before


def test_fingerprint_changes_with_style_and_character(book: Book):
    """画风和角色描述是书级的，但会印在每一页上。"""
    before = _fp(book)
    book.style = "蜡笔"
    assert _fp(book) != before

    styled = _fp(book)
    book.characterDescription = "一只戴蓝围巾的小熊"
    assert _fp(book) != styled


def test_fingerprint_changes_with_trim_size_and_bleed(book: Book):
    """换开本、改出血都会让页面尺寸和重排结果变，旧图不能再用。"""
    before = _fp(book)
    book.trimSize = TrimSize.from_id("A4")
    assert _fp(book) != before

    resized = _fp(book)
    book.bleedMm = 5
    assert _fp(book) != resized


def test_fingerprint_changes_with_mode_and_scale(book: Book):
    """成品和含出血是两张不同的图；倍率不同像素也不同。"""
    assert _fp(book, mode="preview") != _fp(book, mode="print")
    assert _fp(book, scale=2) != _fp(book, scale=3)


def test_fingerprint_changes_with_page_identity(book: Book):
    """页 id 也在指纹里：拆分出来的两页内容一样、id 不同，各存各的图。

    少了它，两页内容相同就会共用一个文件名 —— 改了这一页，另一页的画面跟着变。
    """
    twin = book.pages[0].model_copy(deep=True)
    # model_copy 连 id 一起复制（不重跑 default_factory），要手动换掉才算"另一页"
    twin.id = "p_twin"
    book.pages.append(twin)
    assert _fp(book, index=0) != _fp(book, index=2)


def test_shot_path_is_scoped_by_page_number(book: Book):
    """指纹一样、页码不同的两页仍要落到不同文件上，否则互相覆盖。"""
    key = _fp(book, index=0)
    assert paths.page_shot_file("b1", 1, "preview", 2, key) != paths.page_shot_file(
        "b1", 2, "preview", 2, key
    )


# ---------------------------------------------------------------- 路径


def test_shot_path_is_under_book_dir_and_carries_the_key(book: Book):
    key = _fp(book)
    path = paths.page_shot_file("book_x", 7, "print", 3, key)

    # 落在书本目录下的隐藏子目录里，不会被当成页面资源扫出来
    assert path.parent.name == ".shots"
    assert path.parent.parent == paths.book_dir("book_x")
    assert path.suffix == ".png"
    # 页码要补零，否则 p10 会排到 p2 前面，翻日志时看着乱
    assert "p007" in path.name
    assert "print" in path.name and "3" in path.name
    assert key in path.name


def test_shot_path_rejects_nothing_but_stays_a_filename(book: Book):
    """指纹和 mode 都进文件名，必须保证拼出来还是个安全文件名。"""
    path = paths.page_shot_file("book_x", 1, "preview", 2, _fp(book))
    assert "/" not in path.name
    assert path.name.endswith(".png")
