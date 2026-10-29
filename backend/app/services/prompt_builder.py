"""把 LLM 生成的场景提示词，拼装成最终发给生图接口的提示词。

这里全是确定性字符串拼接，没有模型参与 —— 拼错了很容易看出来，
所以和"让模型写提示词"（text_generator）分开。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from app import config
from app.models.book import Book, FreePage, Page, PresetPage

# 所有画风共用的一段，保证出图风格统一。
#
# 全部中文，且**显式要求画面里不要有文字**：绘本是给中国儿童看的，模型看到
# 中文提示词里出现"故事""标题"之类的词，很容易顺手在画面上写一串英文
# —— 之前有一页就生成了写着 "Little Lanternfish Finds Mom" 的封面。
# 这条比在 LLM 指令里叮嘱更可靠：它是确定性拼接的，模型改不了。
_BASE = [
    "儿童绘本插画风格",
    "适合 3-5 岁儿童",
    "构图简洁清晰",
    "画面中不要出现任何文字、字母、数字或标题",
]

# 没写角色描述时的兜底，避免同一本书里主角长相每页都不一样
_NO_CHARACTER = "全书各页的角色形象保持一致"


def page_text_for_prompt(page: Page) -> str:
    """取一页用于生成提示词的文本。

    自由拖拽页的文字在 blocks 里，page.text 可能是空的 —— 那种情况下
    把文字块内容拼起来用，否则模型拿不到任何信息，只能瞎编。
    """
    if page.text.strip():
        return page.text.strip()

    if isinstance(page, FreePage):
        parts = [b.content for b in page.blocks if getattr(b, "content", "").strip()]
        return "\n".join(parts).strip()

    return ""


def preset_hint(page: Page) -> str:
    """按版式给构图要求 —— 满版叠字的图必须在下方留白，否则字会压在人脸上。"""
    if isinstance(page, PresetPage):
        return config.PRESET_PROMPT_HINTS.get(page.preset, "")
    return ""


# ---------------------------------------------------------------- 镜头语言


def design_for_page(number: int) -> Dict[str, str]:
    """按页码挑一组镜头语言（景别 / 机位 / 光线 / 画面结构）。

    用**错位取模**而不是随机，两个原因：

    - 随机会撞车。相邻两页抽到同一个景别，在对开预览里正好并排显示，
      一眼就看出来"这两页一样"。取模保证相邻页在每个维度上都不同。
    - 随机不可复现。同一本书重新生成一次，画面语言全变了，用户没法对照，
      也没法说"第 5 页那个角度好，别的页照这个来"。

    页码是 1-based。`stride` 与各组的长度互质（见 config.DESIGN_STRIDE），
    所以每组都能取满一轮再循环，四个维度合起来 20 页不重样。
    """
    picked: Dict[str, str] = {}
    for name, options in config.DESIGN_ELEMENTS.items():
        offset, stride = config.DESIGN_STRIDE[name]
        picked[name] = options[(offset + (number - 1) * stride) % len(options)]
    return picked


def design_hint(design: Dict[str, str]) -> str:
    """把一组镜头语言拼成一句，按固定语序，读起来像人话。"""
    return "，".join(design[name] for name in config.DESIGN_ORDER if design.get(name))


def _page_number(book: Book, page: Page) -> int:
    """按 id 反查页码。

    不用 `book.pages.index(page)`：pydantic 模型的 `__eq__` 是**值比较**，
    两页内容碰巧一样时会返回靠前那个的下标。找不到（这一页刚被删）就退回
    第 1 页 —— 页码只影响镜头语言的轮转，退一格不会画错内容。
    """
    for index, candidate in enumerate(book.pages, start=1):
        if candidate.id == page.id:
            return index
    return 1


def enhance_prompt(
    base_prompt: str, book: Book, page: Page, number: Optional[int] = None
) -> str:
    """场景提示词 + 角色 + 画风 + 版式要求 + 镜头语言 → 最终提示词。

    `number` 是这一页的页码（1-based），用来挑镜头语言；省略时按 `page`
    在书里的位置现算，所以老调用点和测试不用改签名。
    """
    style = config.IMAGE_STYLES.get(book.style, config.IMAGE_STYLES[config.DEFAULT_IMAGE_STYLE])

    parts: List[str] = []
    parts.append(book.characterDescription.strip() or _NO_CHARACTER)
    parts.append(base_prompt.strip())
    parts.append(style)
    parts.extend(_BASE)

    hint = preset_hint(page)
    if hint:
        parts.append(hint)

    # 镜头语言放在最后：它是对画面的最终调整，压在前面会被版式那类硬约束盖掉
    parts.append(design_hint(design_for_page(number if number is not None else _page_number(book, page))))

    # 去掉空段和重复项，保持提示词干净
    seen = set()
    cleaned = []
    for part in parts:
        if part and part not in seen:
            seen.add(part)
            cleaned.append(part)
    return ", ".join(cleaned)


def pages_needing_prompts(pages: List[Page], page_numbers: List[int]) -> List[int]:
    """挑出还没写过提示词的页。已有提示词的不覆盖 —— 用户可能手工改过。"""
    result = []
    for number in page_numbers:
        if 1 <= number <= len(pages):
            page = pages[number - 1]
            if not page.imagePrompt.strip() and page_text_for_prompt(page):
                result.append(number)
    return result


# ---------------------------------------------------------------- 封面背景


def cover_prompt(book: Book) -> str:
    """封面背景图的提示词。

    封面要叠加书名与作者文字，所以除了"切合故事主题"之外还有一条硬构图要求：
    画面上半部（约 15%-70% 高度）必须是柔和、简洁、低对比的区域（虚化天空、
    远山、柔焦光斑之类），主体角色/建筑集中在画面底部，文字位置绝不允许出现
    复杂图形 —— 这条直接写进 prompt，比事后加蒙版更能凸显文字。
    """
    style = config.IMAGE_STYLES.get(book.style, config.IMAGE_STYLES[config.DEFAULT_IMAGE_STYLE])

    theme = book.title.strip()
    if book.protagonist.strip():
        theme += f"，主角：{book.protagonist.strip()}"
    if book.characterDescription.strip():
        theme += f"（{book.characterDescription.strip()}）"

    parts = [
        "儿童绘本封面插画",
        f"故事主题：{theme}",
        style,
        "构图简洁清晰，色彩柔和温暖",
        "画面上部三分之二是柔和简洁的低对比背景（虚化的天空、柔焦云雾或渐变光影），"
        "不要出现任何角色、建筑、树木或复杂图形，留出干净的区域用于后期排版书名和作者名",
        "故事中的主体场景或角色只安排在画面底部三分之一，且不与上方留白区域争抢视觉",
        "画面中不要出现任何文字、字母、数字或标题",
    ]
    return "，".join(part for part in parts if part)
