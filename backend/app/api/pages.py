"""故事文本、分页与页面级操作。

页面操作一律返回**整页**（或整份页面列表）而非整本书：拖拽时前端每秒会发
好几次请求，回传整本书会让 payload 无谓地变大。
"""

import re
from typing import List, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import config
from app.api.books import require_book
from app.models.book import (
    Block,
    FreePage,
    ImageBlock,
    Page,
    PresetPage,
    PresetPageOptions,
    TextBlock,
    TextStyle,
    new_id,
)
from app.render import presets
from app.services import paginator, story_text
from app.storage import book_store

router = APIRouter(prefix="/books", tags=["pages"])


class StoryRequest(BaseModel):
    text: str = ""


class StoryResponse(BaseModel):
    charCount: int
    paragraphCount: int
    # 入库时剥掉 Markdown 标记之后的文本。与请求里的不一样时前端要把它写回
    # 输入框 —— 否则界面上显示的原文和后面分页出来的页面会对不上。
    text: str


class PaginateRequest(BaseModel):
    pageCount: int = Field(
        default=config.DEFAULT_PAGES, ge=config.MIN_PAGES, le=config.MAX_PAGES
    )
    preset: str = config.DEFAULT_PRESET


class PaginateResponse(BaseModel):
    pages: List[Page]
    warnings: List[str]


class UpdatePageRequest(BaseModel):
    text: Optional[str] = None
    imagePrompt: Optional[str] = None
    preset: Optional[str] = None
    options: Optional[PresetPageOptions] = None
    # 整页覆盖块。给撤销/重做用：一次原子写入，比逐块 PATCH 既快又不会
    # 出现"撤到一半失败、页面停在中间状态"。只对自由页有效。
    blocks: Optional[List[Block]] = None


class SplitPageRequest(BaseModel):
    atCharIndex: int


class PageOrderRequest(BaseModel):
    order: List[str]


class ToPresetRequest(BaseModel):
    preset: str = config.DEFAULT_PRESET


class NewBlockRequest(BaseModel):
    type: Literal["text", "image"]
    xMm: float = 0
    yMm: float = 0
    widthMm: float = 0
    heightMm: float = 0
    # 新文字块的初始内容，默认取这一页的正文
    content: Optional[str] = None
    imageId: Optional[str] = None


class UpdateBlockRequest(BaseModel):
    """块的局部更新。

    字段全可选：拖拽时前端只发 xMm/yMm，改样式时只发 style，不必回传整块。
    类型专属字段（content 之于文字块、fit 之于图片块）用错会报 400，
    而不是被静默忽略 —— 静默忽略会让前端以为设置生效了。
    """

    xMm: Optional[float] = None
    yMm: Optional[float] = None
    widthMm: Optional[float] = None
    heightMm: Optional[float] = None
    rotationDeg: Optional[float] = None
    zIndex: Optional[int] = None
    locked: Optional[bool] = None
    opacity: Optional[float] = None

    # 文字块
    content: Optional[str] = None
    style: Optional[TextStyle] = None

    # 图片块
    imageId: Optional[str] = None
    fit: Optional[Literal["cover", "contain"]] = None
    focusX: Optional[float] = None
    focusY: Optional[float] = None
    borderRadiusMm: Optional[float] = None
    bleed: Optional[bool] = None


class PageResponse(BaseModel):
    page: Page


class PagesResponse(BaseModel):
    pages: List[Page]


# ---------------------------------------------------------------- 工具


def _require_page(book, number: int) -> Page:
    page = book.page_by_number(number)
    if page is None:
        raise HTTPException(
            status_code=404, detail=f"页码超出范围: {number}（共 {len(book.pages)} 页）"
        )
    return page


def _char_count(text: str) -> int:
    """字数按去空白计 —— 与分页容量估算口径一致，用户看到的提示才对得上。"""
    return len(re.sub(r"\s+", "", text))


# ---------------------------------------------------------------- 故事


@router.put("/{book_id}/story", response_model=StoryResponse)
async def put_story(book_id: str, payload: StoryRequest) -> StoryResponse:
    """存故事原文。

    在**这里**剥掉 Markdown 标记（`## 标题` → `标题`），而不是在分页时剥：
    分页的底线是逐字保留原文，它得跟 storyText 严格对得上。把清理放在入库
    这一步，storyText 本身就是干净文本，分页不变量照旧成立、也照旧有意义。
    """
    text = story_text.strip_markdown(payload.text)

    async with book_store.lock_for(book_id):
        book = require_book(book_id)
        book.storyText = text
        book_store.save(book)

    return StoryResponse(
        charCount=_char_count(text),
        paragraphCount=paginator.count_paragraphs(text),
        text=text,
    )


@router.post("/{book_id}/paginate", response_model=PaginateResponse)
async def paginate_book(book_id: str, payload: PaginateRequest) -> PaginateResponse:
    if payload.preset not in config.PRESETS:
        raise HTTPException(status_code=400, detail=f"未知的预设版式: {payload.preset}")

    async with book_store.lock_for(book_id):
        book = require_book(book_id)
        if not book.storyText.strip():
            raise HTTPException(status_code=400, detail="还没有故事内容，请先粘贴故事。")

        options = PresetPageOptions()
        box_w, box_h = paginator.text_box_mm(
            payload.preset, book.trimSize.widthMm, book.trimSize.heightMm, options.paddingMm
        )
        capacity = paginator.estimate_capacity(
            box_w, box_h, options.fontSizePt, options.lineHeight
        )

        result = paginator.paginate(book.storyText, payload.pageCount, capacity_chars=capacity)
        if not result.texts:
            raise HTTPException(status_code=400, detail="故事内容为空，无法分页。")

        warnings = list(result.warnings)

        # 重新分页是按文本重切的，旧插图对应的文字已经变了，留着反而是错的
        # （兔子图配到了"木门"那页）。明确告知用户，由前端二次确认。
        illustrated = sum(1 for page in book.pages if page.imageId)
        if illustrated:
            warnings.insert(0, f"重新分页丢弃了已生成的 {illustrated} 张插图，需要重新配图。")

        book.pages = [
            PresetPage(text=text, preset=payload.preset, options=options.model_copy())
            for text in result.texts
        ]
        book.pageCount = payload.pageCount
        book_store.save(book)

    return PaginateResponse(pages=book.pages, warnings=warnings)


# ---------------------------------------------------------------- 页面


@router.patch("/{book_id}/pages/{number}", response_model=PageResponse)
async def update_page(
    book_id: str, number: int, payload: UpdatePageRequest
) -> PageResponse:
    async with book_store.lock_for(book_id):
        book = require_book(book_id)
        page = _require_page(book, number)

        if payload.preset is not None:
            if not isinstance(page, PresetPage):
                raise HTTPException(
                    status_code=400, detail="自由拖拽页没有预设版式，不能直接改 preset。"
                )
            if payload.preset not in config.PRESETS:
                raise HTTPException(status_code=400, detail=f"未知的预设版式: {payload.preset}")
            page.preset = payload.preset

        if payload.options is not None:
            if not isinstance(page, PresetPage):
                raise HTTPException(status_code=400, detail="该页不是预设页，没有 options。")
            page.options = payload.options

        if payload.blocks is not None:
            if not isinstance(page, FreePage):
                raise HTTPException(status_code=400, detail="该页不是自由页，没有 blocks。")
            page.blocks = payload.blocks

        if payload.text is not None:
            page.text = payload.text
        if payload.imagePrompt is not None:
            page.imagePrompt = payload.imagePrompt

        book_store.save(book)

    return PageResponse(page=page)


# ---------------------------------------------------------------- 模式转换


@router.post("/{book_id}/pages/{number}/to-free", response_model=PageResponse)
async def to_free(book_id: str, number: int) -> PageResponse:
    """预设页 → 自由拖拽页。

    展开成等价的块，用户从"看起来完全没变"的状态开始拖。视觉等价性由
    render/presets.py 保证，tests/test_presets.py 会逐版式核对。
    """
    async with book_store.lock_for(book_id):
        book = require_book(book_id)
        page = _require_page(book, number)
        if isinstance(page, FreePage):
            raise HTTPException(status_code=400, detail="这一页已经是自由拖拽页。")

        converted = presets.to_free_page(page, book)
        book.pages[number - 1] = converted
        book_store.save(book)

    return PageResponse(page=converted)


@router.post("/{book_id}/pages/{number}/to-preset", response_model=PageResponse)
async def to_preset(
    book_id: str, number: int, payload: ToPresetRequest
) -> PageResponse:
    """自由拖拽页 → 预设页。

    **有损**：块的位置、旋转、逐块样式都会被丢弃，只剩正文和图片。
    前端必须先二次确认再调这个接口。
    """
    if payload.preset not in config.PRESETS:
        raise HTTPException(status_code=400, detail=f"未知的预设版式: {payload.preset}")

    async with book_store.lock_for(book_id):
        book = require_book(book_id)
        page = _require_page(book, number)
        if isinstance(page, PresetPage):
            raise HTTPException(status_code=400, detail="这一页已经是预设页。")

        converted = presets.to_preset_page(page, payload.preset)
        book.pages[number - 1] = converted
        book_store.save(book)

    return PageResponse(page=converted)


# ---------------------------------------------------------------- 块操作


def _require_free_page(book, number: int) -> FreePage:
    page = _require_page(book, number)
    if not isinstance(page, FreePage):
        raise HTTPException(
            status_code=400,
            detail="这一页是预设版式，请先「转为自由拖拽」再编辑块。",
        )
    return page


def _require_block(page: FreePage, block_id: str) -> Block:
    for block in page.blocks:
        if block.id == block_id:
            return block
    raise HTTPException(status_code=404, detail=f"找不到块: {block_id}")


_TEXT_ONLY_FIELDS = {"content", "style"}
_IMAGE_ONLY_FIELDS = {"imageId", "fit", "focusX", "focusY", "borderRadiusMm", "bleed"}


@router.post("/{book_id}/pages/{number}/blocks", response_model=PageResponse)
async def add_block(
    book_id: str, number: int, payload: NewBlockRequest
) -> PageResponse:
    async with book_store.lock_for(book_id):
        book = require_book(book_id)
        page = _require_free_page(book, number)

        common = {
            "id": new_id("b"),
            "xMm": payload.xMm,
            "yMm": payload.yMm,
            "widthMm": payload.widthMm,
            "heightMm": payload.heightMm,
            # 新块放在最上层，否则加完看不见，用户以为没生效
            "zIndex": max((b.zIndex for b in page.blocks), default=0) + 1,
        }

        if payload.type == "text":
            block: Block = TextBlock(
                **common,
                content=payload.content if payload.content is not None else page.text,
                style=TextStyle(),
            )
        else:
            block = ImageBlock(**common, imageId=payload.imageId)

        page.blocks.append(block)
        book_store.save(book)

    return PageResponse(page=page)


@router.patch(
    "/{book_id}/pages/{number}/blocks/{block_id}", response_model=PageResponse
)
async def update_block(
    book_id: str, number: int, block_id: str, payload: UpdateBlockRequest
) -> PageResponse:
    """局部更新一个块。拖拽过程中前端会以 ~50ms 节流反复调它。"""
    async with book_store.lock_for(book_id):
        book = require_book(book_id)
        page = _require_free_page(book, number)
        block = _require_block(page, block_id)

        data = payload.model_dump(exclude_unset=True)
        wrong = (
            _IMAGE_ONLY_FIELDS
            if isinstance(block, TextBlock)
            else _TEXT_ONLY_FIELDS
        ) & data.keys()
        if wrong:
            kind = "文字块" if isinstance(block, TextBlock) else "图片块"
            raise HTTPException(
                status_code=400,
                detail=f"{kind}不接受这些字段: {', '.join(sorted(wrong))}",
            )

        if "style" in data and data["style"] is not None:
            block.style = TextStyle(**data.pop("style"))
        elif "style" in data:
            data.pop("style")

        for key, value in data.items():
            setattr(block, key, value)

        book_store.save(book)

    return PageResponse(page=page)


@router.delete(
    "/{book_id}/pages/{number}/blocks/{block_id}", response_model=PageResponse
)
async def delete_block(book_id: str, number: int, block_id: str) -> PageResponse:
    async with book_store.lock_for(book_id):
        book = require_book(book_id)
        page = _require_free_page(book, number)
        _require_block(page, block_id)

        page.blocks = [b for b in page.blocks if b.id != block_id]
        book_store.save(book)

    return PageResponse(page=page)


@router.delete("/{book_id}/pages/{number}", response_model=PagesResponse)
async def delete_page(book_id: str, number: int) -> PagesResponse:
    async with book_store.lock_for(book_id):
        book = require_book(book_id)
        _require_page(book, number)
        book.pages.pop(number - 1)
        book_store.save(book)

    return PagesResponse(pages=book.pages)


class BatchDeleteRequest(BaseModel):
    pageNumbers: List[int]


@router.post("/{book_id}/pages/batch-delete", response_model=PagesResponse)
async def batch_delete_pages(book_id: str, payload: BatchDeleteRequest) -> PagesResponse:
    """一次删掉多页。

    用 POST 而不是 `DELETE .../pages`：带请求体的 DELETE 在部分客户端和代理上
    不可靠。路径也不会和 `/{number}` 撞 —— 这里的段是字面量 batch-delete，
    和整数路径参数不匹配（`PUT /pages/order` 已经是同一个先例）。
    """
    async with book_store.lock_for(book_id):
        book = require_book(book_id)

        # 先整体校验再动手：有一个越界就整单拒绝。部分成功会让前端的选中状态
        # 和实际页面对不上，而用户看到的是一句"删了 3 页"的提示。
        numbers = sorted(set(payload.pageNumbers))
        if not numbers:
            raise HTTPException(status_code=400, detail="没有指定要删除的页码。")
        for number in numbers:
            _require_page(book, number)

        # 倒序删。正序删会让后面的下标整体前移 —— 删掉第 2 页之后，原来的
        # 第 3 页变成了第 2 页，接着 pop(2) 删的就是错的那一页。
        for number in reversed(numbers):
            book.pages.pop(number - 1)

        book_store.save(book)

    return PagesResponse(pages=book.pages)


@router.post("/{book_id}/pages/{number}/split", response_model=PagesResponse)
async def split_page(book_id: str, number: int, payload: SplitPageRequest) -> PagesResponse:
    """在指定字符位置把一页切成两页。切点两侧都原样保留，不补字不删字。"""
    async with book_store.lock_for(book_id):
        book = require_book(book_id)
        page = _require_page(book, number)

        index = payload.atCharIndex
        if not 0 < index < len(page.text):
            raise HTTPException(
                status_code=400,
                detail=f"切分位置 {index} 超出范围（应在 1 到 {len(page.text) - 1} 之间）",
            )

        head = page.model_copy(deep=True)
        tail = page.model_copy(deep=True)
        # model_copy 复制的是**已生成**的字段值，不会重跑 default_factory，
        # 所以 tail 会和 head（以及原页）拿着同一个 id。重复 id 会让前端
        # `:key="page.id"` 撞车、v-model 绑错页，必须手动换一个。
        tail.id = new_id("p")
        head.text = page.text[:index].strip()
        tail.text = page.text[index:].strip()
        # 切出来的两页内容都变了，旧插图不再对应，不继承
        head.imageId = tail.imageId = None
        head.imageStatus = tail.imageStatus = "none"

        book.pages[number - 1 : number] = [head, tail]
        book_store.save(book)

    return PagesResponse(pages=book.pages)


@router.post("/{book_id}/pages/{number}/merge-next", response_model=PagesResponse)
async def merge_next(book_id: str, number: int) -> PagesResponse:
    async with book_store.lock_for(book_id):
        book = require_book(book_id)
        page = _require_page(book, number)
        if number >= len(book.pages):
            raise HTTPException(status_code=400, detail="这是最后一页，后面没有可合并的页。")

        following = book.pages[number]
        page.text = f"{page.text}\n{following.text}".strip()
        page.imageId = None
        page.imageStatus = "none"
        book.pages.pop(number)
        book_store.save(book)

    return PagesResponse(pages=book.pages)


@router.put("/{book_id}/pages/order", response_model=PagesResponse)
async def reorder_pages(book_id: str, payload: PageOrderRequest) -> PagesResponse:
    async with book_store.lock_for(book_id):
        book = require_book(book_id)

        by_id = {page.id: page for page in book.pages}
        if set(payload.order) != set(by_id):
            raise HTTPException(
                status_code=400, detail="order 必须包含且仅包含当前所有页面的 id。"
            )

        book.pages = [by_id[page_id] for page_id in payload.order]
        book_store.save(book)

    return PagesResponse(pages=book.pages)
