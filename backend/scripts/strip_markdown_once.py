"""一次性清理：把已有书籍里残留的 Markdown 标记剥掉。

新数据由 `PUT /story` 在入库时清理（见 services/story_text.py），但**清理之前
建的**书里，storyText 和页面文本都还带着 `##` 这类标记，会一直印在 PDF 上。
重贴一遍故事能修好，代价是重新分页会丢掉已经配好的插图 —— 这个脚本原地修，
页数、配图、版式全都不动。

安全性靠两件事：

1. **不把好好的书改坏**：清理前页面与原文逐字一致（`paginator.check_verbatim`，
   和分页用的是同一个不变量）的书，清理后必须仍然一致，否则跳过不写。
   本来就不一致的书照常清理 —— 那种不一致是**别的原因**造成的（原文被改过），
   和剥 Markdown 无关，拿它当拦路虎只会让该修的书修不了。
2. **备份**：改动前把 book.json 复制成 book.json.bak，脚本可以直接反悔。

默认只预览，不改任何东西。确认无误后加 --apply。

用法：
    uv run python scripts/strip_markdown_once.py
    uv run python scripts/strip_markdown_once.py --apply
"""

import argparse
import re
import shutil
import sys
from pathlib import Path

# 直接跑脚本时 sys.path[0] 是 scripts/，`app` 包不在上面（pytest 靠 pyproject
# 里的 pythonpath 才找得到）。别的 e2e 脚本只发 HTTP 请求，不需要这一句。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import paginator, story_text  # noqa: E402
from app.storage import book_store, paths  # noqa: E402


def clean_book(book) -> list[str]:
    """就地清理一本书，返回改动说明。没有可改的就返回空列表。"""
    changes: list[str] = []

    old_story = book.storyText
    old_pages = [p.text for p in book.pages]
    cleaned_story = story_text.strip_markdown(old_story)
    cleaned_pages = [story_text.strip_markdown(text) for text in old_pages]

    if cleaned_story != old_story:
        changes.append(f"  原文: {old_story[:40]!r} → {cleaned_story[:40]!r}")
    for index, (old, new) in enumerate(zip(old_pages, cleaned_pages), 1):
        if old != new:
            changes.append(f"  第{index}页: {old[:40]!r} → {new[:40]!r}")

    if not changes:
        return []

    # 原本对得上的书，清理后必须仍然对得上。对不上说明剥 Markdown 动到了
    # 不该动的地方（正文里的字面星号之类），宁可跳过让人来看。
    if paginator.check_verbatim(old_story, old_pages) and not paginator.check_verbatim(
        cleaned_story, cleaned_pages
    ):
        changes.append("  ⚠ 清理会破坏原文与页面的对应关系，已跳过")
        return ["SKIP"] + changes

    # 本来就不一致：说清楚，但别拿它当拦路虎 —— 那是原文被改过造成的，与剥标记无关
    if not paginator.check_verbatim(old_story, old_pages):
        story_len = len(re.sub(r"\s+", "", old_story))
        page_len = len(re.sub(r"\s+", "", "".join(old_pages)))
        changes.append(
            f"  ⚠ 这本书的页面与原文本来就不一致（原文 {story_len} 字，页面共 {page_len} 字）"
            "—— 多半是分页后又改过原文。本次只剥标记，不动分页。"
        )

    book.storyText = cleaned_story
    for page, new in zip(book.pages, cleaned_pages):
        page.text = new
    return changes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真的写入（默认只预览）")
    args = parser.parse_args()

    books = book_store.list_all()
    if not books:
        print("没有书。")
        return

    touched = 0
    for summary in books:
        book = book_store.load(summary.id)
        if book is None:
            continue
        changes = clean_book(book)
        if not changes:
            continue

        print(f"{book.title} ({book.id})")
        for line in changes:
            print(line)

        if changes[0] == "SKIP":
            print("  → 跳过，未写入\n")
            continue

        touched += 1
        if args.apply:
            book_file = paths.book_file(book.id)
            backup = book_file.with_suffix(".json.bak")
            if not backup.exists():
                shutil.copy2(book_file, backup)
            book_store.save(book)
            print(f"  → 已写入（备份在 {backup.name}）\n")
        else:
            print("  → 预览，未写入\n")

    if not touched:
        print("所有书都是干净的，无需清理。")
    elif args.apply:
        print(f"完成，清理了 {touched} 本书。")
    else:
        print(f"共 {touched} 本书需要清理。加 --apply 才会写入。")
        sys.exit(0)


if __name__ == "__main__":
    main()
