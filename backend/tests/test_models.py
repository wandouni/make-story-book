"""数据模型的判别联合往返测试。

这是整个数据层最容易悄悄坏掉的地方：预设页和自由页共存在同一个
pages 列表里，全靠 mode / type 两个判别字段还原类型。一旦序列化
丢了判别字段，反序列化会静默退化成错误的类型。
"""

import json

from app.models.book import (
    Book,
    FreePage,
    ImageBlock,
    PresetPage,
    TextBlock,
    TrimSize,
)


def _mixed_book() -> Book:
    book = Book(title="测试绘本")
    book.pages = [
        PresetPage(text="从前有一只小兔子。", preset="image-top"),
        FreePage(
            text="它住在一座森林里。",
            blocks=[
                TextBlock(content="它住在一座森林里。", xMm=10, yMm=20, widthMm=80, heightMm=40),
                ImageBlock(imageId="img_abc", xMm=0, yMm=0, widthMm=200, heightMm=120),
            ],
        ),
    ]
    return book


def test_roundtrip_preserves_page_and_block_types():
    restored = Book.model_validate_json(_mixed_book().model_dump_json())

    assert isinstance(restored.pages[0], PresetPage)
    assert isinstance(restored.pages[1], FreePage)
    assert restored.pages[0].preset == "image-top"
    assert restored.pages[1].text == "它住在一座森林里。"

    text_block, image_block = restored.pages[1].blocks
    assert isinstance(text_block, TextBlock)
    assert isinstance(image_block, ImageBlock)
    assert text_block.content == "它住在一座森林里。"
    assert image_block.imageId == "img_abc"


def test_discriminator_fields_survive_json():
    raw = json.loads(_mixed_book().model_dump_json())

    assert raw["pages"][0]["mode"] == "preset"
    assert raw["pages"][1]["mode"] == "free"
    assert raw["pages"][1]["blocks"][0]["type"] == "text"
    assert raw["pages"][1]["blocks"][1]["type"] == "image"


def test_defaults_are_applied():
    restored = Book.model_validate_json(_mixed_book().model_dump_json())

    style = restored.pages[1].blocks[0].style
    assert style.fontSizePt == 18
    assert style.autoFit is True
    assert style.writingMode == "horizontal-tb"


def test_trim_size_snapshot_is_not_looked_up_later():
    """开本尺寸冗余存在书里，配置表改了也不会让老书跑版。"""
    book = Book(trimSize=TrimSize(id="CUSTOM", label="自定义", widthMm=123, heightMm=456))
    restored = Book.model_validate_json(book.model_dump_json())

    assert restored.trimSize.widthMm == 123
    assert restored.trimSize.heightMm == 456


def test_page_by_number_is_one_based():
    book = _mixed_book()

    assert book.page_by_number(1) is book.pages[0]
    assert book.page_by_number(2) is book.pages[1]
    assert book.page_by_number(0) is None
    assert book.page_by_number(3) is None
