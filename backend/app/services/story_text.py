"""故事文本入库前的清理：剥掉 Markdown 标记，只留文字本身。

用户粘贴的故事常带着 Markdown —— 标题的 `##`、加粗的 `**`。而分页的底线是
**逐字保留原文**（见 paginator.py），于是这些标记会被原样带进页面、印进 PDF。
"逐字保留"要保的是**作品**，不是排版符号，所以在这里、也就是**入库这一步**
剥掉：storyText 存的就是干净文本，下游的分页不变量照旧成立、也照旧有意义。

清的是标记，不是内容：`## 小灯笼鱼找妈妈` → `小灯笼鱼找妈妈`，
`**好冷**` → `好冷`，围栏代码块里的正文留着，只去掉那两行围栏。

**故意不做**的两件事：

- 不动有序列表的序号（`1. `）。中文故事里作者真写的"1、"和 Markdown 的
  "1. "分不清，误删一个真序号比留一个多余的"1."糟得多。
- 不解析 HTML 标签。`<br>` 在故事里几乎不出现，而一旦真出现，多半是作者
  故意写的字面内容。
"""

from __future__ import annotations

import re

# 行首标记
_ATX = re.compile(r"^\s{0,3}#{1,6}\s+")
_BLOCKQUOTE = re.compile(r"^\s{0,3}>\s?")
_ULIST = re.compile(r"^\s{0,3}[-*+]\s+")
# 整行只有分隔符：Markdown 的水平线，也是 setext 标题的下划线
_RULE = re.compile(r"^\s{0,3}([-=*_])(\s*\1){2,}\s*$")
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")

# 行内标记
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_CODE = re.compile(r"`([^`\n]+)`")
# 强调要成对出现才剥，且 `_` 不能贴着字母数字 —— 否则 snake_case_name 会被吃掉。
# 分隔符前面不能是反斜杠：`\*` 是**转义过的字面星号**，不是强调标记。
_STRONG_STAR = re.compile(r"(?<!\\)\*\*(?=\S)(.+?)(?<=\S)(?<!\\)\*\*", re.DOTALL)
_STRONG_UNDER = re.compile(
    r"(?<![A-Za-z0-9_\\])__(?=\S)(.+?)(?<=\S)__(?![A-Za-z0-9_])", re.DOTALL
)
_STRIKE = re.compile(r"(?<!\\)~~(?=\S)(.+?)(?<=\S)(?<!\\)~~", re.DOTALL)
_EM_STAR = re.compile(r"(?<!\\)\*(?=\S)([^*\n]+?)(?<=\S)(?<!\\)\*")
_EM_UNDER = re.compile(r"(?<![A-Za-z0-9_\\])_(?=\S)([^_\n]+?)(?<=\S)_(?![A-Za-z0-9_])")
_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!>~])")

_BLANK_RUNS = re.compile(r"\n{3,}")


def _drop_fences(lines: list[str]) -> list[str]:
    """去掉围栏行本身，保留里面的正文。"""
    out = []
    inside = False
    for line in lines:
        if _FENCE.match(line):
            inside = not inside
            continue
        out.append(line)
    return out


def strip_markdown(text: str) -> str:
    """剥掉 Markdown 标记，返回纯文本。

    对没有标记的普通故事是**恒等**的（只顺带把三个以上连续换行压成两个空行），
    所以可以放心对任何文本调用，不会动到用户的作品。
    """
    lines = []
    for line in _drop_fences(text.splitlines()):
        if _RULE.match(line):
            continue
        line = _ATX.sub("", line)
        line = _BLOCKQUOTE.sub("", line)
        line = _ULIST.sub("", line)
        lines.append(line)

    cleaned = "\n".join(lines)
    # 先长后短：`**加粗**` 要在 `*斜体*` 之前处理，否则会被拆成半个星号
    cleaned = _IMAGE.sub(r"\1", cleaned)
    cleaned = _LINK.sub(r"\1", cleaned)
    cleaned = _CODE.sub(r"\1", cleaned)
    cleaned = _STRONG_STAR.sub(r"\1", cleaned)
    cleaned = _STRONG_UNDER.sub(r"\1", cleaned)
    cleaned = _STRIKE.sub(r"\1", cleaned)
    cleaned = _EM_STAR.sub(r"\1", cleaned)
    cleaned = _EM_UNDER.sub(r"\1", cleaned)
    cleaned = _ESCAPE.sub(r"\1", cleaned)

    return _BLANK_RUNS.sub("\n\n", cleaned).strip()
