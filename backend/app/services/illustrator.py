"""配图任务的编排：生成提示词 → 逐页生图 → 落盘 → 更新 book.json。

每个 job 是一组页。进度通过 job_manager 广播，前端用 SSE 收。

两条必须守住的规则：

1. **每页生成完立刻落盘并写 book.json**。任务中途挂掉时已完成的页不会白做。
2. **失败必须留下可见的占位图**，并把 imageStatus 标成 failed。悄悄跳过会让
   用户以为图都齐了，拿去印刷才发现缺页。
"""

from __future__ import annotations

import asyncio
import json
from typing import List, Optional, Tuple

from app import config
from app.logger import get_logger
from app.models.book import Book, Page, now_iso
from app.render import geometry
from app.services import image_generator, job_manager, placeholder, prompt_builder, text_generator
from app.storage import book_store, paths

logger = get_logger()


# ---------------------------------------------------------------- 图片存储


def next_image_id(book_id: str, page_number: int) -> str:
    """给某页分配下一个版本号。

    按**已有最大版本号 + 1** 算，不是按文件个数 —— 删掉 v2 之后按个数算
    会重新分配出一个已存在的 v3，把旧图覆盖掉。
    """
    prefix = f"p{page_number:03d}_v"
    versions = []
    for file in paths.images_dir(book_id).glob(f"{prefix}*.png"):
        suffix = file.stem[len(prefix) :]
        if suffix.isdigit():
            versions.append(int(suffix))
    return f"{prefix}{max(versions, default=0) + 1}"


def save_image(book_id: str, image_id: str, data: bytes, meta: dict) -> None:
    paths.images_dir(book_id).mkdir(parents=True, exist_ok=True)
    paths.image_file(book_id, image_id).write_bytes(data)
    paths.image_history_file(book_id, image_id).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def page_history(book_id: str, page_number: int) -> List[dict]:
    """某页的所有历史版本，新的在前。供"回滚到上一版"用。"""
    prefix = f"p{page_number:03d}_v"
    entries: List[dict] = []

    for file in paths.images_dir(book_id).glob(f"{prefix}*.png"):
        image_id = file.stem
        meta_file = paths.image_history_file(book_id, image_id)
        meta = {}
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                meta = {}
        entries.append({"imageId": image_id, **meta})

    entries.sort(key=lambda e: e.get("createdAt", ""), reverse=True)
    return entries


# ---------------------------------------------------------------- 生成提示词


async def run_prompts_job(job: job_manager.Job, book_id: str, targets: List[int]) -> None:
    """给 targets 这几页生成提示词。

    targets 是**已经定好**的页码列表，这里不再过滤。过滤（跳过已有提示词的页）
    由调用方做 —— 因为"重新生成某一页"要的正是覆盖，在这里再过滤一次会把
    用户明确要求的重生成悄悄吃掉。
    """
    await job_manager.set_status(job, "running")

    book = book_store.load_or_none(book_id)
    if book is None:
        await job_manager.set_status(job, "failed", "书籍不存在")
        return

    if not targets:
        await job_manager.set_status(job, "done", "没有需要生成提示词的页面")
        for item in job.items:
            await job_manager.mark(job, item.pageNumber, "done")
        return

    job.total = len(targets)
    job.items = [job_manager.JobItem(pageNumber=n) for n in targets]
    await job_manager.publish(job)

    for number in targets:
        await job_manager.mark(job, number, "running")

    texts = [prompt_builder.page_text_for_prompt(book.pages[n - 1]) for n in targets]
    # 按页码算好每页该用的镜头语言，交给模型照着写画面
    design_hints = [
        prompt_builder.design_hint(prompt_builder.design_for_page(n)) for n in targets
    ]

    try:
        prompts = await text_generator.generate_prompts_batch(
            texts, book.characterDescription, design_hints
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("生成提示词失败: %s", exc)
        for number in targets:
            await job_manager.mark(job, number, "failed", error=str(exc))
        await job_manager.set_status(job, "failed", f"生成提示词失败：{exc}")
        return

    async with book_store.lock_for(book_id):
        fresh = book_store.load_or_none(book_id)
        if fresh is None:
            await job_manager.set_status(job, "failed", "书籍不存在")
            return
        for number, prompt in zip(targets, prompts):
            page = fresh.page_by_number(number)
            if page is not None:
                page.imagePrompt = prompt
        book_store.save(fresh)

    for number in targets:
        await job_manager.mark(job, number, "done")

    await job_manager.set_status(job, "done", f"已为 {len(targets)} 页生成提示词")


# ---------------------------------------------------------------- 生图


async def _illustrate_one(
    job: job_manager.Job,
    book_id: str,
    number: int,
    service: str,
    size: str,
    instruction: str = "",
) -> None:
    await job_manager.mark(job, number, "running")

    book = book_store.load_or_none(book_id)
    if book is None:
        await job_manager.mark(job, number, "failed", error="书籍不存在")
        return

    page = book.page_by_number(number)
    if page is None:
        await job_manager.mark(job, number, "failed", error="页码超出范围")
        return

    if not page.imagePrompt.strip():
        await job_manager.mark(job, number, "failed", error="这一页还没有提示词，请先生成提示词")
        return

    # 带上页码：镜头语言按页序轮转，页码是唯一的输入
    final_prompt = prompt_builder.enhance_prompt(page.imagePrompt, book, page, number)
    if instruction:
        try:
            final_prompt = await text_generator.refine_prompt(final_prompt, instruction)
        except Exception as exc:  # noqa: BLE001
            logger.warning("第 %d 页改写提示词失败，改用原提示词: %s", number, exc)

    error = ""
    is_placeholder = False
    try:
        data = await image_generator.generate_image(final_prompt, size, service)
    except Exception as exc:  # noqa: BLE001
        logger.error("第 %d 页生图失败: %s", number, exc)
        error = str(exc)
        is_placeholder = True
        data = placeholder.make_from_size(
            size, detail=f"第 {number} 页", prompt=final_prompt
        )

    image_id = next_image_id(book_id, number)
    save_image(
        book_id,
        image_id,
        data,
        {
            "pageNumber": number,
            "prompt": final_prompt,
            "basePrompt": page.imagePrompt,
            "service": service,
            "size": size,
            "placeholder": is_placeholder,
            "createdAt": now_iso(),
        },
    )

    async with book_store.lock_for(book_id):
        fresh = book_store.load_or_none(book_id)
        target = fresh.page_by_number(number) if fresh else None
        if target is not None:
            target.imageId = image_id
            target.imageStatus = "failed" if is_placeholder else "done"
            book_store.save(fresh)

    await job_manager.mark(
        job,
        number,
        "failed" if is_placeholder else "done",
        imageId=image_id,
        error=error,
    )


async def run_illustrate_job(
    job: job_manager.Job,
    book_id: str,
    page_numbers: List[int],
    instruction: str = "",
) -> None:
    await job_manager.set_status(job, "running")

    book = book_store.load_or_none(book_id)
    if book is None:
        await job_manager.set_status(job, "failed", "书籍不存在")
        return

    reason = image_generator.service_unavailable_reason(book.imageService)
    if reason:
        await job_manager.set_status(job, "failed", reason)
        for item in job.items:
            await job_manager.mark(job, item.pageNumber, "failed", error=reason)
        return

    size = geometry.image_size_for_service(
        book.trimSize.widthMm, book.trimSize.heightMm, book.imageService
    )
    logger.info(
        "开始配图：%d 页，服务 %s，尺寸 %s", len(page_numbers), book.imageService, size
    )

    # 并发由 image_generator 内部的 Semaphore 限制，这里只是同时发出去，
    # 让"某一页慢"不至于把后面的页全堵住。
    await asyncio.gather(
        *(
            _illustrate_one(job, book_id, number, book.imageService, size, instruction)
            for number in page_numbers
        )
    )

    if job_manager.is_cancelled(job):
        await job_manager.set_status(job, "cancelled", "已取消")
        return

    failed = job.failed
    if failed == job.total:
        await job_manager.set_status(job, "failed", f"全部 {failed} 页生成失败")
    elif failed:
        await job_manager.set_status(
            job, "done", f"完成 {job.completed} 页，{failed} 页失败（已用占位图替代）"
        )
    else:
        await job_manager.set_status(job, "done", f"已完成 {job.completed} 页配图")


# ---------------------------------------------------------------- 收尾


def reset_stuck_pages() -> int:
    """启动时把卡在 running 的页重置。

    进程被杀时任务表就没了，但 book.json 里 imageStatus 还写着 running，
    前端会一直显示"生成中"。启动时统一清成 pending。
    """
    fixed = 0
    for summary in book_store.list_all():
        book = book_store.load(summary.id)
        if book is None:
            continue
        changed = False
        for page in book.pages:
            if page.imageStatus == "running":
                page.imageStatus = "pending" if page.imagePrompt else "none"
                changed = True
                fixed += 1
        if changed:
            book_store.save(book)
    return fixed


def resolve_page_numbers(book: Book, requested: Optional[List[int]]) -> Tuple[List[int], str]:
    """把请求里的页码规整成合法列表，返回 (页码, 错误信息)。"""
    total = len(book.pages)
    if total == 0:
        return [], "这本书还没有页面，请先在「故事」步骤里分页。"

    if not requested:
        return list(range(1, total + 1)), ""

    cleaned = sorted({n for n in requested if 1 <= n <= total})
    if not cleaned:
        return [], f"页码都不在 1..{total} 范围内。"
    return cleaned, ""
