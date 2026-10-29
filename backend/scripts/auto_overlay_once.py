"""一次性迁移：把满版叠字页的落点从固定 "bottom" 改成 "auto"。

"auto" 是新的默认值（后端分析插图，把文字放进细节最少的那条横带）。但
`overlayPosition` 是**存进 book.json 的**，所以在此之前建的书里都写着当时的
默认值 "bottom" —— 不改的话，用户不会自动享受到自动定位，得逐页手动点一下
「自动」，而他根本不知道有这个选项。

只动这一件事：叠字位置。页数、配图、正文、字号、其他版式全都不碰。

安全性靠两件事：

1. **只改满版叠字页**。其他版式根本没有叠字位置这个选项，改它是无意义的风险。
2. **备份**：改动前把 book.json 复制成 book.json.bak（已有 .bak 就不覆盖，
   免得把最早的版本冲掉），脚本可以直接反悔。

默认只预览，不改任何东西。确认无误后加 --apply。

用法：
    uv run python scripts/auto_overlay_once.py
    uv run python scripts/auto_overlay_once.py --apply
"""

import argparse
import shutil
import sys
from pathlib import Path

# 直接跑脚本时 sys.path[0] 是 scripts/，`app` 包不在上面
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.book import PresetPage  # noqa: E402
from app.storage import book_store, paths  # noqa: E402

# 旧默认值。只迁移恰好等于它的页 —— 用户手动选过 "top"/"center" 的页说明
# 他有明确意图，不该被"自动"覆盖掉。
LEGACY_DEFAULT = "bottom"
TARGET = "auto"


def migrate_book(book) -> list[str]:
    """就地迁移一本书，返回改动说明。没有可改的就返回空列表。"""
    changes: list[str] = []

    for index, page in enumerate(book.pages, 1):
        if not isinstance(page, PresetPage):
            continue  # 自由页的落点是块的坐标，属于用户数据，不动
        if page.preset != "full-bleed-overlay":
            continue
        if page.options.overlayPosition != LEGACY_DEFAULT:
            continue

        page.options.overlayPosition = TARGET
        has_image = bool(page.imageId)
        note = "有插图，会自动分析留白" if has_image else "暂无插图，暂时按贴底渲染"
        changes.append(f"  第{index}页: {LEGACY_DEFAULT} → {TARGET}（{note}）")

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
        changes = migrate_book(book)
        if not changes:
            continue

        print(f"{book.title} ({book.id})")
        for line in changes:
            print(line)

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
        print("所有书的叠字页都已经是自动定位了，无需迁移。")
    elif args.apply:
        print(f"完成，迁移了 {touched} 本书。")
    else:
        print(f"共 {touched} 本书需要迁移。加 --apply 才会写入。")


if __name__ == "__main__":
    main()
