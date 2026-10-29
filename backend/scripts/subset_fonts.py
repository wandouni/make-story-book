"""把第三方字体子集化成自托管 woff2。

有些字体在 @fontsource 的 CDN 上没有可用的中文切片（霞鹜文楷就是），只能从
上游下原始 TTF 自己裁。原始文件 25MB，裁完 1.8MB。

**裁剪用的字符集取自已有的 Noto 字体**，不是随手凑的常用字表。这样三个字体族
覆盖的汉字完全相同，不会出现"这个故事里有个字，宋体能显示、楷体变方框"——
那种字到用时才发现，而且只在某一页上。要扩字符集就换 Noto 的源文件重跑，
三边一起扩。

字体源文件不进版本库（`.cache/fonts/` 已忽略），缺哪个补哪个。

用法：
    uv run python scripts/subset_fonts.py
    uv run python scripts/subset_fonts.py --force    # 已存在的也重新裁
"""

import argparse
import sys
from pathlib import Path

import httpx
from fontTools.subset import Options, Subsetter
from fontTools.ttLib import TTFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402

# 字符集的基准。必须是已经躺在 fonts/ 里的那个，脚本只读不写。
REFERENCE_FONT = "noto-serif-sc-400.woff2"

CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "fonts"

WENKAI_VERSION = "v1.522"
WENKAI_BASE = (
    f"https://github.com/lxgw/LxgwWenKai/releases/download/{WENKAI_VERSION}"
)

FONTS = [
    {
        "source": f"{WENKAI_BASE}/LXGWWenKai-Regular.ttf",
        "target": "lxgw-wenkai-400.woff2",
    },
    {
        # 霞鹜文楷只有 Light/Regular/Medium，没有 Bold。用 Medium 顶 700：
        # 楷体的假粗（浏览器自己加粗）笔画会糊成一团，很难看。
        "source": f"{WENKAI_BASE}/LXGWWenKai-Medium.ttf",
        "target": "lxgw-wenkai-700.woff2",
    },
]

MIN_SOURCE_BYTES = 1_000_000  # 小于这个说明拿到的是错误页而不是字体


def reference_codepoints() -> set[int]:
    path = config.FONTS_DIR / REFERENCE_FONT
    if not path.exists():
        raise SystemExit(
            f"找不到基准字体 {path}。先跑 scripts/fetch_fonts.py 把 Noto 拉下来。"
        )
    font = TTFont(path)
    codepoints: set[int] = set()
    for table in font["cmap"].tables:
        codepoints |= set(table.cmap.keys())
    return codepoints


def fetch(source: str, target: Path) -> None:
    if target.exists() and target.stat().st_size >= MIN_SOURCE_BYTES:
        print(f"  已缓存  {target.name}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"  下载    {target.name}  <- {source}")
    with httpx.stream("GET", source, follow_redirects=True, timeout=600.0) as response:
        response.raise_for_status()
        with target.open("wb") as handle:
            for chunk in response.iter_bytes(1 << 20):
                handle.write(chunk)


def subset(source: Path, target: Path, codepoints: set[int]) -> None:
    options = Options()
    options.flavor = "woff2"
    # 提示指令对屏幕排版没用，留着白占体积
    options.hinting = False
    options.desubroutinize = False
    # 保留全部 name 记录：字体名、版权、许可都在里面，OFL 要求随字体分发
    options.name_IDs = ["*"]
    options.notdef_outline = True

    subsetter = Subsetter(options=options)
    subsetter.populate(unicodes=sorted(codepoints))

    font = TTFont(source)
    subsetter.subset(font)
    font.flavor = "woff2"
    font.save(target)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="已存在的也重新裁")
    args = parser.parse_args()

    config.FONTS_DIR.mkdir(parents=True, exist_ok=True)
    codepoints = reference_codepoints()
    print(f"基准字符集：{len(codepoints)} 个码位（取自 {REFERENCE_FONT}）\n")

    for font in FONTS:
        target = config.FONTS_DIR / font["target"]
        if target.exists() and not args.force:
            print(f"  已存在  {font['target']}")
            continue

        cached = CACHE_DIR / font["source"].rsplit("/", 1)[-1]
        try:
            fetch(font["source"], cached)
            subset(cached, target, codepoints)
        except Exception as error:  # noqa: BLE001 —— 网络/字体格式都可能出问题
            print(f"  失败    {font['target']}  ({type(error).__name__}: {error})")
            return 1
        print(f"  已生成  {font['target']}  ({target.stat().st_size / 1e6:.2f} MB)")

    print(f"\n字体目录: {config.FONTS_DIR}")
    print("别忘了把新字体族加进 scripts/fetch_fonts.py 的清单和 book.css 的 @font-face。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
