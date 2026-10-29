"""页面与块的操作接口。

这里测的是**端点行为**而不是几何：几何归 test_presets.py。重点在几条
容易悄悄退化的规则上 ——

- 预设页不能直接改块（必须先转自由页），错误信息要能指路
- 类型专属字段用错要报 400，不能静默忽略（静默忽略会让前端以为设置生效了）
- 撤销用的整页 blocks 覆盖要原子生效
"""

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    """把数据目录指到临时路径，测试不碰真实的 data/。"""
    monkeypatch.setattr(config, "BOOKS_DIR", tmp_path / "books")
    with TestClient(app) as c:
        yield c


@pytest.fixture
def book(client) -> dict:
    resp = client.post(
        "/api/books",
        json={"title": "块测试", "trimSizeId": "A5", "pageCount": 2},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def _make_pages(client, book_id: str) -> None:
    client.put(f"/api/books/{book_id}/story", json={"text": "第一段。\n\n第二段。"})
    resp = client.post(
        f"/api/books/{book_id}/paginate", json={"pageCount": 2, "preset": "image-top"}
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------- 模式转换


def test_to_free_then_edit_then_back(client, book):
    book_id = book["id"]
    _make_pages(client, book_id)

    free = client.post(f"/api/books/{book_id}/pages/1/to-free")
    assert free.status_code == 200, free.text
    page = free.json()["page"]
    assert page["mode"] == "free"
    # image-top：一个图片块 + 一个文字块
    assert len(page["blocks"]) == 2
    assert {b["type"] for b in page["blocks"]} == {"text", "image"}

    back = client.post(f"/api/books/{book_id}/pages/1/to-preset", json={"preset": "split-lr"})
    assert back.status_code == 200, back.text
    assert back.json()["page"]["mode"] == "preset"
    assert back.json()["page"]["preset"] == "split-lr"


def test_to_free_rejects_already_free(client, book):
    book_id = book["id"]
    _make_pages(client, book_id)
    client.post(f"/api/books/{book_id}/pages/1/to-free")

    resp = client.post(f"/api/books/{book_id}/pages/1/to-free")
    assert resp.status_code == 400
    assert "已经是自由" in resp.json()["detail"]


def test_to_preset_rejects_unknown_preset(client, book):
    book_id = book["id"]
    _make_pages(client, book_id)
    client.post(f"/api/books/{book_id}/pages/1/to-free")

    resp = client.post(f"/api/books/{book_id}/pages/1/to-preset", json={"preset": "不存在的版式"})
    assert resp.status_code == 400


# ---------------------------------------------------------------- 块守卫


def test_blocks_are_rejected_on_preset_page(client, book):
    """预设页没有 blocks —— 错误信息要告诉用户先转自由页，而不是只说"不行"。"""
    book_id = book["id"]
    _make_pages(client, book_id)

    resp = client.patch(f"/api/books/{book_id}/pages/1/blocks/b_x", json={"xMm": 10})
    assert resp.status_code == 400
    assert "转为自由拖拽" in resp.json()["detail"]


def test_missing_block_is_404(client, book):
    book_id = book["id"]
    _make_pages(client, book_id)
    client.post(f"/api/books/{book_id}/pages/1/to-free")

    resp = client.patch(f"/api/books/{book_id}/pages/1/blocks/b_nope", json={"xMm": 10})
    assert resp.status_code == 404


def test_text_block_rejects_image_fields(client, book):
    book_id = book["id"]
    _make_pages(client, book_id)
    page = client.post(f"/api/books/{book_id}/pages/1/to-free").json()["page"]
    text_id = next(b["id"] for b in page["blocks"] if b["type"] == "text")

    resp = client.patch(
        f"/api/books/{book_id}/pages/1/blocks/{text_id}", json={"fit": "contain"}
    )
    assert resp.status_code == 400
    assert "fit" in resp.json()["detail"]


def test_image_block_rejects_text_fields(client, book):
    book_id = book["id"]
    _make_pages(client, book_id)
    page = client.post(f"/api/books/{book_id}/pages/1/to-free").json()["page"]
    image_id = next(b["id"] for b in page["blocks"] if b["type"] == "image")

    resp = client.patch(
        f"/api/books/{book_id}/pages/1/blocks/{image_id}", json={"content": "改文字"}
    )
    assert resp.status_code == 400
    assert "content" in resp.json()["detail"]


# ---------------------------------------------------------------- 块的增删改


def test_add_block_lands_on_top_and_can_be_deleted(client, book):
    book_id = book["id"]
    _make_pages(client, book_id)
    before = client.post(f"/api/books/{book_id}/pages/1/to-free").json()["page"]
    top = max(b["zIndex"] for b in before["blocks"])

    added = client.post(
        f"/api/books/{book_id}/pages/1/blocks",
        json={"type": "text", "xMm": 15, "yMm": 15, "widthMm": 70, "heightMm": 40},
    )
    assert added.status_code == 200, added.text
    blocks = added.json()["page"]["blocks"]
    new_block = blocks[-1]
    # 新块必须在最上层，否则加完看不见，用户以为没生效
    assert new_block["zIndex"] > top

    removed = client.delete(f"/api/books/{book_id}/pages/1/blocks/{new_block['id']}")
    assert removed.status_code == 200
    assert len(removed.json()["page"]["blocks"]) == len(blocks) - 1


def test_patch_block_updates_geometry(client, book):
    book_id = book["id"]
    _make_pages(client, book_id)
    page = client.post(f"/api/books/{book_id}/pages/1/to-free").json()["page"]
    block_id = page["blocks"][0]["id"]

    resp = client.patch(
        f"/api/books/{book_id}/pages/1/blocks/{block_id}",
        json={"xMm": 33.5, "yMm": 12.25, "rotationDeg": 5},
    )
    assert resp.status_code == 200, resp.text
    saved = next(b for b in resp.json()["page"]["blocks"] if b["id"] == block_id)
    assert (saved["xMm"], saved["yMm"], saved["rotationDeg"]) == (33.5, 12.25, 5)


def test_bleed_flag_is_settable_only_on_image_blocks(client, book):
    book_id = book["id"]
    _make_pages(client, book_id)
    page = client.post(f"/api/books/{book_id}/pages/1/to-free").json()["page"]
    image_id = next(b["id"] for b in page["blocks"] if b["type"] == "image")
    text_id = next(b["id"] for b in page["blocks"] if b["type"] == "text")

    ok = client.patch(f"/api/books/{book_id}/pages/1/blocks/{image_id}", json={"bleed": True})
    assert ok.status_code == 200, ok.text
    assert next(b for b in ok.json()["page"]["blocks"] if b["id"] == image_id)["bleed"] is True

    bad = client.patch(f"/api/books/{book_id}/pages/1/blocks/{text_id}", json={"bleed": True})
    assert bad.status_code == 400


# ---------------------------------------------------------------- 整页覆盖（撤销用）


def test_blocks_overwrite_replaces_whole_page(client, book):
    """撤销/重做靠它一次性还原：必须是替换而不是合并。"""
    book_id = book["id"]
    _make_pages(client, book_id)
    original = client.post(f"/api/books/{book_id}/pages/1/to-free").json()["page"]
    snapshot = original["blocks"]

    # 先破坏：删掉一个块、挪走另一个
    client.delete(f"/api/books/{book_id}/pages/1/blocks/{snapshot[0]['id']}")
    moved = client.patch(
        f"/api/books/{book_id}/pages/1/blocks/{snapshot[1]['id']}", json={"xMm": 99}
    ).json()["page"]
    assert len(moved["blocks"]) == 1

    restored = client.patch(
        f"/api/books/{book_id}/pages/1", json={"blocks": snapshot}
    )
    assert restored.status_code == 200, restored.text
    blocks = restored.json()["page"]["blocks"]
    assert len(blocks) == len(snapshot)
    assert [b["id"] for b in blocks] == [b["id"] for b in snapshot]
    # 位置也回来了，不只是数量对上
    assert next(b for b in blocks if b["id"] == snapshot[1]["id"])["xMm"] == snapshot[1]["xMm"]


def test_blocks_overwrite_rejected_on_preset_page(client, book):
    book_id = book["id"]
    _make_pages(client, book_id)

    resp = client.patch(f"/api/books/{book_id}/pages/1", json={"blocks": []})
    assert resp.status_code == 400
    assert "自由页" in resp.json()["detail"]


# ---------------------------------------------------------------- 拆分 / 合并


def _make_many_pages(client, book_id: str, count: int = 6) -> list[dict]:
    client.put(f"/api/books/{book_id}/story", json={"text": "从前有座山。" * 40})
    resp = client.post(
        f"/api/books/{book_id}/paginate", json={"pageCount": count, "preset": "image-top"}
    )
    assert resp.status_code == 200, resp.text
    pages = resp.json()["pages"]
    assert len(pages) == count, f"分页没给出 {count} 页：{len(pages)}"
    return pages


def test_split_gives_the_two_halves_distinct_ids(client, book):
    """拆出来的两页必须有不同的 id。

    这里钉的是一个真实踩过的坑：head/tail 由 `page.model_copy(deep=True)` 造，
    而 `PageBase.id` 是 default_factory —— model_copy 复制的是**已生成**的字段值，
    不重跑 default_factory，于是两页拿着同一个 id。后果是前端 `:key="page.id"`
    撞车、v-model 绑错页（改第 4 页的字写进第 5 页），而渲染出来一切正常。

    head 保留原页 id 是**有意**的：前端不用重新定位当前页。
    """
    book_id = book["id"]
    before = _make_many_pages(client, book_id, 3)

    resp = client.post(f"/api/books/{book_id}/pages/1/split", json={"atCharIndex": 10})
    assert resp.status_code == 200, resp.text
    pages = resp.json()["pages"]

    assert len(pages) == 4
    assert pages[0]["id"] != pages[1]["id"], "拆出来的两页 id 相同"
    assert pages[0]["id"] == before[0]["id"], "head 应当保留原页 id"
    assert pages[1]["id"] not in {p["id"] for p in before}, "tail 应当是新 id"
    # 全书 id 仍然唯一 —— 重复 id 会一路传染给后续的分页/选择状态
    ids = [p["id"] for p in pages]
    assert len(set(ids)) == len(ids)


def test_split_keeps_the_text_verbatim(client, book):
    book_id = book["id"]
    _make_many_pages(client, book_id, 2)
    original = client.get(f"/api/books/{book_id}").json()["pages"][0]["text"]

    pages = client.post(
        f"/api/books/{book_id}/pages/1/split", json={"atCharIndex": 10}
    ).json()["pages"]

    assert pages[0]["text"] + pages[1]["text"] == original.replace(" ", "").replace("\n", "")


# ---------------------------------------------------------------- 批量删除


def test_batch_delete_removes_exactly_the_asked_pages(client, book):
    """删 [2,4] 之后，剩下的必须是原来的 1,3,5,6。

    这条专门钉**倒序 pop**。正序删会让下标整体前移：删掉第 2 页后原来的第 3 页
    变成第 2 页，接着 pop(4) 删的就是原来的第 6 页 —— 页数对得上，删错的页
    只有比对 id 才看得出来。
    """
    book_id = book["id"]
    before = [p["id"] for p in _make_many_pages(client, book_id, 6)]

    resp = client.post(f"/api/books/{book_id}/pages/batch-delete", json={"pageNumbers": [2, 4]})
    assert resp.status_code == 200, resp.text
    after = [p["id"] for p in resp.json()["pages"]]

    assert after == [before[0], before[2], before[4], before[5]]


def test_batch_delete_ignores_duplicates(client, book):
    """同一页被勾两次也只该删一次 —— 否则会顺手删掉后面一页。"""
    book_id = book["id"]
    before = [p["id"] for p in _make_many_pages(client, book_id, 4)]

    resp = client.post(
        f"/api/books/{book_id}/pages/batch-delete", json={"pageNumbers": [2, 2, 2]}
    )
    assert resp.status_code == 200, resp.text
    assert [p["id"] for p in resp.json()["pages"]] == [before[0], before[2], before[3]]


def test_batch_delete_is_all_or_nothing(client, book):
    """有一个越界就整单拒绝，一页都不删。

    部分成功会让前端的选择状态和实际页面对不上，而用户看到的是"删了 N 页"。
    """
    book_id = book["id"]
    before = [p["id"] for p in _make_many_pages(client, book_id, 5)]

    resp = client.post(
        f"/api/books/{book_id}/pages/batch-delete", json={"pageNumbers": [1, 999]}
    )
    assert resp.status_code == 404

    after = client.get(f"/api/books/{book_id}").json()["pages"]
    assert [p["id"] for p in after] == before, "越界请求之后一页都不该少"


def test_batch_delete_rejects_empty_list(client, book):
    book_id = book["id"]
    _make_many_pages(client, book_id, 3)

    resp = client.post(f"/api/books/{book_id}/pages/batch-delete", json={"pageNumbers": []})
    assert resp.status_code == 400


def test_batch_delete_persists(client, book):
    """删完要落盘 —— 重新读回来的才是用户下次打开看到的。"""
    book_id = book["id"]
    _make_many_pages(client, book_id, 5)

    client.post(f"/api/books/{book_id}/pages/batch-delete", json={"pageNumbers": [1, 2]})

    reloaded = client.get(f"/api/books/{book_id}").json()
    assert len(reloaded["pages"]) == 3


def test_page_edit_survives_reload(client, book):
    """改完要真的落盘 —— 重新读回来的才是用户下次打开看到的。"""
    book_id = book["id"]
    _make_pages(client, book_id)
    page = client.post(f"/api/books/{book_id}/pages/1/to-free").json()["page"]
    block_id = page["blocks"][0]["id"]
    client.patch(f"/api/books/{book_id}/pages/1/blocks/{block_id}", json={"xMm": 42})

    reloaded = client.get(f"/api/books/{book_id}").json()
    saved = next(
        b for b in reloaded["pages"][0]["blocks"] if b["id"] == block_id
    )
    assert saved["xMm"] == 42


# ---------------------------------------------------------------- 叠字区矩形


def _full_bleed_book(client, page_count: int = 2) -> str:
    resp = client.post(
        "/api/books", json={"title": "矩形", "trimSizeId": "A5", "pageCount": page_count}
    )
    book_id = resp.json()["id"]
    client.put(f"/api/books/{book_id}/story", json={"text": "第一段。\n\n第二段。"})
    resp = client.post(
        f"/api/books/{book_id}/paginate",
        json={"pageCount": page_count, "preset": "full-bleed-overlay"},
    )
    assert resp.status_code == 200, resp.text
    return book_id


def _options(client, book_id: str, number: int = 1) -> dict:
    return client.get(f"/api/books/{book_id}").json()["pages"][number - 1]["options"]


RECT = {"xMm": 20.0, "yMm": 30.0, "widthMm": 120.0, "heightMm": 45.0}


def test_overlay_rect_is_saved_and_cleared(client):
    """`overlayRect` 的写入与清除 —— 前端「恢复自动」和「改叠字位置」两个动作
    都依赖这一条。

    这里必须走**真的重新读一遍**：options 是整对象替换（`page.options =
    payload.options`），序列化漏了这个字段的话，同一次响应里看不出来。
    """
    book_id = _full_bleed_book(client)
    options = _options(client, book_id)
    assert options["overlayRect"] is None  # 默认必须是 null，见 PresetPageOptions
    assert options["overlayPosition"] == "auto"

    resp = client.patch(
        f"/api/books/{book_id}/pages/1", json={"options": {**options, "overlayRect": RECT}}
    )
    assert resp.status_code == 200, resp.text
    assert _options(client, book_id)["overlayRect"] == RECT

    # 「恢复自动」：矩形清空 + 落点回到 auto，同一次请求里发出去
    resp = client.patch(
        f"/api/books/{book_id}/pages/1",
        json={"options": {**options, "overlayRect": None, "overlayPosition": "auto"}},
    )
    assert resp.status_code == 200, resp.text
    assert _options(client, book_id)["overlayRect"] is None


def test_out_of_range_rect_does_not_break_the_book(client):
    """越界的矩形能被存下来，加载时不报错 —— 由渲染端夹进成品区。

    这条守的是「手改 book.json 打错一个负号，代价不该是这本书打不开了」。
    """
    book_id = _full_bleed_book(client)
    options = _options(client, book_id)
    bad = {"xMm": -30.0, "yMm": 9999.0, "widthMm": 500.0, "heightMm": -5.0}
    resp = client.patch(
        f"/api/books/{book_id}/pages/1", json={"options": {**options, "overlayRect": bad}}
    )
    assert resp.status_code == 200, resp.text

    reloaded = client.get(f"/api/books/{book_id}")
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json()["pages"][0]["options"]["overlayRect"] == bad
