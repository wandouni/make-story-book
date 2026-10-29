"""任务查询与 SSE 进度流。

SSE 走 Vite 代理时不能有缓冲/压缩中间件介入，否则进度会攒着一次性吐出，
看起来像卡住了。所以显式带 X-Accel-Buffering: no。

EventSource 不能带自定义请求头，所以这个端点不加 header 鉴权 ——
本地单机也不需要。
"""

import asyncio
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.services import job_manager

router = APIRouter(prefix="/jobs", tags=["jobs"])

_TERMINAL = ("done", "failed", "cancelled")
_HEARTBEAT_SECONDS = 15


def _require_job(job_id: str) -> job_manager.Job:
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail=f"任务不存在: {job_id}（服务重启会清空任务表）",
        )
    return job


@router.get("/{job_id}")
async def get_job(job_id: str) -> dict:
    """轮询兜底。SSE 断线时前端可以退回轮询。"""
    return _require_job(job_id).to_dict()


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict:
    job = _require_job(job_id)
    if not job_manager.request_cancel(job):
        return {"ok": False, "reason": "任务已经结束"}
    return {"ok": True}


@router.get("/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
    job = _require_job(job_id)

    async def stream() -> AsyncIterator[str]:
        queue = job_manager.subscribe(job)
        try:
            # 先补一帧当前快照：订阅之前已经发生的进度不会丢
            snapshot = job.to_dict()
            yield job_manager.sse_format(snapshot)
            if snapshot["status"] in _TERMINAL:
                return

            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    # 注释行不会被 EventSource 当成事件，只为把连接撑住
                    yield ": ping\n\n"
                    continue

                yield job_manager.sse_format(payload)
                if payload["status"] in _TERMINAL:
                    return
        finally:
            job_manager.unsubscribe(job, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
