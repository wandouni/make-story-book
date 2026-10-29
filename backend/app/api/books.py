"""书籍 CRUD。"""

from typing import List

from fastapi import APIRouter, HTTPException

from app.models.book import (
    Book,
    BookSummary,
    CreateBookRequest,
    TrimSize,
    UpdateBookRequest,
)
from app.storage import book_store, paths

router = APIRouter(prefix="/books", tags=["books"])


def require_book(book_id: str) -> Book:
    book = book_store.load_or_none(book_id)
    if book is None:
        raise HTTPException(status_code=404, detail=f"书籍不存在: {book_id}")
    return book


@router.get("", response_model=List[BookSummary])
async def list_books() -> List[BookSummary]:
    return book_store.list_all()


@router.post("", response_model=Book, status_code=201)
async def create_book(payload: CreateBookRequest) -> Book:
    book = Book(
        title=payload.title.strip() or "未命名绘本",
        author=payload.author,
        protagonist=payload.protagonist,
        style=payload.style,
        trimSize=TrimSize.from_id(payload.trimSizeId),
        pageCount=payload.pageCount,
        imageService=payload.imageService,
    )
    return book_store.create(book)


@router.get("/{book_id}", response_model=Book)
async def get_book(book_id: str) -> Book:
    return require_book(book_id)


@router.patch("/{book_id}", response_model=Book)
async def update_book(book_id: str, payload: UpdateBookRequest) -> Book:
    async with book_store.lock_for(book_id):
        book = require_book(book_id)
        data = payload.model_dump(exclude_unset=True, exclude_none=True)

        trim_id = data.pop("trimSizeId", None)
        if trim_id is not None:
            book.trimSize = TrimSize.from_id(trim_id)

        for field, value in data.items():
            setattr(book, field, value)

        book_store.save(book)
    return book


@router.delete("/{book_id}")
async def delete_book(book_id: str) -> dict:
    if not book_store.delete(book_id):
        raise HTTPException(status_code=404, detail=f"书籍不存在: {book_id}")
    return {"ok": True}
