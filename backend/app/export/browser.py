"""Playwright 浏览器单例。

导出 PDF 和预览截图都要开 Chromium，两边共用这一个 —— 每次 `launch()` 要
300ms 以上，各自起一个既慢又会攒下一堆孤儿进程。

单独抽出来是因为它有两个使用者：`pdf_exporter`（整本导 PDF）和
`services/page_shot`（给 3D 翻页截单页纹理）。放在其中一边、另一边去
import 对方的私有函数，会把"谁是主"这件事变得含糊。
"""

import asyncio
from typing import Optional

from playwright.async_api import Browser, Playwright, async_playwright

from app.logger import get_logger

logger = get_logger()

_playwright: Optional[Playwright] = None
_browser: Optional[Browser] = None
_launch_lock = asyncio.Lock()


async def get_browser() -> Browser:
    global _playwright, _browser

    async with _launch_lock:
        if _browser is not None and _browser.is_connected():
            return _browser

        if _playwright is None:
            _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch()
        logger.info("Playwright Chromium 已启动")
        return _browser


async def shutdown() -> None:
    """服务退出时收尾，别留下孤儿 Chromium 进程。"""
    global _playwright, _browser
    if _browser is not None:
        await _browser.close()
        _browser = None
    if _playwright is not None:
        await _playwright.stop()
        _playwright = None
