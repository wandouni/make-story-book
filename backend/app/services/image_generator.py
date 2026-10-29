"""文生图：豆包（火山 ARK）与通义万相。

**两个 key 绝不能混用**：豆包分支只读 ARK_API_KEY，通义分支只读 API_KEY。
StoryCraft 里 api_key 参数在豆包分支被完全忽略，移植时保留了这个行为，
但构造时显式断言 key 非空 —— 否则会静默 401，用户只看到一堆占位图。

**返回的图片 URL 会过期，必须立刻下载落盘**，不能存 URL 到 book.json。

移植自 StoryCraft 的 image_generator，改动：
- httpx.Client → AsyncClient，time.sleep → asyncio.sleep（同步会阻塞事件循环）
- 逐页生成而非豆包组图：进度条要能一页一页地走，组图一次性返回反而没法报进度
- 尺寸不再全局配置，由 geometry.image_size_for_service 按开本+服务算出
"""

from __future__ import annotations

import asyncio
from typing import Optional

import httpx

from app import config
from app.logger import get_logger

logger = get_logger()

# 两家的 QPS 限制都不宽松，串行 + 间隔最稳。Semaphore 允许少量并发，
# 间隔由 acquire 后的 sleep 保证。
_semaphore: Optional[asyncio.Semaphore] = None
_last_request_at = 0.0
_throttle_lock = asyncio.Lock()


class ImageGenerationError(RuntimeError):
    pass


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(config.IMAGE_CONCURRENCY)
    return _semaphore


async def _throttle() -> None:
    """保证两次请求之间至少间隔 IMAGE_REQUEST_INTERVAL 秒。"""
    global _last_request_at
    async with _throttle_lock:
        now = asyncio.get_running_loop().time()
        wait = _last_request_at + config.IMAGE_REQUEST_INTERVAL - now
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at = asyncio.get_running_loop().time()


async def _download(client: httpx.AsyncClient, url: str) -> bytes:
    """立刻下载 —— 生成接口返回的 URL 有有效期，存 URL 等于存了一个定时炸弹。"""
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = await client.get(url, timeout=60 + attempt * 30)
            response.raise_for_status()
            return response.content
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
            last_error = exc
            logger.warning("下载图片第 %d 次失败: %s", attempt + 1, exc)
            await asyncio.sleep(2 * (attempt + 1))
    raise ImageGenerationError(f"下载生成的图片失败: {last_error}")


async def _call_doubao(prompt: str, size: str) -> bytes:
    from volcenginesdkarkruntime import Ark

    if not config.ARK_API_KEY:
        raise ImageGenerationError("未配置 ARK_API_KEY，无法使用豆包生图。")

    client = Ark(base_url=config.ARK_BASE_URL, api_key=config.ARK_API_KEY)
    logger.info("豆包生图，模型 %s，尺寸 %s", config.DOUBAO_IMAGE_MODEL, size)

    response = await asyncio.to_thread(
        client.images.generate,
        model=config.DOUBAO_IMAGE_MODEL,
        prompt=prompt,
        size=size,
        response_format="url",
        watermark=False,
    )

    if not response.data or not response.data[0].url:
        raise ImageGenerationError("豆包接口未返回任何图片")

    async with httpx.AsyncClient() as http:
        return await _download(http, response.data[0].url)


async def _call_tongyi(prompt: str, size: str) -> bytes:
    if not config.API_KEY:
        raise ImageGenerationError("未配置 API_KEY，无法使用通义万相生图。")

    # 通义用 * 作分隔符，不是 x
    qwen_size = size.replace("x", "*")
    payload = {
        "model": config.TONGYI_IMAGE_MODEL,
        "input": {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
        "parameters": {"prompt_extend": True, "watermark": False, "n": 1, "size": qwen_size},
    }
    headers = {
        "Authorization": f"Bearer {config.API_KEY}",
        "Content-Type": "application/json",
    }

    logger.info("通义生图，模型 %s，尺寸 %s", config.TONGYI_IMAGE_MODEL, qwen_size)
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=30, read=180, write=60, pool=30)) as http:
        response = await http.post(
            "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

        try:
            image_url = data["output"]["choices"][0]["message"]["content"][0]["image"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ImageGenerationError(f"通义响应格式异常: {data}") from exc

        return await _download(http, image_url)


async def generate_image(prompt: str, size: str, service: str) -> bytes:
    """生成一张图，返回图片字节。

    失败时抛异常，由调用方决定是否降级为占位图 —— 这里不擅自替用户
    决定"假装成功了"。
    """
    async with _get_semaphore():
        await _throttle()
        if service == "doubao":
            return await _call_doubao(prompt, size)
        return await _call_tongyi(prompt, size)


def service_unavailable_reason(service: str) -> str:
    """生图服务为什么不可用；可用时返回空串。

    比布尔值多一个原因，是因为"没填 key"和"key 跟服务厂商对不上"要给
    完全不同的指引 —— 前者让用户去填，后者让用户去改配置。
    """
    if service == "doubao":
        if not config.ARK_API_KEY:
            return "未配置 ARK_API_KEY，无法调用豆包生图。"
        return ""

    if not config.API_KEY:
        return "未配置 API_KEY，无法调用通义万相生图。"

    # 通义万相是阿里 DashScope 的服务，只认 DashScope 的 key。本项目把
    # API_ENDPOINT 做成了可配的 OpenAI 兼容端点（方便指向火山方舟等），
    # 一旦指向别家，同一把 key 发给 DashScope 只会得到 InvalidApiKey。
    # 这种情况在开跑前就说清楚，否则用户会看到整本配图全失败。
    if "dashscope" not in config.API_ENDPOINT:
        return (
            f"通义万相需要 DashScope 的 API_KEY，但当前 API_ENDPOINT 指向 "
            f"{config.API_ENDPOINT}（非 DashScope）。请改用豆包生图，"
            f"或把 API_ENDPOINT 与 API_KEY 一并换成 DashScope 的。"
        )
    return ""


def service_available(service: str) -> bool:
    return not service_unavailable_reason(service)
