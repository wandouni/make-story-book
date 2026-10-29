"""封面：生成 AI 封面背景图。"""

import asyncio

from fastapi import APIRouter

from app.api.books import require_book
from app.services import cover, job_manager
from app.api.illustrate import JobCreatedResponse, launch_job

router = APIRouter(prefix="/books", tags=["cover"])


@router.post("/{book_id}/cover/generate", response_model=JobCreatedResponse)
async def generate_cover(book_id: str) -> JobCreatedResponse:
    require_book(book_id)

    running = job_manager.active("cover", book_id)
    if running is not None:
        return JobCreatedResponse(jobId=running.id, total=running.total)

    job = await job_manager.create("cover", book_id, [1])
    return launch_job(job, cover.run_cover_job(job, book_id))
