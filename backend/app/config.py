"""集中配置：环境变量、画风表、开本表、预设版式表。

环境变量名与 StoryCraft 保持一致，方便直接复用已有的 .env。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/
load_dotenv(BASE_DIR / ".env")

APP_VERSION = "0.1.0"

# ---------------------------------------------------------------- 服务

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))

# Playwright 导出时回连本服务，必须与 HOST/PORT 一致
SELF_BASE_URL = os.getenv("SELF_BASE_URL", f"http://{HOST}:{PORT}")

# ---------------------------------------------------------------- 存储

DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
BOOKS_DIR = DATA_DIR / "books"

# ---------------------------------------------------------------- 文本生成

API_KEY = os.getenv("API_KEY", "")
API_ENDPOINT = os.getenv(
    "API_ENDPOINT", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
TEXT_MODEL = os.getenv("TEXT_MODEL", "qwen-plus")

# 批量生成图片提示词时推理模型响应可能远超 60s，读超时需放宽。
# 每次重试读超时翻倍：TEXT_TIMEOUT -> 2x -> 3x
TEXT_TIMEOUT = int(os.getenv("TEXT_TIMEOUT", "300"))
TEXT_MAX_RETRIES = int(os.getenv("TEXT_MAX_RETRIES", "3"))

# ---------------------------------------------------------------- 图片生成

# tongyi 或 doubao
IMAGE_SERVICE = os.getenv("IMAGE_SERVICE", "doubao")

# 豆包 / 火山引擎 Ark。注意：这个 key 只用于豆包分支，
# 绝不能发往通义接口（反之亦然），详见 services/image_generator.py
ARK_API_KEY = os.getenv("ARK_API_KEY", "")
ARK_BASE_URL = os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
DOUBAO_IMAGE_MODEL = os.getenv("DOUBAO_IMAGE_MODEL", "doubao-seedream-4-5-251128")

# 通义万相（走 API_KEY）
TONGYI_IMAGE_MODEL = os.getenv("TONGYI_IMAGE_MODEL", "wan2.6-t2i")

# 生图并发与节流。两家都有 QPS 限制，串行 + 间隔最稳。
IMAGE_CONCURRENCY = int(os.getenv("IMAGE_CONCURRENCY", "2"))
IMAGE_REQUEST_INTERVAL = float(os.getenv("IMAGE_REQUEST_INTERVAL", "3"))

# ---------------------------------------------------------------- 应用

MIN_PAGES = 1
MAX_PAGES = 30
DEFAULT_PAGES = 8

# 图片风格 → prompt 片段。
#
# 一律用中文：绘本是给中国儿童看的，提示词走中文能让模型把"不要出现文字"
# 这类否定要求理解得更准，也免得模型顺手在画面上写一串英文标题。
# 前 5 条取自项目根目录的 style-spec.md，是这个项目自己挑的风格。
#
# 键名同时是落盘到 book.json 的 book.style，**改名会让旧书读不回风格**
# （会退回 DEFAULT_IMAGE_STYLE），要改就一起做迁移。
IMAGE_STYLES = {
    "高定羊毛毡艺术插画": (
        "高定羊毛毡艺术插画，留白式情绪铺垫，有张力，人物层次，角度，水彩晕染，"
        "高饱色彩，注重线条笔触，极繁主义，高质量，电影质感，梦幻，矿物原料晕染，"
        "动态定格，强烈视觉效果，童话，Q萌，色彩明亮梦幻，"
        "人物增加高光、暗面等光影效果，惬意灵动"
    ),
    "治愈毛绒绘本风格": (
        "儿童治愈绘本插画，水彩蜡笔手绘质感，细微纸张颗粒噪点，柔和漫射暖光，"
        "低饱和奶油柔和配色，圆润无棱角线条，人物动物带有粉嫩腮红，蓬松柔软毛绒质感，"
        "厚涂笔触，画面朦胧柔和，没有尖锐轮廓，温暖松弛童话氛围感"
    ),
    "肌理颗粒绘本": (
        "儿童绘本插图，手绘板绘，纸张噪点颗粒肌理，磨砂质感，厚涂笔触，平涂带纹理，"
        "造型圆润柔和，没有尖锐棱角，低饱和莫兰迪配色，柔和漫射光，弱阴影，"
        "朦胧治愈氛围感，色彩柔和渐变，图画书插画，细腻手绘质感，非写实"
    ),
    "埃里克巴纳风格": (
        "柔和水彩儿童绘本，通透水彩晕染，柔和模糊边缘，淡淡的水痕纹理，"
        "低饱和度柔和配色，拟人小动物，松弛线条，浅淡轮廓，柔和自然光，温馨治愈，"
        "纸张水彩纹理，儿童图画书，没有粗黑勾线"
    ),
    "国风水墨绘本风格": (
        "国风儿童绘本插画，水墨淡彩，宣纸肌理，水墨自然晕染，墨色浓淡变化，"
        "柔和写意笔触，造型圆润简约，线条松弛不锋利，浅淡轮廓，低饱和国风配色，"
        "青灰、赭石、淡茶、浅蓝为主，留白构图，温润柔和自然光，朦胧诗意氛围感，"
        "传统山水草木，写意云气，淡彩薄涂，干净雅致，童书插图质感，"
        "不厚重写实，没有强烈硬阴影，宣纸颗粒纹理"
    ),
    # ---- 下面几条是通用美术风格，沿用旧键名，旧书仍能读回 ----
    "漫画风": "漫画风格，黑白线稿，墨线勾勒，单色",
    "动漫": "动漫风格，色彩鲜艳，赛璐璐上色，日本动画质感",
    "中国风": "中国风绘画，传统国画，水墨晕染，雅致，东方美学",
    "水墨画": "水墨画，水彩质感，柔和笔触，极简，写意",
    "古典": "古典绘画，文艺复兴风格，油画质感，博物馆藏品级",
    "油画": "油画，厚涂笔触，画布肌理，色彩浓郁，古典艺术",
    "水彩画": "水彩画，柔和色彩，轻柔笔触，通透轻盈",
    "卡通": "卡通风格，可爱，色彩明亮，造型简洁，儿童插画",
}

# 下拉里显示的名字。不在这里的键名就用键名本身。
# 单独一张表是为了让键名保持短、稳定（它要落进 book.json），而显示名可以随文案改。
IMAGE_STYLE_LABELS = {
    "埃里克巴纳风格": "埃里克・巴纳风格（柔和水彩绘本）",
}

DEFAULT_IMAGE_STYLE = "卡通"

# ---------------------------------------------------------------- 开本

# 几何一律用 mm：CSS 的 mm 在 Chromium 里固定映射 96/25.4 px，
# 预览与 PDF 共用同一套数字，导出时零换算误差。
TRIM_SIZES = {
    "A4": {"label": "A4 竖版 210×297mm", "widthMm": 210.0, "heightMm": 297.0},
    "A4L": {"label": "A4 横版 297×210mm", "widthMm": 297.0, "heightMm": 210.0},
    "A5": {"label": "A5 竖版 148×210mm", "widthMm": 148.0, "heightMm": 210.0},
    "SQ210": {"label": "方形 210×210mm", "widthMm": 210.0, "heightMm": 210.0},
    "SQ200": {"label": "方形 200×200mm", "widthMm": 200.0, "heightMm": 200.0},
    "SQ160": {"label": "方形 160×160mm", "widthMm": 160.0, "heightMm": 160.0},
}
DEFAULT_TRIM_SIZE = "SQ200"

# 印刷出血。预览版为 0，印刷版按此值向四周扩展。
DEFAULT_BLEED_MM = 3.0

# ---------------------------------------------------------------- 预设版式

# slots 描述每个预设用到的元素，前端据此画选择器图示。
PRESETS = {
    "image-top": {
        "label": "图文上下（图在上）",
        "slots": ["image", "text"],
        "image_ratio": 0.55,
    },
    "image-bottom": {
        "label": "图文上下（图在下）",
        "slots": ["image", "text"],
        "image_ratio": 0.55,
    },
    "split-lr": {
        "label": "左右分栏",
        "slots": ["image", "text"],
        "image_ratio": 0.5,
    },
    "full-bleed-overlay": {
        "label": "满版图叠字",
        "slots": ["image", "text"],
        "image_ratio": 1.0,
    },
    "image-only": {"label": "整页插图", "slots": ["image"], "image_ratio": 1.0},
    "text-only": {"label": "整页文字", "slots": ["text"], "image_ratio": 0.0},
}
# 默认版式：满版图叠字。整页图 + 叠字，配上 overlayPosition 的自动定位
# 和下面那个"底衬默认关掉"，才是绘本该有的样子（图是图、字是字地融在一起）。
# 改这里就够了 —— 前端从 /api/meta/defaults 取，别在界面上再写一份。
DEFAULT_PRESET = "full-bleed-overlay"

# 叠字底衬（半透明白色圆角板）的默认不透明度。
#
# 默认 0 = 不要底衬，纯靠字色压图。这是刻意的：绘本里那层白板一铺上，画面
# 就被切掉一块，"图"和"字"变成两层拼在一起的东西，不像一本书。字能不能看清
# 交给 overlayPosition 的自动定位（把文字挪到插图最空的那条横带里，见
# services/image_analysis.py），而不是靠盖一块板子。
#
# 想要底衬的用户在编辑器里调上去就行（0~1）。
DEFAULT_OVERLAY_PANEL_OPACITY = 0.0

# 各预设对生图构图的额外要求，追加到 image_prompt 末尾
PRESET_PROMPT_HINTS = {
    "full-bleed-overlay": "画面下方三分之一留白，不要有复杂细节，供叠加文字",
    "image-top": "构图居中",
    "image-bottom": "构图居中",
    "split-lr": "构图居中，竖向构图",
    "image-only": "整页插画，细节丰富",
}

# ---------------------------------------------------------------- 镜头语言

# 逐页轮换的镜头语言，用来打破"每一页都是中景、平视、正面、居中"——
# 翻完整本像同一张画重复了二十遍。按页序从每组里各取一项，四组拼成一句
# 追加到提示词末尾（见 services/prompt_builder.py 的 design_for_page）。
#
# 措辞**必须全中文**：它们会被拼进每一条生图提示词，而绘本是给中国儿童看的
# （tests/test_illustrate.py 里有对应的英文扫描，新加组别记得同步）。
#
# 组的长度刻意取 4 和 5 这两个互质的数：配合下面不同的步长，四个维度合起来
# 的循环周期是 lcm(4,5)=20 页 —— 一本书从头翻到尾不会撞出同一个组合。
DESIGN_ELEMENTS = {
    "shot": [
        "远景，人物在环境里显得很小",
        "全景，人物全身入画",
        "中景，人物到腰部",
        "近景，人物胸部以上",
        "特写，只画脸和眼睛",
    ],
    "angle": [
        "平视视角",
        "微微俯视",
        "高高俯视的鸟瞰角度",
        "微微仰视",
    ],
    "light": [
        "清晨的柔和侧光",
        "温暖的逆光，轮廓发亮",
        "夜晚的暖黄灯光",
        "阴天漫射光，没有硬阴影",
    ],
    "structure": [
        "前景有枝叶遮挡，画面有层次",
        "对称构图，平稳安定",
        "大片留白，主体偏在一侧",
        "画面元素丰富，细节铺满",
        "由近及远的纵深引导视线",
    ],
}

# 每个维度的起始偏移和步长。**步长必须与该组的长度互质**，否则取不满一轮
# 就会开始打转（长度 4、步长 2 的话只会在两个值之间来回跳）。
#
# 步长和偏移取不同的值是为了错开：机位和光线都是 4 项，步长一样的话它们会
# 永远绑在一起变，看上去像是同一个维度。
DESIGN_STRIDE = {
    "shot": (0, 1),
    "angle": (1, 1),
    "light": (2, 3),
    "structure": (3, 2),
}

# 拼句时的固定顺序。不依赖 dict 的插入顺序 —— 读起来要像人话，
# "特写…，平视视角，清晨的柔和侧光，对称构图" 这个语序是顺的。
DESIGN_ORDER = ["shot", "angle", "light", "structure"]

# ---------------------------------------------------------------- 智能排版

# 空白候选框检测（services/layout_analysis.py）的参数。**这些只在智能排版任务
# 里用得上**，不在渲染路径上 —— 别把它们接到 renderer / presets 里去。

# 分析用的缩略图宽度。比 image_analysis 的 64 大一倍：那边只按行求标量，这边
# 要按像素算梯度、彩度、亮度和积分图，看得细一点才分得清"一小块空地"和"一整条
# 空带"。128×182（A5 竖版）也就两万多个像素，一次二十来毫秒。
LAYOUT_ANALYSIS_WIDTH = 128

# 候选框离成品区边缘的最小距离。和叠字白板的 OVERLAY_PANEL_INSET_MM 同值 ——
# 贴着裁切线放字，印刷裁切一晃就切到笔画。
LAYOUT_MARGIN_MM = 8.0

# 最多给模型几个候选。给太多它就开始瞎选，而且 prompt 会长。
LAYOUT_MAX_CANDIDATES = 8

# 候选框去重的 IoU 阈值。重叠超过这个比例的框只留分高的那个。
LAYOUT_NMS_IOU = 0.35

# 归一化的分位数。用 95 分位而不是最大值：一张图里只要有一个高光点，
# 按最大值归一化会把整幅图压成一片黑，"哪里更空"就看不出来了。
LAYOUT_NORM_PERCENTILE = 95

# 打分权重，越小越空。改这些值之前先看 services/layout_analysis.py 里
# _score() 的注释 —— 每一项都在对付一种具体的误判。
LAYOUT_COST = {
    # 平均细节量。主判据：有笔画、有纹理的地方梯度大。
    "detail": 0.60,
    # 平均彩度。梯度只看"有没有边"，一大片纯色但很鲜艳的天空梯度也是 0，
    # 可那是主体的一部分（天空、草地）。彩度补上"这里画过东西"这个信号。
    "saturation": 0.25,
    # 细节量的**标准差**。惩罚"框横跨空区边界"：一半空一半是主体的框，均值
    # 可能不高，但标准差很大 —— 文字会有一半压在东西上。
    "spread": 0.50,
    # 越亮越好。用户的原话是"浅色背景或者白色背景的空白位置"。
    "bright": 0.10,
    # 靠下先验，与 image_analysis.BOTTOM_PRIOR 同源（绘本惯例文字在下）。
    # 只是很轻的倾斜，压不过真实的细节差异。
    "bottom": 0.08,
}

# 空白区的亮度硬门槛。过不了就逐级放宽，保证总有结果 —— 夜景那种整幅都暗的
# 插图不该一页都配不出来。放宽过这件事从候选框自己的 lum 字段看得出来。
LAYOUT_MIN_BRIGHTNESS = (0.72, 0.55, 0.40, 0.0)

# 字号反算（services/auto_layout.py）的边界。**字号是算出来的，不让模型猜**：
# 它看不见字体度量，猜出来的字号在浏览器里必然溢出或者留一大块白。
LAYOUT_MIN_FONT_PT = 12.0
LAYOUT_MAX_FONT_PT = 40.0

# 目标填充率：算出来的字只占可用高的 88%，剩下 12% 留作余量。
#
# **方向不能搞反**：Python 这边是**预测**，浏览器（fit-text.js）是**真值**，
# 两者必然有偏差（字体度量、标点挤压、letter-spacing）。而浏览器只会缩不会长，
# 所以 Python 给一个偏大的上界是安全的（会被缩回来），给小了才是白边。
LAYOUT_TARGET_FILL = 0.88

# 字色和背景的最小对比度（WCAG 比值）。4.5 是 AA 级的正文标准。
# 模型看不见图，颜色是凭候选框的亮度数字猜的，猜出"米色字压米色天空"很正常 ——
# 这一条是守卫，不是夺权：达标就原样保留模型给的颜色。
LAYOUT_MIN_CONTRAST = 4.5

# 发给模型的每页正文字数上限。模型只需要判断语气和篇幅感，长度我们自己算得更准
# （见 auto_layout.fit_text_in_box）。不截断的话，30 页的请求会到几十 KB，
# 既慢又容易被端点截断。
LAYOUT_TEXT_EXCERPT_CHARS = 40

# ---------------------------------------------------------------- 导出

PDF_PREVIEW_IMAGE_MAX_DIM = 1600  # 预览版图片长边上限
PDF_PREVIEW_JPEG_QUALITY = 85
PDF_PRINT_DPI = 300  # 印刷版目标 dpi
PDF_PRINT_JPEG_QUALITY = 92

# ---------------------------------------------------------------- 字体

# 自托管字体目录。绝不用系统字体出 PDF —— 浏览器预览与 headless
# Chromium 的系统字体可能不同，会导致"预览好看、导出跑版"。
FONTS_DIR = Path(__file__).resolve().parent / "render" / "fonts"

# 新页/重新分页时的默认字体族。名字必须与 render/fonts/fonts.json 里的
# `family` 逐字相同，否则字体下拉选不中它，界面会显示成"未知字体"。
#
# **改这里只影响新写的页。** 字体族是 per-page 存进 book.json 的，已有的书
# 里存着老值，不会被迁移 —— 这是有意的：用户已经排好的书不该因为我们换默认
# 字体而整本变样。所以同一本书里不会混（重新分页会整批重置）。
#
# 改这个值时要一起改 render/static/book.css 里 body 的 font-family 兜底栈，
# 否则会出现"正文是楷体、某处一小块还是宋体"的鬼影。
DEFAULT_FONT_FAMILY = "LXGW WenKai"

# 占位图回退用的系统中文字体（本机没有 PingFang.ttc，实测候选见下）
PLACEHOLDER_FONT_CANDIDATES = [
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]
