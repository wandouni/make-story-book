"""HTML 渲染端点。

前端的预览 iframe 和 Playwright 导出都打这里，拿到的是同一份 HTML。
"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse

from app.api.books import require_book
from app.logger import get_logger
from app.render.renderer import render_book_html, render_page_html
from app.services import page_shot

logger = get_logger()

router = APIRouter(prefix="/books/{book_id}/render", tags=["render"])

RenderMode = Literal["preview", "print"]


@router.get("/page/{number}", response_class=HTMLResponse)
async def render_page(book_id: str, number: int, mode: RenderMode = "preview") -> HTMLResponse:
    book = require_book(book_id)
    page = book.page_by_number(number)
    if page is None:
        raise HTTPException(status_code=404, detail=f"页码超出范围: {number}")
    return HTMLResponse(render_page_html(book, page, mode))


@router.get("/all", response_class=HTMLResponse)
async def render_all(book_id: str, mode: RenderMode = "preview") -> HTMLResponse:
    book = require_book(book_id)
    return HTMLResponse(render_book_html(book, mode))


@router.get("/page/{number}/shot.png")
async def render_page_shot(
    book_id: str,
    number: int,
    mode: RenderMode = "preview",
    scale: int = Query(page_shot.DEFAULT_SCALE, ge=1, le=page_shot.MAX_SCALE),
) -> FileResponse:
    """单页 PNG，给 3D 翻页当纹理。见 services/page_shot.py。

    路径写成 `/page/{number}/shot.png` 而不是 `/page/{number}.png`：后者会和
    上面那条 `/page/{number}` 撞上 —— Starlette 的路由正则是 `[^/]+`，
    `3.png` 会先匹配到 `{number}`，再因为转不成 int 而 422，根本轮不到
    带后缀的那条路由。
    """
    book = require_book(book_id)
    page = book.page_by_number(number)
    if page is None:
        raise HTTPException(status_code=404, detail=f"页码超出范围: {number}")

    try:
        path = await page_shot.shot_page(book, page, number, mode, scale)
    except Exception as exc:  # noqa: BLE001
        logger.error("第 %d 页截图失败: %s", number, exc)
        raise HTTPException(status_code=500, detail=f"页面截图失败：{exc}") from exc

    return FileResponse(
        path,
        media_type="image/png",
        # 内容改了 URL 不变，所以不能让浏览器直接吃缓存。但文件名带内容指纹，
        # 命中缓存时只是一次 304 往返，很便宜。
        headers={"Cache-Control": "no-cache"},
    )
