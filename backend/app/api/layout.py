"""智能排版端点：整本一键配置、候选框预览、撤销。

和配图那套（`api/illustrate.py`）的分工一样：这里只做**校验 + 建任务 + 转交**，
真正的编排在 `services/auto_layout.py`。
"""

from fastapi import APIRouter, HTTPException

from app.api.books import require_book
from app.api.illustrate import JobCreatedResponse, launch_job
from app.services import auto_layout, job_manager
from app.storage import book_store

router = APIRouter(prefix="/books", tags=["layout"])


# ---------------------------------------------------------------- 一键配置


@router.post("/{book_id}/layout/auto", response_model=JobCreatedResponse)
async def auto_layout_book(book_id: str) -> JobCreatedResponse:
    book = require_book(book_id)
    targets, skips = auto_layout.applicable_pages(book)

    if not targets:
        detail = "这本书没有可以智能排版的页面。"
        if skips:
            detail += f"（{auto_layout.skip_summary(skips)}）"
        raise HTTPException(status_code=400, detail=detail)

    # job 的 items 是**整本书**：跳过的页也要有格子，进度条上显示成灰点带
    # 原因。只放 targets 的话，"15/15 完成"而 5 页悄悄没动，用户会以为
    # 功能坏了。
    numbers = [t.number for t in targets] + [n for n, _ in skips]
    job = await job_manager.create("layout", book_id, sorted(numbers))
    return launch_job(job, auto_layout.run_layout_job(job, book_id))


# ---------------------------------------------------------------- 候选框预览


@router.get("/{book_id}/layout/preview/{number}")
async def layout_preview(book_id: str, number: int) -> dict:
    """这一页的候选框长什么样。

    **只看，不写，不调模型** —— 所以它免费、确定、可以反复看。这是排查"位置
    挑得不对"时的第一抓手：如果候选框里根本没有那块该被选中的空地，问题在
    算法；候选框对了而结果不对，问题在模型或落笔。
    """
    book = require_book(book_id)
    page = book.page_by_number(number)
    if page is None:
        raise HTTPException(status_code=404, detail=f"页码超出范围: {number}")

    target, reason = auto_layout._classify(book, page, number)  # noqa: SLF001
    if target is None:
        return {"applicable": False, "reason": reason, "candidates": []}

    plan = auto_layout._plan(book_id, target)  # noqa: SLF001
    return {
        "applicable": True,
        "reason": "",
        "boxMm": list(target.box),
        "candidates": [
            {
                "xMm": round(rect[0], 1),
                "yMm": round(rect[1], 1),
                "widthMm": round(rect[2], 1),
                "heightMm": round(rect[3], 1),
                "score": round(cand.score, 4),
                "kind": cand.kind,
                "luminance": round(cand.lum, 3),
                "fontSizePt": fitted.fontSizePt,
                "lineHeight": fitted.lineHeight,
                "lines": fitted.lines,
                "overflow": fitted.overflow,
            }
            for cand, rect, fitted in zip(plan.candidates, plan.rects, plan.fitted)
        ],
    }


# ---------------------------------------------------------------- 撤销


@router.get("/{book_id}/layout/undo")
async def layout_undo_status(book_id: str) -> dict:
    """让撤销按钮**刷新页面后仍然正确**。

    按钮永远可见、点了才 404 是很糟的体验，所以前端拿这个决定显示与否。
    """
    require_book(book_id)
    return auto_layout.snapshot_info(book_id)


@router.post("/{book_id}/layout/undo")
async def undo_layout(book_id: str) -> dict:
    if job_manager.has_active("layout", book_id):
        raise HTTPException(status_code=409, detail="智能配置任务正在运行，请先等它结束")

    async with book_store.lock_for(book_id):
        book = require_book(book_id)
        snapshot = auto_layout.read_snapshot(book_id)
        if not snapshot:
            raise HTTPException(
                status_code=404, detail="没有可撤销的智能配置记录（只保留最近一次）"
            )

        restored, skipped = auto_layout.restore_snapshot(book, snapshot)
        book_store.save(book)
        auto_layout.delete_snapshot(book_id)

    return {
        "restored": restored,
        "skipped": skipped,
        "createdAt": snapshot.get("createdAt"),
    }
