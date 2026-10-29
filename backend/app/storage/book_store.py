"""书籍持久化。

单机单用户，但一次生图任务和一次编辑可能同时写同一本书，所以每个
book 配一把 asyncio.Lock。写入走 tmp + os.replace 原子替换，避免
进程中途挂掉留下半截 JSON。
"""

import asyncio
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from app.logger import get_logger
from app.models.book import Book, BookSummary
from app.storage import paths

logger = get_logger()

_locks: Dict[str, asyncio.Lock] = {}


def lock_for(book_id: str) -> asyncio.Lock:
    """取某本书的写锁。调用方负责 async with。"""
    lock = _locks.get(book_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[book_id] = lock
    return lock


def atomic_write(path: Path, text: str) -> None:
    """先写临时文件再 os.replace —— 中途挂掉不会留下半截 JSON。

    公开的：智能排版的撤销快照（services/auto_layout.py）也要用同一套写法。
    它写的是另一个文件，但"半截 JSON"这个坑是一模一样的。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load(book_id: str) -> Optional[Book]:
    file = paths.book_file(book_id)
    if not file.exists():
        return None
    try:
        return Book.model_validate_json(file.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        logger.error("读取书籍 %s 失败: %s", book_id, exc)
        return None


def save(book: Book) -> None:
    from app.models.book import now_iso

    book.updatedAt = now_iso()
    atomic_write(paths.book_file(book.id), book.model_dump_json(indent=2))


def create(book: Book) -> Book:
    paths.ensure_book_dirs(book.id)
    save(book)
    logger.info("创建书籍 %s（%s）", book.id, book.title)
    return book


def delete(book_id: str) -> bool:
    directory = paths.book_dir(book_id)
    if not directory.exists():
        return False
    shutil.rmtree(directory)
    _locks.pop(book_id, None)
    logger.info("删除书籍 %s", book_id)
    return True


def list_all() -> List[BookSummary]:
    if not paths.config.BOOKS_DIR.exists():
        return []

    summaries: List[BookSummary] = []
    for entry in paths.config.BOOKS_DIR.iterdir():
        if not entry.is_dir():
            continue
        book = load(entry.name)
        if book is not None:
            summaries.append(BookSummary.of(book))
    summaries.sort(key=lambda s: s.updatedAt, reverse=True)
    return summaries


def load_or_none(book_id: str) -> Optional[Book]:
    try:
        paths.safe_id(book_id)
    except ValueError:
        return None
    return load(book_id)
