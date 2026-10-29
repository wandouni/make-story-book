"""失败占位图。

生图失败时**必须给一张肉眼可见写着"生成失败"的图**，而不是留空或悄悄
用上一张。否则用户会拿着假图去印刷，印出来才发现问题。

这里用系统字体是刻意的：占位图只是给人看的提示，不参与"预览与导出一致"
那条约束（那条约束针对的是 book.css 里渲染的正文）。本机没有 PingFang.ttc，
候选表见 config.PLACEHOLDER_FONT_CANDIDATES。
"""

from __future__ import annotations

import io
from functools import lru_cache
from typing import List

from PIL import Image, ImageDraw, ImageFont

from app import config
from app.logger import get_logger

logger = get_logger()

_BG = "#F5F5DC"
_BORDER = "#DEB887"
_TITLE_COLOR = "#B22222"
_TEXT_COLOR = "#333333"
_HINT_COLOR = "#666666"


@lru_cache(maxsize=8)
def _load_font(size: int):
    for path in config.PLACEHOLDER_FONT_CANDIDATES:
        try:
            # .ttc 是字体集合，必须指定 index，否则 PIL 直接报错
            return ImageFont.truetype(path, size, index=0)
        except (OSError, ValueError):
            continue

    logger.warning("未找到可用的中文字体，占位图上的中文会显示为方框")
    return ImageFont.load_default()


def _wrap(text: str, width: int) -> List[str]:
    """按字符数折行。中文没有空格，只能按字数硬切。"""
    lines: List[str] = []
    for paragraph in text.splitlines():
        current = ""
        for char in paragraph:
            current += char
            if len(current) >= width:
                lines.append(current)
                current = ""
        if current:
            lines.append(current)
    return lines


def make_placeholder(
    width_px: int,
    height_px: int,
    title: str = "图片生成失败",
    detail: str = "",
    prompt: str = "",
) -> bytes:
    """画一张占位图并返回 PNG 字节。"""
    width_px = max(320, min(width_px, 4096))
    height_px = max(320, min(height_px, 4096))

    image = Image.new("RGB", (width_px, height_px), color=_BG)
    draw = ImageDraw.Draw(image)

    margin = max(12, int(min(width_px, height_px) * 0.04))
    draw.rectangle(
        [margin, margin, width_px - margin, height_px - margin],
        outline=_BORDER,
        width=max(2, margin // 3),
    )

    # 字号随图宽走，保证不同尺寸下观感一致
    title_size = max(20, int(width_px * 0.06))
    body_size = max(13, int(width_px * 0.033))

    title_font = _load_font(title_size)
    body_font = _load_font(body_size)

    y = int(height_px * 0.22)
    draw.text((width_px // 2, y), title, fill=_TITLE_COLOR, font=title_font, anchor="mm")
    y += int(title_size * 1.6)

    if detail:
        for line in _wrap(detail, 30)[:3]:
            draw.text((width_px // 2, y), line, fill=_HINT_COLOR, font=body_font, anchor="mm")
            y += int(body_size * 1.5)

    # 保留提示词，方便用户手动拿去重画
    if prompt:
        y += int(body_size * 0.8)
        for line in _wrap(prompt, 34)[:8]:
            if y > height_px - margin - body_size * 2:
                break
            draw.text((width_px // 2, y), line, fill=_TEXT_COLOR, font=body_font, anchor="mm")
            y += int(body_size * 1.4)

    draw.text(
        (width_px // 2, height_px - margin - int(body_size * 1.6)),
        "请检查 API Key / 网络后重试",
        fill=_HINT_COLOR,
        font=body_font,
        anchor="mm",
    )

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def size_to_px(size: str) -> tuple:
    """把 "1920x2560" 这样的尺寸串转成 (宽, 高)。"""
    try:
        width, height = size.lower().split("x")
        return int(width), int(height)
    except (ValueError, AttributeError):
        return 1024, 1024


def make_from_size(size: str, **kwargs) -> bytes:
    width, height = size_to_px(size)
    return make_placeholder(width, height, **kwargs)
