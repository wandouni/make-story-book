"""静态资源：插图与自托管字体。

插图有两种取法：

- `.png` —— 原图，给前端缩略图用
- `.jpg?box=WxH&fit=cover|contain` —— 按**实际放置尺寸**重采样后的图

重采样放在这里而不是导出流程里，是为了让预览和导出走同一条路：两边都
请求"这个盒子需要多少像素"，只是一个按 150dpi 算、一个按 300dpi 算。
导出前不需要预处理一批临时文件，也就没有"临时文件过期了没"这类问题。
"""

import io

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response
from PIL import Image

from app import config
from app.logger import get_logger
from app.render import geometry
from app.storage import paths

logger = get_logger()

router = APIRouter(prefix="/assets", tags=["assets"])

# JPEG 质量：92 在 300dpi 下肉眼与 PNG 无差别，体积只有十分之一左右。
_JPEG_QUALITY = 92

# 单边上限，挡住畸形请求（?box=999999x999999）把内存吃光
_MAX_SIDE = 8000


def _parse_box(box: str) -> tuple:
    try:
        width, height = box.lower().split("x")
        return int(width), int(height)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail=f"box 格式应为 WxH，收到: {box!r}") from None


@router.get("/books/{book_id}/images/{image_id}.png")
async def book_image(book_id: str, image_id: str) -> FileResponse:
    try:
        file = paths.image_file(book_id, image_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="非法图片 id") from None

    if not file.exists():
        raise HTTPException(status_code=404, detail=f"图片不存在: {image_id}")
    # 图片 id 不可变，内容不会变，可以放心长缓存
    return FileResponse(file, media_type="image/png", headers={"Cache-Control": "max-age=31536000"})


@router.get("/books/{book_id}/images/{image_id}.jpg")
async def book_image_resized(
    book_id: str,
    image_id: str,
    box: str = Query(..., description="放置盒子的像素尺寸，形如 1240x1754"),
    fit: str = Query("cover", pattern="^(cover|contain)$"),
) -> Response:
    try:
        source = paths.image_file(book_id, image_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="非法图片 id") from None

    if not source.exists():
        raise HTTPException(status_code=404, detail=f"图片不存在: {image_id}")

    box_w, box_h = _parse_box(box)
    if not (0 < box_w <= _MAX_SIDE and 0 < box_h <= _MAX_SIDE):
        raise HTTPException(status_code=400, detail=f"box 超出允许范围: {box}")

    cache = paths.image_cache_file(book_id, image_id, box_w, box_h, fit)
    if not cache.exists():
        try:
            _resize_to(source, cache, box_w, box_h, fit)
        except OSError as exc:
            # 图坏了不该让整本书导不出来：退回原图，让 PDF 里至少有个东西
            logger.error("重采样 %s 失败（%s），退回原图", image_id, exc)
            return FileResponse(source, media_type="image/png")

    return FileResponse(
        cache, media_type="image/jpeg", headers={"Cache-Control": "max-age=31536000"}
    )


def _resize_to(source, cache, box_w: int, box_h: int, fit: str) -> None:
    with Image.open(source) as img:
        target = geometry.resample_size(img.width, img.height, box_w, box_h, fit)
        if target is None:
            # 原图还没这个盒子清晰 —— 缩小只会更糊。原样转 JPEG 存下来，
            # 体积上依然划算（PNG 换成 JPEG）。
            out = img
        else:
            out = img.resize(target, Image.LANCZOS)

        # 生成图都是不透明的，但万一有 alpha，转 JPEG 会报错
        if out.mode not in ("RGB", "L"):
            out = out.convert("RGB")

        cache.parent.mkdir(parents=True, exist_ok=True)
        buffer = io.BytesIO()
        out.save(buffer, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
        # 原子写：并发的预览和导出可能同时请求同一张图，写到一半被读到
        # 会得到半张图（表现是页面上一条灰带，很难查）
        tmp = cache.with_name(cache.name + ".tmp")
        tmp.write_bytes(buffer.getvalue())
        tmp.replace(cache)


@router.get("/fonts/{filename}")
async def font_file(filename: str) -> FileResponse:
    # 只允许取 fonts 目录下的文件，挡掉 ../ 之类的输入
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="非法文件名")

    file = config.FONTS_DIR / filename
    if not file.exists() or not file.is_file():
        raise HTTPException(status_code=404, detail=f"字体不存在: {filename}")

    return FileResponse(file, media_type="font/woff2")
