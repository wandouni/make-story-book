"""分页器测试。

最重要的一条是 test_verbatim_*：分页结果去空白后必须与原文逐字相同。
这是"绘本不改写用户作品"的底线，改分页算法后必须先看这里过不过。
"""

import pytest

from app.services import paginator

STORY = """从前，在一座安静的森林里，住着一只白色的小兔子。

它最喜欢的事情，是在清晨的草地上蹦蹦跳跳，寻找最甜的露水。

有一天，小兔子发现了一扇从未见过的木门。门上写着：推开我，你会看见一个不一样的世界。

它伸出手，指尖碰到了冰凉的门环。它轻轻推开了门……
"""


def _paragraphs(n: int) -> str:
    return "\n\n".join(f"这是第 {i} 段。它有两个句子。" for i in range(1, n + 1))


# ---------------------------------------------------------------- 逐字保真


@pytest.mark.parametrize("page_count", [1, 2, 3, 5, 8, 20])
def test_verbatim_paragraph_story(page_count: int):
    result = paginator.paginate(STORY, page_count)
    assert paginator.check_verbatim(STORY, result.texts)


@pytest.mark.parametrize("page_count", [1, 3, 7, 15, 40])
def test_verbatim_many_paragraphs(page_count: int):
    story = _paragraphs(20)
    result = paginator.paginate(story, page_count)
    assert paginator.check_verbatim(story, result.texts)


def test_verbatim_single_line_without_blank_lines():
    """粘贴的文本常常是一整段没有空行，这是最常见的输入形态。"""
    story = "小兔子出门了。它遇见了一只乌龟。它们成了朋友。" * 20
    for page_count in (1, 4, 9, 30):
        result = paginator.paginate(story, page_count)
        assert paginator.check_verbatim(story, result.texts)


def test_verbatim_keeps_quotes_with_sentence():
    story = "它说：“你好！”然后走了。乌龟回答：“早啊。”" * 10
    result = paginator.paginate(story, 5)
    assert paginator.check_verbatim(story, result.texts)


# ---------------------------------------------------------------- 页数


def test_hits_requested_page_count():
    result = paginator.paginate(_paragraphs(20), 8)
    assert len(result.texts) == 8


def test_splits_when_paragraphs_fewer_than_pages():
    """段落只有 3 个但要 8 页 —— 必须退化到句子粒度去拆。"""
    story = "\n\n".join(f"第 {i} 段的第一句。第二句。第三句。第四句。第五句。" for i in range(3))
    result = paginator.paginate(story, 8)
    assert len(result.texts) == 8
    assert paginator.check_verbatim(story, result.texts)


def test_warns_when_content_cannot_fill_pages():
    result = paginator.paginate("只有一句话。", 8)
    assert len(result.texts) == 1
    assert any("少于设定的" in w for w in result.warnings)


def test_merges_when_paragraphs_exceed_pages():
    result = paginator.paginate(_paragraphs(30), 4)
    assert len(result.texts) == 4


# ---------------------------------------------------------------- 其他


def test_empty_story():
    result = paginator.paginate("   \n\n  ", 4)
    assert result.texts == []
    assert result.warnings


def test_capacity_warning_names_the_page():
    story = "字" * 500
    result = paginator.paginate(story, 2, capacity_chars=100)
    assert any("第 1 页" in w for w in result.warnings)


def test_deterministic():
    """同样的输入必须给出同样的分页，否则用户调参没法收敛。"""
    first = paginator.paginate(STORY, 4).texts
    second = paginator.paginate(STORY, 4).texts
    assert first == second


def test_no_page_exceeds_trim_of_neighbours_by_far():
    """合并要基本均衡，不能出现"一页 3 字、一页 300 字"。"""
    result = paginator.paginate(_paragraphs(20), 6)
    lengths = [len(t) for t in result.texts]
    assert max(lengths) < min(lengths) * 4


def test_text_box_matches_css_ratios():
    """容量估算的槽位比例要和 book.css 里的 grid 对得上。"""
    w, h = paginator.text_box_mm("image-top", 200.0, 200.0, 10.0)
    assert w == 180.0
    assert h == pytest.approx(200.0 * 0.45 - 20.0)

    w, h = paginator.text_box_mm("split-lr", 200.0, 200.0, 10.0)
    assert w == pytest.approx(100.0 - 20.0)
    assert h == pytest.approx(200.0 - 20.0)

    assert paginator.text_box_mm("image-only", 200.0, 200.0, 10.0) == (0.0, 0.0)


def test_capacity_is_positive_and_sane():
    # A5、18pt、行距 1.6、padding 10mm 的整页文字，量级应在几百字
    w, h = paginator.text_box_mm("text-only", 148.0, 210.0, 10.0)
    capacity = paginator.estimate_capacity(w, h, 18, 1.6)
    assert 200 < capacity < 900


def test_count_paragraphs():
    assert paginator.count_paragraphs(STORY) == 4
    assert paginator.count_paragraphs("   ") == 0
