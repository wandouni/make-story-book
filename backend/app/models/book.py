"""绘本数据模型。

两条贯穿全局的约定：

1. **几何一律用 mm**。CSS 的 mm 在 Chromium 里固定映射 96/25.4 px，
   预览与 PDF 共用同一套数字，导出时零换算误差。
2. **Page 用 mode 做判别字段**。预设页存紧凑语义（preset + options），
   自由页存 blocks，两者字段互斥 —— 类型系统层面保证不会同时存在。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, List, Literal, Optional, Union

from pydantic import BaseModel, Field

from app import config


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 文字样式


class TextStyle(BaseModel):
    # 默认值走 config 常量，不在这里再写一份字符串 —— 写两份的结果是
    # 迟早只改一处，表现为"新建页是楷体、重新分页出来的页是宋体"。
    fontFamily: str = config.DEFAULT_FONT_FAMILY
    fontSizePt: float = 18
    fontWeight: Literal[400, 700] = 400
    color: str = "#2C3E50"
    align: Literal["left", "center", "right", "justify"] = "left"
    verticalAlign: Literal["top", "middle", "bottom"] = "top"
    lineHeight: float = 1.6
    letterSpacing: float = 0
    writingMode: Literal["horizontal-tb", "vertical-rl"] = "horizontal-tb"
    backgroundColor: Optional[str] = None
    paddingMm: float = 0
    # 背景色/底衬的圆角。满版叠字的白色底衬要它，文字块单独用也好看。
    borderRadiusMm: float = 0
    # 溢出时自动降字号。用户手动调过字号后前端会关掉它，
    # 避免"我调好的字号被系统改回去"。
    autoFit: bool = True


# ---------------------------------------------------------------- 内容块


class BlockBase(BaseModel):
    id: str = Field(default_factory=lambda: new_id("b"))
    xMm: float = 0
    yMm: float = 0
    widthMm: float = 0
    heightMm: float = 0
    rotationDeg: float = 0
    zIndex: int = 0
    locked: bool = False
    opacity: float = 1.0


class TextBlock(BlockBase):
    type: Literal["text"] = "text"
    content: str = ""
    style: TextStyle = Field(default_factory=TextStyle)


class ImageBlock(BlockBase):
    type: Literal["image"] = "image"
    imageId: Optional[str] = None
    fit: Literal["cover", "contain"] = "cover"
    focusX: float = 0.5
    focusY: float = 0.5
    borderRadiusMm: float = 0
    # 满版：图片向四周溢出出血区。坐标仍按成品区记，溢出量在渲染时按
    # 当前模式的 bleed 加 —— 因为预览(0)与印刷(3mm)的溢出量不同，
    # 把出血烘进坐标会让两者之一出错（cover 的缩放盒子会跟着变）。
    bleed: bool = False


Block = Annotated[Union[TextBlock, ImageBlock], Field(discriminator="type")]


# ---------------------------------------------------------------- 页面


class Background(BaseModel):
    type: Literal["none", "color"] = "none"
    color: str = "#FFFFFF"


class PageBase(BaseModel):
    id: str = Field(default_factory=lambda: new_id("p"))
    text: str = ""
    imageId: Optional[str] = None
    imagePrompt: str = ""
    imageStatus: Literal["none", "pending", "running", "done", "failed"] = "none"
    background: Background = Field(default_factory=Background)


class OverlayRect(BaseModel):
    """叠字区的显式矩形，mm，相对成品区。智能排版写的就是它。

    用子模型而不是裸四元组：能校验、book.json 里是带名字的对象（给人看和手改）、
    前端类型也有名字。四边全必填 —— 半个矩形没有意义。

    **故意不加 ge/le 约束**：越界的值由 `presets._clamp_rect_mm` 夹进成品区，
    而 pydantic 约束会让整本书**加载失败** —— 手改 book.json 时打错一个负号，
    代价不该是"这本书打不开了"。
    """

    xMm: float
    yMm: float
    widthMm: float
    heightMm: float


class PresetPageOptions(BaseModel):
    fontFamily: str = config.DEFAULT_FONT_FAMILY
    fontSizePt: float = 18
    color: str = "#2C3E50"
    align: Literal["left", "center", "right"] = "left"
    lineHeight: float = 1.6
    paddingMm: float = 10
    # 仅 full-bleed-overlay 使用。
    # auto = 分析插图，把文字放到细节最少的那条横带里（见 services/image_analysis.py）。
    # 没有插图时回退到 bottom。
    overlayPosition: Literal["auto", "top", "bottom", "center"] = "auto"
    # 显式矩形，智能排版挑中的落点。**非 None 时压过 overlayPosition** ——
    # 用户点「恢复自动」把它清成 None，就回到 overlayPosition 的规则。
    #
    # 默认必须是 None，不能是"算好的自动值"：一旦默认是具体矩形，上面那个
    # 单选框就彻底失灵了，而且已有的书会被改样。
    overlayRect: Optional[OverlayRect] = None
    # 叠字底衬：半透明白色圆角板的不透明度。默认 0 = 不要底衬，纯靠字色压图
    # （理由见 config.DEFAULT_OVERLAY_PANEL_OPACITY）。
    overlayPanelOpacity: float = config.DEFAULT_OVERLAY_PANEL_OPACITY
    imageFocusX: float = 0.5
    imageFocusY: float = 0.5


class PresetPage(PageBase):
    mode: Literal["preset"] = "preset"
    preset: str = config.DEFAULT_PRESET
    options: PresetPageOptions = Field(default_factory=PresetPageOptions)


class FreePage(PageBase):
    mode: Literal["free"] = "free"
    blocks: List[Block] = Field(default_factory=list)


Page = Annotated[Union[PresetPage, FreePage], Field(discriminator="mode")]


# ---------------------------------------------------------------- 书


class TrimSize(BaseModel):
    id: str
    label: str
    widthMm: float
    heightMm: float

    @classmethod
    def from_id(cls, trim_id: str) -> "TrimSize":
        spec = config.TRIM_SIZES.get(trim_id) or config.TRIM_SIZES[config.DEFAULT_TRIM_SIZE]
        return cls(id=trim_id, **spec)


class Book(BaseModel):
    id: str = Field(default_factory=lambda: new_id("book"))
    schemaVersion: int = 1
    title: str = "未命名绘本"
    author: str = ""
    # 中文，给用户看
    protagonist: str = ""
    # 中文固定外貌描述，注入每条生图 prompt 以保持角色一致性
    characterDescription: str = ""
    style: str = config.DEFAULT_IMAGE_STYLE
    trimSize: TrimSize = Field(
        default_factory=lambda: TrimSize.from_id(config.DEFAULT_TRIM_SIZE)
    )
    bleedMm: float = config.DEFAULT_BLEED_MM
    pageCount: int = config.DEFAULT_PAGES
    # 原始粘贴文本，永不改写
    storyText: str = ""
    # 封面背景图（AI 生成）。封面是装订的一部分，不占内页页码。
    coverImageId: Optional[str] = None
    pages: List[Page] = Field(default_factory=list)
    imageService: Literal["doubao", "tongyi"] = Field(
        default_factory=lambda: config.IMAGE_SERVICE  # type: ignore[arg-type]
    )
    createdAt: str = Field(default_factory=now_iso)
    updatedAt: str = Field(default_factory=now_iso)

    def page_by_number(self, number: int) -> Optional[Page]:
        """页码从 1 开始，与前端展示一致。"""
        if 1 <= number <= len(self.pages):
            return self.pages[number - 1]
        return None


class BookSummary(BaseModel):
    """列表页用的精简结构，避免把整本书的 blocks 都传过去。"""

    id: str
    title: str
    author: str
    style: str
    pageCount: int
    coverImageId: Optional[str] = None
    # 列表页的封面缩略图要按开本比例裁，不然 A5 竖版的封面会被 4:3 的框
    # 裁掉上下大半。前端拿它去 meta.trimSizes 里查宽高。
    trimSizeId: str = config.DEFAULT_TRIM_SIZE
    updatedAt: str

    @classmethod
    def of(cls, book: Book) -> "BookSummary":
        cover = book.coverImageId or next((p.imageId for p in book.pages if p.imageId), None)
        return cls(
            id=book.id,
            title=book.title,
            author=book.author,
            style=book.style,
            pageCount=len(book.pages),
            coverImageId=cover,
            trimSizeId=book.trimSize.id,
            updatedAt=book.updatedAt,
        )


# ---------------------------------------------------------------- 请求体


class CreateBookRequest(BaseModel):
    title: str = "未命名绘本"
    author: str = ""
    protagonist: str = ""
    style: str = config.DEFAULT_IMAGE_STYLE
    trimSizeId: str = config.DEFAULT_TRIM_SIZE
    pageCount: int = config.DEFAULT_PAGES
    imageService: Literal["doubao", "tongyi"] = Field(
        default_factory=lambda: config.IMAGE_SERVICE  # type: ignore[arg-type]
    )


class UpdateBookRequest(BaseModel):
    title: Optional[str] = None
    author: Optional[str] = None
    protagonist: Optional[str] = None
    characterDescription: Optional[str] = None
    style: Optional[str] = None
    trimSizeId: Optional[str] = None
    bleedMm: Optional[float] = None
    pageCount: Optional[int] = None
    imageService: Optional[Literal["doubao", "tongyi"]] = None
