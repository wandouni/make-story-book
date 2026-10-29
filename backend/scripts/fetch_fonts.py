"""下载自托管字体。

字体不进版本库（5 个字重合计约 7MB），用这个脚本按需拉取。
`npm run setup` 会自动调用它。

来源是 @fontsource 的 CDN —— 它把 Google Fonts 的字体按 unicode-range
切成了多个 woff2。我们只取 chinese-simplified 那一片：实测它同时包含
拉丁字母、数字和中文标点，所以每个字重只需要一个文件，不用在 CSS 里
写一堆 unicode-range。

为什么必须自托管：浏览器预览用的字体和 headless Chromium 能拿到的系统
字体可能不同，依赖系统字体会导致"预览好看、导出跑版"。
"""

import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402

CDN = "https://cdn.jsdelivr.net/npm/@fontsource/{pkg}@5.3.0/files/{pkg}-chinese-simplified-{weight}-normal.woff2"

# required=False 的字体拉不到只警告，不中断 —— 缺了会回退到别的字体族
FONTS = [
    {
        "pkg": "noto-serif-sc",
        "weight": 400,
        "family": "Noto Serif SC",
        "label": "思源宋体",
        "target": "noto-serif-sc-400.woff2",
        "required": True,
    },
    {
        "pkg": "noto-serif-sc",
        "weight": 700,
        "family": "Noto Serif SC",
        "label": "思源宋体",
        "target": "noto-serif-sc-700.woff2",
        "required": True,
    },
    {
        "pkg": "noto-sans-sc",
        "weight": 400,
        "family": "Noto Sans SC",
        "label": "思源黑体",
        "target": "noto-sans-sc-400.woff2",
        "required": True,
    },
    {
        "pkg": "noto-sans-sc",
        "weight": 700,
        "family": "Noto Sans SC",
        "label": "思源黑体",
        "target": "noto-sans-sc-700.woff2",
        "required": True,
    },
    # 霞鹜文楷（楷体）不在 @fontsource 的 CDN 上 —— 那个包只有 -latin- 切片，
    # 名字对不上中文。它由 scripts/subset_fonts.py 从上游 TTF 裁出来，
    # 这里只负责登记进清单，不下载。
    {
        "local": True,
        "weight": 400,
        "family": "LXGW WenKai",
        "label": "楷体（霞鹜文楷）",
        "target": "lxgw-wenkai-400.woff2",
    },
    {
        # 霞鹜文楷没有 Bold，用 Medium 顶 700 —— 楷体的假粗笔画会糊成一团
        "local": True,
        "weight": 700,
        "family": "LXGW WenKai",
        "label": "楷体（霞鹜文楷）",
        "target": "lxgw-wenkai-700.woff2",
    },
]

MIN_BYTES = 10_000  # 小于这个大小说明拿到的是错误页而不是字体


def download(client: httpx.Client, url: str, target: Path) -> bool:
    response = client.get(url, follow_redirects=True)
    if response.status_code != 200 or len(response.content) < MIN_BYTES:
        return False
    target.write_bytes(response.content)
    return True


def main() -> int:
    config.FONTS_DIR.mkdir(parents=True, exist_ok=True)

    families: dict = {}
    failed_required = []

    with httpx.Client(timeout=120.0) as client:
        for font in FONTS:
            target = config.FONTS_DIR / font["target"]

            if font.get("local"):
                if target.exists() and target.stat().st_size >= MIN_BYTES:
                    print(f"  已存在  {font['target']}（本地子集化）")
                else:
                    print(
                        f"  缺失    {font['target']} —— 跑 "
                        f"uv run python scripts/subset_fonts.py 生成"
                    )
                    continue
            elif target.exists() and target.stat().st_size >= MIN_BYTES:
                print(f"  已存在  {font['target']}")
            else:
                url = CDN.format(pkg=font["pkg"], weight=font["weight"])
                if download(client, url, target):
                    size_kb = target.stat().st_size // 1024
                    print(f"  已下载  {font['target']}  ({size_kb} KB)")
                else:
                    print(f"  失败    {font['target']}  <- {url}")
                    if font["required"]:
                        failed_required.append(font["target"])
                    continue

            entry = families.setdefault(
                font["family"], {"family": font["family"], "label": font["label"], "weights": []}
            )
            entry["weights"].append(font["weight"])

    manifest = []
    for family, entry in families.items():
        slug = family.lower().replace(" ", "-")
        manifest.append(
            {
                "family": family,
                "label": entry["label"],
                "url": f"/api/assets/fonts/{slug}-400.woff2",
                "weights": sorted(entry["weights"]),
            }
        )
    (config.FONTS_DIR / "fonts.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n字体目录: {config.FONTS_DIR}")
    print(f"清单已写入 fonts.json，共 {len(manifest)} 个字体族")

    if failed_required:
        print(f"\n错误：以下必需字体下载失败 {failed_required}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
