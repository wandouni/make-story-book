"""健康检查：除了服务本身，还要探测 PDF 导出依赖是否就绪。

Chromium 缺失时给出明确提示，而不是等到用户点导出才 500。
"""

import json
import os
import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter

from app import config
from app.services import image_generator

router = APIRouter(tags=["meta"])

_INSTALL_HINT = "请运行：cd backend && uv run playwright install chromium"


def _playwright_browsers_dir() -> Path:
    env = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
    if env:
        return Path(env)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    if sys.platform == "win32":
        return Path(os.getenv("USERPROFILE", "")) / "AppData/Local/ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def _expected_chromium_revision() -> Optional[str]:
    """从 playwright 自带的 browsers.json 读出它期望的 chromium 版本。

    只看"缓存目录里有 chromium-*"是不够的 —— 升级 playwright 后旧版本
    仍在缓存里，检测会通过但启动浏览器时才报 Executable doesn't exist。
    """
    try:
        import playwright
    except ImportError:
        return None

    manifest = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    for entry in data.get("browsers", []):
        if entry.get("name") == "chromium":
            return str(entry.get("revision"))
    return None


def chromium_status() -> dict:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return {"ready": False, "reason": "playwright 未安装，请运行 uv sync"}

    browsers_dir = _playwright_browsers_dir()
    if not browsers_dir.exists():
        return {"ready": False, "reason": f"浏览器未下载。{_INSTALL_HINT}"}

    revision = _expected_chromium_revision()
    if revision is None:
        # 读不到 manifest 时退化为宽松检测，宁可放过也不错杀
        ready = any(
            p.is_dir() and p.name.startswith("chromium-") for p in browsers_dir.iterdir()
        )
        return {"ready": ready, "reason": "" if ready else f"chromium 未下载。{_INSTALL_HINT}"}

    # page.pdf() 用的是 headless shell，优先查它
    for name in (f"chromium_headless_shell-{revision}", f"chromium-{revision}"):
        if (browsers_dir / name).exists():
            return {"ready": True, "reason": ""}

    return {
        "ready": False,
        "reason": f"chromium 版本不匹配（需要 r{revision}）。{_INSTALL_HINT}",
    }


@router.get("/health")
async def health():
    chromium = chromium_status()
    return {
        "ok": True,
        "version": config.APP_VERSION,
        "playwright": chromium["ready"],
        "playwrightReason": chromium["reason"],
        "imageService": config.IMAGE_SERVICE,
        # 让前端能在启动时提示"没配 key"，而不是生图时才失败
        "keysConfigured": {
            "text": bool(config.API_KEY),
            "doubao": bool(config.ARK_API_KEY),
        },
        # 比 keysConfigured 更准：key 填了不代表这个服务能用
        # （比如 API_KEY 是火山方舟的，发给通义的 DashScope 只会 401）。
        # 前端据此禁用选不了的服务，而不是让用户配完整本再失败。
        "imageServices": {
            service: {
                "available": not reason,
                "reason": reason,
            }
            for service, reason in (
                (s, image_generator.service_unavailable_reason(s))
                for s in ("doubao", "tongyi")
            )
        },
    }
