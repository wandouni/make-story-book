"""确定性分页：把整篇故事切成 N 页，且逐字不改原文。

**为什么不用 LLM 分页**：绘本必须逐字保留原文，LLM 分页会顺手润色改写，
这是不可接受的风险。而且确定性算法可以反复调参 —— 页数从 8 改到 12 瞬间
重算；LLM 每次结果都不同，用户没法收敛。LLM 只用在"中文页文本 → 中文
生图 prompt"这一步。

**不变量**：把所有页文本去掉空白后拼接，必须与原故事去掉空白后逐字相同。

实现上靠"只记录原文的下标区间，绝不重新拼接字符串"来保证：每个 Unit 是
原文里的一段 [start, end)，页面文本 = 原文[首个 unit 起点 : 末个 unit 终点]。
这样切分逻辑再怎么写都不会丢字或造字，最多在页边界多留/少留几个空白字符 ——
而空白本来就在比较时被忽略。

想改动这个文件，先跑 `pytest tests/test_paginator.py`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Sequence, Tuple

# 段落分隔：一个空行（行内允许有空白）
_PARAGRAPH_RE = re.compile(r"\n[ \t]*\n")

# 句子：到句末标点为止，并带上紧跟其后的右引号/右括号。
# 第二个分支兜底没有标点的结尾（例如最后一句漏了句号）。
# 字符类里排除 \n，保证句子不跨行 —— 跨行的"句子"多半是误判。
_SENTENCE_RE = re.compile(r"[^。！？!?…\n]*[。！？!?…]+[”’」』）】]*|[^。！？!?…\n]+")

# 合并时给每页留的余量：标点和断行会让实际占用略高于纯字数估算
_CAPACITY_SAFETY = 0.95


@dataclass(frozen=True)
class Unit:
    """原文里的一段区间。文本本身不存，用时现切 —— 这是逐字保真的关键。"""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass
class PaginationResult:
    texts: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------- 切分


def _slice(text: str, unit: Unit) -> str:
    return text[unit.start : unit.end]


def _has_content(text: str, unit: Unit) -> bool:
    return bool(_slice(text, unit).strip())


def _paragraph_units(text: str) -> List[Unit]:
    units: List[Unit] = []
    pos = 0
    for match in _PARAGRAPH_RE.finditer(text):
        units.append(Unit(pos, match.start()))
        pos = match.end()
    units.append(Unit(pos, len(text)))
    return [u for u in units if _has_content(text, u)]


def _sentence_units(text: str, unit: Unit) -> List[Unit]:
    segment = _slice(text, unit)
    units: List[Unit] = []
    for match in _SENTENCE_RE.finditer(segment):
        candidate = Unit(unit.start + match.start(), unit.start + match.end())
        if _has_content(text, candidate):
            units.append(candidate)
    return units


def _merge_shortest_adjacent(units: List[Unit], target: int) -> List[Unit]:
    """反复合并"最短的相邻两个"，直到剩下 target 组。

    贪心，不保证全局最优，但对"把 N 段合成 M 页"这种问题足够稳，
    而且结果可预测 —— 同样的输入永远得到同样的分页。
    """
    units = list(units)
    while len(units) > target > 0:
        best = 0
        best_size = units[0].length + units[1].length
        for i in range(1, len(units) - 1):
            size = units[i].length + units[i + 1].length
            if size < best_size:
                best, best_size = i, size
        units[best : best + 2] = [Unit(units[best].start, units[best + 1].end)]
    return units


# ---------------------------------------------------------------- 容量估算


def text_box_mm(
    preset: str, trim_w_mm: float, trim_h_mm: float, padding_mm: float
) -> Tuple[float, float]:
    """某预设下文字的可用区尺寸（mm）。

    这些比例**必须与 book.css 里的 grid 定义保持一致**，否则容量提示会
    误报或漏报。改 CSS 时记得回来改这里。
    """
    if preset in ("image-top", "image-bottom"):
        box = (trim_w_mm, trim_h_mm * 0.45)
    elif preset == "split-lr":
        box = (trim_w_mm * 0.5, trim_h_mm)
    elif preset == "text-only":
        box = (trim_w_mm, trim_h_mm)
    elif preset == "full-bleed-overlay":
        # 叠字只占一条，位置可调，取三分之一页高作经验值
        box = (trim_w_mm, trim_h_mm * 0.35)
    else:  # image-only 没有文字槽
        return 0.0, 0.0

    return max(box[0] - 2 * padding_mm, 0.0), max(box[1] - 2 * padding_mm, 0.0)


def estimate_capacity(
    box_w_mm: float, box_h_mm: float, font_size_pt: float, line_height: float
) -> int:
    """一个文字框大概能放多少字。用于分页后的溢出提示，不追求精确。"""
    if box_w_mm <= 0 or box_h_mm <= 0 or font_size_pt <= 0:
        return 0

    # 中文一个字的宽度约等于字号
    char_w_mm = font_size_pt / 72 * 25.4
    line_h_mm = char_w_mm * line_height

    chars_per_line = box_w_mm / char_w_mm
    lines = box_h_mm / line_h_mm
    return int(chars_per_line * lines * _CAPACITY_SAFETY)


# ---------------------------------------------------------------- 主入口


def paginate(text: str, page_count: int, capacity_chars: int = 0) -> PaginationResult:
    """把 text 切成 page_count 页。

    capacity_chars > 0 时，超出容量的页会在 warnings 里点名。
    """
    result = PaginationResult()
    if not text.strip():
        result.warnings.append("故事内容为空。")
        return result

    page_count = max(1, page_count)

    paragraphs = _paragraph_units(text)
    units = paragraphs

    if len(units) < page_count:
        # 段落不够分，退化到句子粒度再合并。
        # 直接按句子分页会让每页语义过碎，所以只在段落数不足时才这么做。
        sentences: List[Unit] = []
        for paragraph in paragraphs:
            sentences.extend(_sentence_units(text, paragraph))

        if len(sentences) > len(units):
            units = sentences

    units = _merge_shortest_adjacent(units, page_count)

    if len(units) < page_count:
        result.warnings.append(
            f"故事只能拆出 {len(units)} 页，少于设定的 {page_count} 页 —— "
            f"内容不足以填满，请减少页数或补充故事。"
        )

    result.texts = [_slice(text, unit).strip() for unit in units]

    if capacity_chars > 0:
        for index, page_text in enumerate(result.texts, start=1):
            if len(page_text) > capacity_chars:
                result.warnings.append(
                    f"第 {index} 页约 {len(page_text)} 字，超过当前版式的容量"
                    f"（约 {capacity_chars} 字），可能需要调小字号或改版式。"
                )

    return result


def count_paragraphs(text: str) -> int:
    return len(_paragraph_units(text))


def check_verbatim(original: str, pages: Sequence[str]) -> bool:
    """校验分页结果去空白后与原文一致。

    这是分页器的核心不变量，测试和"AI 智能分页"的降级判断都靠它。
    """
    normalize = lambda s: re.sub(r"\s+", "", s)  # noqa: E731
    return normalize("".join(pages)) == normalize(original)
