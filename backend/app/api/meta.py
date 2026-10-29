"""元数据：画风、开本、预设版式、可用字体。前端启动时拉一次并缓存。"""

import json

from fastapi import APIRouter

from app import config

router = APIRouter(prefix="/meta", tags=["meta"])


@router.get("/styles")
async def styles():
    # 显示名单独一张表：键名要落进 book.json，得短而稳定；显示名可以随文案改
    return [
        {"key": key, "label": config.IMAGE_STYLE_LABELS.get(key, key), "prompt": prompt}
        for key, prompt in config.IMAGE_STYLES.items()
    ]


@router.get("/trim-sizes")
async def trim_sizes():
    return [{"id": key, **value} for key, value in config.TRIM_SIZES.items()]


@router.get("/presets")
async def presets():
    return [{"id": key, **value} for key, value in config.PRESETS.items()]


@router.get("/defaults")
async def defaults():
    """新建页、重新分页时用的默认值。

    前端在版式下拉和「转回预设版式」里都要给一个初始值。写死在前端的话，
    改了 config.DEFAULT_PRESET 界面上不会有任何变化 —— 用户实际点「分页」
    时发出去的是**前端那个值**，后端常量只管新加的页。两边迟早对不上，
    所以和 export-spec 一样从这里取。
    """
    return {
        "preset": config.DEFAULT_PRESET,
        "overlayPanelOpacity": config.DEFAULT_OVERLAY_PANEL_OPACITY,
        "fontFamily": config.DEFAULT_FONT_FAMILY,
    }


@router.get("/export-spec")
async def export_spec():
    """导出规格。

    前端在**导出之前**就要说明"印刷版是 300dpi、预览版是 150dpi"，
    而这两个数是 render/geometry.py 里的常量。硬编码到前端迟早会飘，
    所以从这里取 —— 改了 PRINT_DPI，界面上写的字跟着变。

    出血量不在这里：每本书的 bleedMm 可能不同，用 book.bleedMm 那个。
    """
    from app.render import geometry

    return {"printDpi": geometry.PRINT_DPI, "screenDpi": geometry.SCREEN_DPI}


@router.get("/fonts")
async def fonts():
    """可用字体清单。由 render/fonts/fonts.json 描述，缺省时返回空列表。

    前端字体下拉直接消费这个列表，所以自托管字体一旦就位就会自动出现。
    """
    manifest = config.FONTS_DIR / "fonts.json"
    if not manifest.exists():
        return []
    try:
        return json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
