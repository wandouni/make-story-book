"""PDF 导出（集成测试）。

这是 Phase 7 唯一的验收手段 —— 只有真的把 PDF 读回来，才能证明"页数对、
物理尺寸对、中文能提取"这三件事同时成立。

**为什么要真起一个 uvicorn**：导出时 Playwright 要用 HTTP 打开渲染端点，
而 TestClient 不监听端口，浏览器够不着。顺带也就验证了 SELF_BASE_URL
配错时能被发现 —— 这正是 pdf_exporter 里那行注释说的坑。

代价是一次导出几秒。图片重采样那条链路不需要浏览器，单独放在
test_assets.py 里，跑得快。
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from PIL import Image
from pypdf import PdfReader

from app.render import geometry, presets

BACKEND_DIR = Path(__file__).resolve().parent.parent
MM_PER_PT = 25.4 / 72

STORY = (
    "从前有座山，山里有座庙，庙里有个老和尚。\n\n"
    "老和尚对小和尚说：今天下山去挑水。\n\n"
    "小和尚挑着水桶，一路走一路唱着歌。"
)

# A5 竖版 + 3mm 出血
TRIM_W_MM, TRIM_H_MM = 148.0, 210.0
BLEED_MM = 3.0


# ---------------------------------------------------------------- 测试服务器


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, base: str, data: Path) -> None:
        self.base = base
        self.data = data

    @property
    def api(self) -> str:
        return f"{self.base}/api"

    def images_dir(self, book_id: str) -> Path:
        return self.data / "books" / book_id / "images"


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("export-data")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"

    # config 在 import 时读环境变量，所以只能靠子进程的环境传进去。
    # load_dotenv 默认不覆盖已有变量，.env 里的 DATA_DIR 不会顶掉这里的。
    env = {
        **os.environ,
        "DATA_DIR": str(data_dir),
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "SELF_BASE_URL": base,
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=BACKEND_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    deadline = time.time() + 40
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"服务启动即退出：\n{proc.stdout.read().decode()[-2000:]}")
        try:
            if httpx.get(f"{base}/api/health", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.3)
    else:
        proc.kill()
        raise RuntimeError("服务 40 秒内没起来")

    yield Server(base, data_dir)

    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


# ---------------------------------------------------------------- 造数据


def _make_book(server: Server, pages: int = 3, preset: str = "image-top") -> str:
    with httpx.Client(base_url=server.api, timeout=60) as c:
        resp = c.post(
            "/books", json={"title": "导出验证", "trimSizeId": "A5", "pageCount": pages}
        )
        assert resp.status_code in (200, 201), resp.text
        book_id = resp.json()["id"]
        assert c.put(f"/books/{book_id}/story", json={"text": STORY}).status_code == 200
        resp = c.post(f"/books/{book_id}/paginate", json={"pageCount": pages, "preset": preset})
        assert resp.status_code == 200, resp.text
    return book_id


def _attach_image(server: Server, book_id: str, page_index: int = 0, size=(800, 1200)) -> str:
    """给某一页挂一张图。

    book_store 每次请求都从磁盘重读 book.json，所以直接改文件就生效 ——
    不必为了造一张图去调真·生图接口（那要 API key，测试不该依赖网络）。

    路径不能用 paths.image_file 拼：那个走的是**本测试进程**的 config，
    指向真实的 data/，而服务跑在 tmp 目录里。直接用 server 的目录。
    """
    image_id = "test001_v1"
    images = server.images_dir(book_id)
    images.mkdir(parents=True, exist_ok=True)
    Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)).save(
        images / f"{image_id}.png"
    )

    book_file = server.data / "books" / book_id / "book.json"
    data = json.loads(book_file.read_text(encoding="utf-8"))
    data["pages"][page_index]["imageId"] = image_id
    book_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return image_id


def _export(server: Server, book_id: str, kind: str) -> tuple:
    """导一次并下载，返回 (响应 json, 本地 PDF 路径)。"""
    with httpx.Client(base_url=server.api, timeout=300) as c:
        resp = c.post(f"/books/{book_id}/export", json={"kind": kind})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        pdf = c.get(f"/books/{book_id}/exports/{data['filename']}")
        assert pdf.status_code == 200, pdf.text

    out = Path(f"/tmp/msb-test-{kind}-{book_id}.pdf")
    out.write_bytes(pdf.content)
    return data, out


def _page_size_mm(reader: PdfReader, index: int = 0) -> tuple:
    box = reader.pages[index].mediabox
    return float(box.width) * MM_PER_PT, float(box.height) * MM_PER_PT


def _cjk(text: str) -> set:
    return {c for c in text if "一" <= c <= "鿿"}


# ---------------------------------------------------------------- 印刷版


def test_print_pdf_has_bleed_size_and_extractable_chinese(server):
    """印刷版：尺寸要含出血，文字要能提取出中文。

    尺寸这条最容易被悄悄改坏 —— 少一个 prefer_css_page_size，@page 就不
    生效，整本书会退回 Letter，而预览里看着一切正常。
    """
    book_id = _make_book(server)
    data, path = _export(server, book_id, "print")
    reader = PdfReader(path)

    assert len(reader.pages) == 3
    width, height = _page_size_mm(reader)
    assert width == pytest.approx(TRIM_W_MM + 2 * BLEED_MM, abs=0.5)
    assert height == pytest.approx(TRIM_H_MM + 2 * BLEED_MM, abs=0.5)

    assert data["report"]["pageSizeMm"] == {
        "width": pytest.approx(TRIM_W_MM + 2 * BLEED_MM, abs=0.1),
        "height": pytest.approx(TRIM_H_MM + 2 * BLEED_MM, abs=0.1),
    }
    assert data["report"]["bleedMm"] == BLEED_MM
    assert data["report"]["fileSizeBytes"] > 0


def test_print_pdf_text_is_the_original_story(server):
    """中文能提取，且**逐字就是原文**。

    只断言"有中文"是不够的：字体没嵌进去时 Chromium 会回退到系统字体，
    字可能还在、也可能变成别的码位。拿原文比对才同时挡得住乱码和串字。

    逐段比而不是整篇比：无图页会渲染"暂无插图"占位文字，它夹在段落之间，
    整篇的连续子串就断了。段落本身仍然必须原样出现。
    """
    book_id = _make_book(server)
    _, path = _export(server, book_id, "print")
    reader = PdfReader(path)

    text = "".join(page.extract_text() or "" for page in reader.pages)
    assert len(_cjk(text)) >= 20, f"PDF 里几乎提取不到中文，字体多半没嵌进去：{text!r}"

    # 去掉所有空白再比：提取出来的换行位置跟 DOM 里的排版不一一对应
    flat = "".join(text.split())
    for paragraph in (p for p in STORY.split("\n\n") if p.strip()):
        assert "".join(paragraph.split()) in flat, f"这一段没能原样提取出来：{paragraph!r}"


def test_print_renders_images_at_print_dpi(server):
    """印刷版要按 **300dpi** 反算图片像素。

    这是"300dpi"唯一真正落地的地方。预览版走的是同一条路，只是按 150dpi
    算 —— 所以这里断言缓存键里是印刷版的像素数，把"导出时误用预览 dpi"
    这个 bug 钉死在测试里。
    """
    book_id = _make_book(server)
    image_id = _attach_image(server, book_id)
    _export(server, book_id, "print")

    _, _, width_mm, height_mm = presets.image_slot_mm("image-top", TRIM_W_MM, TRIM_H_MM)
    px = geometry.box_pixels(width_mm, height_mm, geometry.PRINT_DPI)
    cached = list((server.images_dir(book_id) / ".resized").glob(f"*__{px[0]}x{px[1]}_cover.jpg"))

    assert cached, (
        f"没有按 {px[0]}x{px[1]}（{width_mm}×{height_mm}mm @300dpi）重采样的缓存，"
        f"实际有：{[p.name for p in (server.images_dir(book_id) / '.resized').glob('*')]}"
    )


def test_print_pdf_embeds_the_image_without_upscaling(server):
    """图要真的进到 PDF 里，而且**一个像素都没被放大**。

    源图 800×1200 放进 148×115.5mm 的槽位，300dpi 下要 1749×1365 ——
    源图连宽度都不够。这种情况绝不能插值补像素（补不出细节，只让 PDF 变大），
    原样嵌进去、在报告里如实标低 dpi 才对。

    注意 PDF 里图片对象的名字是 pypdf 按 XObject 键取的（X6.jpg 之类），
    跟源文件名无关，所以只能按尺寸断言。
    """
    book_id = _make_book(server)
    _attach_image(server, book_id, size=(800, 1200))
    _, path = _export(server, book_id, "print")
    reader = PdfReader(path)

    embedded = list(reader.pages[0].images)
    assert len(embedded) == 1, f"第 1 页应该有 1 张图，实际 {len(embedded)}"
    assert embedded[0].image.size == (800, 1200)


# ---------------------------------------------------------------- 预览版


def test_preview_pdf_uses_trim_size_without_bleed(server):
    """预览版：裁切尺寸，不带出血 —— 这份是拿去屏幕分享的。"""
    book_id = _make_book(server)
    data, path = _export(server, book_id, "preview")
    reader = PdfReader(path)

    assert len(reader.pages) == 3
    width, height = _page_size_mm(reader)
    assert width == pytest.approx(TRIM_W_MM, abs=0.5)
    assert height == pytest.approx(TRIM_H_MM, abs=0.5)
    assert data["report"]["bleedMm"] == 0.0


def test_preview_renders_images_at_screen_dpi(server):
    """预览按 150dpi 反算 —— 屏幕上一英寸就 96 个 CSS 像素，150 已经有余量，
    再高只是白白撑大体积、拖慢首屏。"""
    book_id = _make_book(server)
    _attach_image(server, book_id)
    _export(server, book_id, "preview")

    _, _, width_mm, height_mm = presets.image_slot_mm("image-top", TRIM_W_MM, TRIM_H_MM)
    px = geometry.box_pixels(width_mm, height_mm, geometry.SCREEN_DPI)
    cached = list((server.images_dir(book_id) / ".resized").glob(f"*__{px[0]}x{px[1]}_cover.jpg"))

    assert cached, f"没有按 {px[0]}x{px[1]}（@150dpi）重采样的缓存"


# ---------------------------------------------------------------- 导出报告


def test_report_flags_low_dpi_images(server):
    """分辨率不够要**如实报**，不能假装是 300dpi。

    800px 铺满 148mm 只有 137dpi，用户有权知道"这张印出来会糊"，
    好自己决定是重画还是就这么印。
    """
    book_id = _make_book(server)
    _attach_image(server, book_id, size=(800, 1200))
    data, _ = _export(server, book_id, "print")
    report = data["report"]

    assert len(report["images"]) == 1
    entry = report["images"][0]
    assert entry["pageNumber"] == 1
    assert entry["sourcePx"] == {"width": 800, "height": 1200}
    assert entry["placedMm"]["width"] == pytest.approx(148.0, abs=0.5)
    assert entry["effectiveDpi"] == 137
    assert entry["belowTarget"] is True

    assert report["warnings"], "低于目标 dpi 却没有给出任何提示"
    assert "第 1 页" in report["warnings"][0]


def test_report_is_quiet_when_images_are_large_enough(server):
    """够清晰时不该报warning —— 否则用户会学会忽略它。"""
    book_id = _make_book(server)
    _attach_image(server, book_id, size=(1800, 2700))
    data, _ = _export(server, book_id, "print")

    assert data["report"]["images"][0]["belowTarget"] is False
    assert data["report"]["warnings"] == []


# ---------------------------------------------------------------- 错误路径


def test_export_rejects_book_without_pages(server):
    with httpx.Client(base_url=server.api, timeout=60) as c:
        book_id = c.post(
            "/books", json={"title": "空书", "trimSizeId": "A5", "pageCount": 3}
        ).json()["id"]
        resp = c.post(f"/books/{book_id}/export", json={"kind": "print"})

    assert resp.status_code == 400
    assert "分页" in resp.json()["detail"]


def test_download_rejects_path_traversal(server):
    with httpx.Client(base_url=server.api, timeout=60) as c:
        resp = c.get(f"/books/{_make_book(server)}/exports/..%2Fbook.json")

    assert resp.status_code in (400, 404), resp.text
