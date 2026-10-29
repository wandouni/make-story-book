"""故事入库前的 Markdown 清理。

清理发生在 **PUT /story** 而不是分页时：分页的底线是逐字保留原文，它必须跟
storyText 严格对得上。所以 storyText 存的就得是干净文本。
"""

import pytest

from app.services.story_text import strip_markdown


def test_strips_heading_markers():
    """用户真实踩到的那一条：粘贴的故事第一行是 `## 小灯笼鱼找妈妈`。"""
    assert strip_markdown("## 小灯笼鱼找妈妈\n\n深海里，住着一只小灯笼鱼。") == (
        "小灯笼鱼找妈妈\n\n深海里，住着一只小灯笼鱼。"
    )


@pytest.mark.parametrize("level", range(1, 7))
def test_strips_every_heading_level(level):
    assert strip_markdown(f"{'#' * level} 标题") == "标题"


def test_keeps_the_words_drops_the_markers():
    text = "**好冷**，风把_围巾_吹起来了，`雪` 落在 ~~睫毛~~ 上。"
    assert strip_markdown(text) == "好冷，风把围巾吹起来了，雪 落在 睫毛 上。"


def test_strips_list_and_quote_markers():
    text = "- 第一件事\n- 第二件事\n\n> 妈妈说\n\n* 第三件事"
    assert strip_markdown(text) == "第一件事\n第二件事\n\n妈妈说\n\n第三件事"


def test_strips_links_and_images():
    assert strip_markdown("看[这里](https://example.com)有只兔子") == "看这里有只兔子"
    assert strip_markdown("![插图](a.png)雪地") == "插图雪地"


def test_keeps_fenced_block_content():
    """围栏里的正文要留着，只去掉那两行围栏。"""
    assert strip_markdown("```\n小兔子出门了。\n```") == "小兔子出门了。"


def test_drops_horizontal_rules():
    assert strip_markdown("上段\n\n---\n\n下段") == "上段\n\n下段"


def test_keeps_ordered_list_numbers():
    """**故意不剥**有序列表序号。

    中文故事里作者真写的"1、"和 Markdown 的"1. "分不清，误删一个真序号比
    留一个多余的"1."糟得多。
    """
    assert strip_markdown("1. 小兔子起床了\n2. 小兔子出门了") == (
        "1. 小兔子起床了\n2. 小兔子出门了"
    )


def test_plain_story_is_untouched():
    """没有标记的普通故事必须原样通过 —— 清理不能动用户的作品。"""
    story = (
        "从前，在一座安静的森林里，住着一只白色的小兔子，名字叫雪球。\n\n"
        "雪球最喜欢的事情，是在清晨的草地上蹦蹦跳跳，寻找最甜的露水。\n\n"
        "“你是我妈妈吗？”他问。\n\n"
        "5 减 3 等于 2，他数了数。"
    )
    assert strip_markdown(story) == story


def test_does_not_eat_underscores_inside_words():
    """`snake_case` 里的下划线不是强调标记。"""
    assert strip_markdown("变量叫 snake_case_name") == "变量叫 snake_case_name"


def test_collapses_blank_line_runs():
    assert strip_markdown("上段\n\n\n\n\n下段") == "上段\n\n下段"


def test_strips_escapes():
    assert strip_markdown(r"这是 \*真的\* 星号") == "这是 *真的* 星号"
