"""配图相关端点：生成提示词、批量生图、单页重绘、历史回滚。"""

import asyncio
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.books import require_book
from app.services import illustrator, job_manager, prompt_builder
from app.storage import book_store, paths

router = APIRouter(prefix="/books", tags=["illustrate"])


class JobCreatedResponse(BaseModel):
    jobId: str
    total: int


class PromptsRequest(BaseModel):
    pageNumbers: Optional[List[int]] = None
    # 默认跳过已有提示词的页（pages_needing_prompts 的语义）—— 批量生成时
    # 不该覆盖用户手改过的提示词。但"重新生成这一页"要求覆盖，否则用户得
    # 先手工清空 textarea 才点得动。
    overwrite: bool = False


class IllustrateRequest(BaseModel):
    pageNumbers: Optional[List[int]] = None


class RefineRequest(BaseModel):
    instruction: str


class SelectImageRequest(BaseModel):
    imageId: str


def launch_job(job: job_manager.Job, coro) -> JobCreatedResponse:
    job.task = asyncio.create_task(coro)
    return JobCreatedResponse(jobId=job.id, total=job.total)


# ---------------------------------------------------------------- 提示词


@router.post("/{book_id}/prompts/generate", response_model=JobCreatedResponse)
async def generate_prompts(book_id: str, payload: PromptsRequest) -> JobCreatedResponse:
    book = require_book(book_id)
    numbers, error = illustrator.resolve_page_numbers(book, payload.pageNumbers)
    if error:
        raise HTTPException(status_code=400, detail=error)

    targets = numbers if payload.overwrite else prompt_builder.pages_needing_prompts(
        book.pages, numbers
    )
    if not targets:
        raise HTTPException(
            status_code=400,
            detail="所选页面都已有提示词。若要重新生成，请先清空对应页面的提示词。",
        )

    job = await job_manager.create("prompts", book_id, targets)
    return launch_job(job, illustrator.run_prompts_job(job, book_id, targets))


# ---------------------------------------------------------------- 生图


@router.post("/{book_id}/illustrate", response_model=JobCreatedResponse)
async def illustrate(book_id: str, payload: IllustrateRequest) -> JobCreatedResponse:
    book = require_book(book_id)
    numbers, error = illustrator.resolve_page_numbers(book, payload.pageNumbers)
    if error:
        raise HTTPException(status_code=400, detail=error)

    missing = [n for n in numbers if not book.pages[n - 1].imagePrompt.strip()]
    if missing:
        preview = ", ".join(str(n) for n in missing[:5])
        more = " 等" if len(missing) > 5 else ""
        raise HTTPException(
            status_code=400,
            detail=f"第 {preview}{more} 页还没有提示词，请先生成提示词。",
        )

    job = await job_manager.create("illustrate", book_id, numbers)
    return launch_job(job, illustrator.run_illustrate_job(job, book_id, numbers))


@router.post("/{book_id}/pages/{number}/illustrate", response_model=JobCreatedResponse)
async def illustrate_page(book_id: str, number: int) -> JobCreatedResponse:
    book = require_book(book_id)
    page = book.page_by_number(number)
    if page is None:
        raise HTTPException(status_code=404, detail=f"页码超出范围: {number}")
    if not page.imagePrompt.strip():
        raise HTTPException(status_code=400, detail="这一页还没有提示词，请先生成提示词。")

    job = await job_manager.create("illustrate", book_id, [number])
    return launch_job(job, illustrator.run_illustrate_job(job, book_id, [number]))


@router.post("/{book_id}/pages/{number}/refine", response_model=JobCreatedResponse)
async def refine_page(
    book_id: str, number: int, payload: RefineRequest
) -> JobCreatedResponse:
    """按自然语言要求重画某一页，例如"把背景改成夜晚"。"""
    book = require_book(book_id)
    page = book.page_by_number(number)
    if page is None:
        raise HTTPException(status_code=404, detail=f"页码超出范围: {number}")
    if not page.imagePrompt.strip():
        raise HTTPException(status_code=400, detail="这一页还没有提示词，无法改写。")
    if not payload.instruction.strip():
        raise HTTPException(status_code=400, detail="请填写修改要求。")

    job = await job_manager.create("refine", book_id, [number])
    return _launch(
        job,
        illustrator.run_illustrate_job(job, book_id, [number], instruction=payload.instruction.strip()),
    )


# ---------------------------------------------------------------- 历史


@router.get("/{book_id}/pages/{number}/image-history")
async def image_history(book_id: str, number: int) -> dict:
    book = require_book(book_id)
    page = book.page_by_number(number)
    if page is None:
        raise HTTPException(status_code=404, detail=f"页码超出范围: {number}")

    return {"current": page.imageId, "versions": illustrator.page_history(book_id, number)}


@router.post("/{book_id}/pages/{number}/image/select")
async def select_image(book_id: str, number: int, payload: SelectImageRequest) -> dict:
    """回滚到某个历史版本。图片文件一直留着，所以这只是一次指针切换。"""
    try:
        paths.safe_id(payload.imageId)
    except ValueError:
        raise HTTPException(status_code=400, detail="非法图片 id") from None

    async with book_store.lock_for(book_id):
        book = require_book(book_id)
        page = book.page_by_number(number)
        if page is None:
            raise HTTPException(status_code=404, detail=f"页码超出范围: {number}")

        if not paths.image_file(book_id, payload.imageId).exists():
            raise HTTPException(status_code=404, detail=f"图片不存在: {payload.imageId}")

        page.imageId = payload.imageId
        page.imageStatus = "done"
        book_store.save(book)

    return {"page": page.model_dump(mode="json")}
