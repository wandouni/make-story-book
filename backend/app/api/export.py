"""PDF 导出端点。

导出是**同步**的，不走任务表：一次导出几秒钟就完事，而任务表是为
"几十张图、每张要等十几秒"的生图设计的。为它套一层 job 只会让前端多写
一套 SSE 订阅，换不来任何东西。真有几十页的大书变慢了再迁也不迟。
"""

from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.api.books import require_book
from app.api.health import chromium_status
from app.export import pdf_exporter
from app.storage import paths

router = APIRouter(prefix="/books/{book_id}", tags=["export"])

ExportKind = Literal["preview", "print"]


class ExportRequest(BaseModel):
    kind: ExportKind = "print"


class ExportResponse(BaseModel):
    kind: ExportKind
    filename: str
    url: str
    report: dict


@router.post("/export", response_model=ExportResponse)
async def export_book(book_id: str, payload: ExportRequest) -> ExportResponse:
    status = chromium_status()
    if not status["ready"]:
        raise HTTPException(status_code=503, detail=f"PDF 导出不可用：{status['reason']}")

    book = require_book(book_id)
    if not book.pages:
        raise HTTPException(status_code=400, detail="还没有页面，先到「故事」步骤分页。")

    path = await pdf_exporter.export_pdf(book, payload.kind)
    report = pdf_exporter.build_report(book, payload.kind, path)

    return ExportResponse(
        kind=payload.kind,
        filename=path.name,
        url=f"/api/books/{book_id}/exports/{path.name}",
        report=report,
    )


@router.get("/exports/{filename}")
async def download_export(book_id: str, filename: str, inline: int = 0) -> FileResponse:
    # 只允许取 exports 目录下的文件，挡掉 ../ 之类的输入
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="非法文件名")

    try:
        file = paths.exports_dir(book_id) / filename
    except ValueError:
        raise HTTPException(status_code=400, detail="非法书籍 id") from None

    if not file.exists() or not file.is_file():
        raise HTTPException(status_code=404, detail=f"导出文件不存在: {filename}")

    disposition = "inline" if inline else "attachment"
    return FileResponse(
        file,
        media_type="application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )
