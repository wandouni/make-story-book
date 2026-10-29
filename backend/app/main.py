"""FastAPI 应用入口。"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import config
from app.api import (
    assets,
    books,
    cover,
    export,
    health,
    illustrate,
    jobs,
    layout,
    meta,
    pages,
    render,
)
from app.export import browser
from app.logger import get_logger
from app.services import illustrator

logger = get_logger()

app = FastAPI(title="make-story-book", version=config.APP_VERSION)

# 前端走 Vite proxy，本不需要 CORS；保留宽松配置便于 curl / 调试
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api")
app.include_router(meta.router, prefix="/api")
app.include_router(books.router, prefix="/api")
app.include_router(pages.router, prefix="/api")
app.include_router(illustrate.router, prefix="/api")
app.include_router(cover.router, prefix="/api")
app.include_router(jobs.router, prefix="/api")
app.include_router(layout.router, prefix="/api")
app.include_router(render.router, prefix="/api")
app.include_router(export.router, prefix="/api")
app.include_router(assets.router, prefix="/api")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    # 不复用浏览器进程的话，反复重启服务会攒下一堆孤儿 Chromium
    await browser.shutdown()


@app.on_event("startup")
async def on_startup() -> None:
    config.BOOKS_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("数据目录: %s", config.DATA_DIR)

    status = health.chromium_status()
    if status["ready"]:
        logger.info("Chromium 就绪，PDF 导出可用")
    else:
        logger.warning("PDF 导出不可用：%s", status["reason"])

    if not config.API_KEY and not config.ARK_API_KEY:
        logger.warning("未配置任何 API key，生图功能不可用（见 backend/.env.example）")

    # 进程被杀时任务表会丢，但 book.json 里还写着 running，前端会一直转圈。
    stuck = illustrator.reset_stuck_pages()
    if stuck:
        logger.info("重置了 %d 个卡在 running 的页面状态", stuck)
