"""封面背景图：AI 生成一张切合故事主题的封面底图。

和内页配图共用同一个生图服务，但提示词走 prompt_builder.cover_prompt ——
封面要叠书名与作者，构图必须给文字留出干净区域。
"""

from __future__ import annotations

from app.logger import get_logger
from app.models.book import now_iso
from app.render import geometry
from app.services import image_generator, illustrator, job_manager, prompt_builder
from app.storage import book_store, paths

logger = get_logger()


def next_cover_id(book_id: str) -> str:
    """cover_v1、cover_v2… 版本号按已有最大值 + 1。"""
    prefix = "cover_v"
    versions = []
    for file in paths.images_dir(book_id).glob(f"{prefix}*.png"):
        suffix = file.stem[len(prefix) :]
        if suffix.isdigit():
            versions.append(int(suffix))
    return f"{prefix}{max(versions, default=0) + 1}"


async def run_cover_job(job: job_manager.Job, book_id: str) -> None:
    await job_manager.mark(job, 1, "running")

    book = book_store.load_or_none(book_id)
    if book is None:
        await job_manager.mark(job, 1, "failed", error="书籍不存在")
        return

    service = book.imageService
    reason = image_generator.service_unavailable_reason(service)
    if reason:
        await job_manager.mark(job, 1, "failed", error=reason)
        return

    size = geometry.image_size_for_service(
        book.trimSize.widthMm, book.trimSize.heightMm, service
    )
    final_prompt = prompt_builder.cover_prompt(book)

    try:
        data = await image_generator.generate_image(final_prompt, size, service)
    except Exception as exc:  # noqa: BLE001
        logger.error("封面生图失败: %s", exc)
        await job_manager.set_status(job, "failed", f"封面生成失败: {exc}")
        return

    image_id = next_cover_id(book_id)
    illustrator.save_image(
        book_id,
        image_id,
        data,
        {
            "prompt": final_prompt,
            "service": service,
            "size": size,
            "createdAt": now_iso(),
        },
    )

    async with book_store.lock_for(book_id):
        fresh = book_store.load_or_none(book_id)
        if fresh is not None:
            fresh.coverImageId = image_id
            book_store.save(fresh)

    await job_manager.mark(job, 1, "done", imageId=image_id)
    await job_manager.set_status(job, "done", "封面已生成")
