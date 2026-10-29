"""内存任务表 + SSE 广播。

**uvicorn 必须 workers=1**（见 run.py）：任务表活在进程内存里，多 worker 时
一次请求可能落到没有该任务的进程上，表现为进度条永远停在 0。

任务重启即丢。单机单用户场景下这可以接受 —— 但 book.json 里的
`imageStatus` 会残留 running，所以启动时要重置（见 reset_stuck_pages）。

SSE 每帧推的都是**完整快照**，前端直接整体替换，不做增量合并。
慢客户端漏掉一帧也不会算错进度，代价只是每帧大一点。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.logger import get_logger
from app.models.book import now_iso

logger = get_logger()

MAX_JOBS = 100


@dataclass
class JobItem:
    pageNumber: int
    # pending / running / done / failed / skipped
    #
    # `skipped` 是智能排版加的：那一页**本来就不该被处理**（版式不需要、
    # 还没有插图……），和"处理了但失败了"是两回事。混在一起的话，用户看到
    # 15/15 已完成而 5 页悄悄没动，会以为功能坏了。
    #
    # 现有三个 kind（提示词/生图/精修）从不发它，计数恒为 0。
    status: str = "pending"
    imageId: Optional[str] = None
    # 失败原因**或跳过原因**。前端进度条的 tooltip 本来就是
    # `第 N 页：${error || status}`，跳过原因放这里读起来是自然的，
    # 不值得为它再加一个字段。
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pageNumber": self.pageNumber,
            "status": self.status,
            "imageId": self.imageId,
            "error": self.error,
        }


@dataclass
class Job:
    id: str
    kind: str  # illustrate / refine / export
    bookId: str
    total: int
    items: List[JobItem]
    status: str = "pending"  # pending / running / done / failed / cancelled
    message: str = ""
    createdAt: str = field(default_factory=now_iso)

    # 不进序列化：订阅者队列和底层任务
    subscribers: List[asyncio.Queue] = field(default_factory=list)
    task: Optional[asyncio.Task] = None
    cancel_requested: bool = False

    @property
    def completed(self) -> int:
        return sum(1 for item in self.items if item.status == "done")

    @property
    def failed(self) -> int:
        return sum(1 for item in self.items if item.status == "failed")

    @property
    def skipped(self) -> int:
        return sum(1 for item in self.items if item.status == "skipped")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "bookId": self.bookId,
            "status": self.status,
            "message": self.message,
            "total": self.total,
            "completed": self.completed,
            "failed": self.failed,
            "skipped": self.skipped,
            "items": [item.to_dict() for item in self.items],
            "createdAt": self.createdAt,
        }


_jobs: Dict[str, Job] = {}
_lock = asyncio.Lock()


async def create(kind: str, book_id: str, page_numbers: List[int]) -> Job:
    job = Job(
        id=f"j_{uuid.uuid4().hex[:12]}",
        kind=kind,
        bookId=book_id,
        total=len(page_numbers),
        items=[JobItem(pageNumber=number) for number in page_numbers],
    )
    async with _lock:
        _jobs[job.id] = job
        _prune()
    return job


def get(job_id: str) -> Optional[Job]:
    return _jobs.get(job_id)


def has_active(kind: str, book_id: str) -> bool:
    """这本书有没有该类型的任务还没跑完。

    智能排版的撤销靠它挡住"撤销和任务同时写同一本书"—— 那样用户看到的是
    "撤销了但只撤销了一半"。
    """
    return any(
        job.kind == kind and job.bookId == book_id
        and job.status not in ("done", "failed", "cancelled")
        for job in _jobs.values()
    )


def active(kind: str, book_id: str) -> Optional[Job]:
    """这本书某类任务中还没结束的那个，没有则 None。"""
    return next(
        (
            job
            for job in _jobs.values()
            if job.kind == kind
            and job.bookId == book_id
            and job.status not in ("done", "failed", "cancelled")
        ),
        None,
    )


def _prune() -> None:
    """只保留最近的若干任务，避免长时间运行后内存里堆一堆已完成的 job。"""
    if len(_jobs) <= MAX_JOBS:
        return
    finished = [j for j in _jobs.values() if j.status in ("done", "failed", "cancelled")]
    finished.sort(key=lambda j: j.createdAt)
    for job in finished[: len(_jobs) - MAX_JOBS]:
        _jobs.pop(job.id, None)


# ---------------------------------------------------------------- 广播


async def publish(job: Job) -> None:
    """把当前快照推给所有订阅者。

    队列满说明这个客户端读得太慢 —— 直接丢弃这一帧即可，下一帧是全量快照，
    不会因此算错。阻塞在这里反而会拖慢整个生成任务。
    """
    payload = job.to_dict()
    for queue in list(job.subscribers):
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            logger.warning("任务 %s 的订阅者队列已满，丢弃一帧", job.id)


def subscribe(job: Job) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    job.subscribers.append(queue)
    return queue


def unsubscribe(job: Job, queue: asyncio.Queue) -> None:
    if queue in job.subscribers:
        job.subscribers.remove(queue)


async def set_status(job: Job, status: str, message: str = "") -> None:
    job.status = status
    if message:
        job.message = message
    await publish(job)


async def mark(job: Job, page_number: int, status: str, **fields: Any) -> None:
    for item in job.items:
        if item.pageNumber == page_number:
            item.status = status
            for key, value in fields.items():
                setattr(item, key, value)
            break
    await publish(job)


def sse_format(payload: Dict[str, Any]) -> str:
    """SSE 帧。data 里不能有裸换行，用 json.dumps 保证转义。"""
    return f"event: progress\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def request_cancel(job: Job) -> bool:
    if job.status in ("done", "failed", "cancelled"):
        return False
    job.cancel_requested = True
    return True


def is_cancelled(job: Job) -> bool:
    return job.cancel_requested
