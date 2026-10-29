"""data/ 目录布局与路径构造。

所有对外暴露的 id 都会拼进路径，因此必须过一遍 safe_id —— 防止
`../` 之类的输入穿越到目录外。
"""

import re
from pathlib import Path

from app import config

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 重采样缓存的子目录名。以点开头，和真实的图片 id 区分开。
_CACHE_DIRNAME = ".resized"

# 预览截图的子目录名。同样以点开头。
_SHOTS_DIRNAME = ".shots"

# 智能排版的撤销快照目录。同样以点开头 —— 它不是用户数据，不该被当成
# 图片 id、也不该出现在导出和备份里。
_UNDO_DIRNAME = ".undo"


def safe_id(value: str) -> str:
    if not _SAFE_ID.match(value):
        raise ValueError(f"非法 id: {value!r}")
    return value


def book_dir(book_id: str) -> Path:
    return config.BOOKS_DIR / safe_id(book_id)


def book_file(book_id: str) -> Path:
    return book_dir(book_id) / "book.json"


def images_dir(book_id: str) -> Path:
    return book_dir(book_id) / "images"


def image_file(book_id: str, image_id: str) -> Path:
    return images_dir(book_id) / f"{safe_id(image_id)}.png"


def image_history_file(book_id: str, image_id: str) -> Path:
    """每张图的元信息（prompt / 生成时间 / 服务），用于历史版本回滚。"""
    return images_dir(book_id) / f"{safe_id(image_id)}.json"


def exports_dir(book_id: str) -> Path:
    return book_dir(book_id) / "exports"


def image_cache_file(
    book_id: str, image_id: str, width_px: int, height_px: int, fit: str
) -> Path:
    """重采样后的图片缓存路径。

    键里必须带盒子尺寸和 fit：同一张插图在不同版式下要的像素数不同
    （满版 A5 要 1240×1754，半页只要 1240×965），只按 image_id 存会串。
    fit 也要带 —— cover 和 contain 算出来的目标尺寸不一样。
    """
    name = f"{safe_id(image_id)}__{width_px}x{height_px}_{safe_id(fit)}.jpg"
    return images_dir(book_id) / _CACHE_DIRNAME / name


def page_shot_file(
    book_id: str, number: int, mode: str, scale: int, fingerprint: str
) -> Path:
    """单页预览截图的缓存路径（3D 翻页的纹理）。

    文件名里必须带**内容指纹**：截图是这一页渲染出来的像素，改一个字、换一张
    插图、调一下版式都会让它变。只按页码存的话，用户改完文字再进 3D 预览
    看到的还是旧画面 —— 而且看起来"像"是对的，只是内容是上一版的。
    """
    name = f"p{number:03d}_{safe_id(mode)}_{scale}_{safe_id(fingerprint)}.png"
    return book_dir(book_id) / _SHOTS_DIRNAME / name


def layout_undo_file(book_id: str) -> Path:
    """智能排版的撤销快照。

    落盘而不是放内存：`job_manager` 重启即丢，刷新页面后撤销按钮就死了。
    也不放进 book.json —— 那是用户数据，会跟着进备份和导出，而快照只是一份
    "上一次长什么样"的副本，用户永远不该看见它。
    """
    return book_dir(book_id) / _UNDO_DIRNAME / "autolayout.json"


def ensure_book_dirs(book_id: str) -> None:
    images_dir(book_id).mkdir(parents=True, exist_ok=True)
    exports_dir(book_id).mkdir(parents=True, exist_ok=True)
