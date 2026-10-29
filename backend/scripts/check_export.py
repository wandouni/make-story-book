"""手工验一次导出：页数、物理尺寸、中文能否提取。

单测里也有同样的断言（tests/test_export.py），这个脚本是给"想看一眼
真实产物"用的 —— 它会打印每页的 MediaBox 尺寸和提取出的前几个字。

用法：
    uv run python scripts/check_export.py [书 id]
不给 id 就临时建一本。
"""

import sys

import httpx
from pypdf import PdfReader

API = "http://127.0.0.1:8000/api"
STORY = (
    "从前有座山，山里有座庙，庙里有个老和尚。\n\n"
    "老和尚对小和尚说：今天下山去挑水。\n\n"
    "小和尚挑着水桶，一路走一路唱着歌。"
)
MM_PER_PT = 25.4 / 72


def make_book() -> str:
    with httpx.Client(base_url=API, timeout=60) as c:
        book_id = c.post(
            "/books", json={"title": "导出验证", "trimSizeId": "A5", "pageCount": 3}
        ).json()["id"]
        c.put(f"/books/{book_id}/story", json={"text": STORY})
        c.post(f"/books/{book_id}/paginate", json={"pageCount": 3, "preset": "image-top"})
    return book_id


def check(book_id: str) -> None:
    with httpx.Client(base_url=API, timeout=300) as c:
        book = c.get(f"/books/{book_id}").json()
        print(f"书: {book['title']}  {book['trimSize']['label']}  {len(book['pages'])} 页")
        print(f"出血: {book['bleedMm']}mm\n")

        for kind in ("preview", "print"):
            resp = c.post(f"/books/{book_id}/export", json={"kind": kind})
            if resp.status_code != 200:
                print(f"✗ {kind} 导出失败: {resp.status_code} {resp.text[:200]}")
                continue
            data = resp.json()
            report = data["report"]

            pdf = c.get(f"/books/{book_id}/exports/{data['filename']}")
            path = f"/tmp/msb-{kind}.pdf"
            with open(path, "wb") as f:
                f.write(pdf.content)

            reader = PdfReader(path)
            box = reader.pages[0].mediabox
            w = float(box.width) * MM_PER_PT
            h = float(box.height) * MM_PER_PT
            text = (reader.pages[1].extract_text() or "").strip()

            print(f"── {kind} ──")
            print(f"  文件      : {path}  ({len(pdf.content)/1024:.0f} KB)")
            print(f"  页数      : {len(reader.pages)}（期望 {len(book['pages'])}）")
            print(f"  第 1 页尺寸: {w:.1f} × {h:.1f} mm")
            print(f"  报告页尺寸 : {report['pageSizeMm']['width']} × {report['pageSizeMm']['height']} mm")
            print(f"  第 2 页文字: {text[:30]!r}")
            if report["warnings"]:
                for line in report["warnings"]:
                    print(f"  ! {line}")
            print()


if __name__ == "__main__":
    check(sys.argv[1] if len(sys.argv) > 1 else make_book())
