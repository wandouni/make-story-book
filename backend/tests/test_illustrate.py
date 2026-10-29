"""配图相关：提示词拼装、占位图、图片版本号、任务表。

不发真实 API 请求 —— 这些测试要能在没配 key 的机器上跑。
"""

import asyncio
import io
import math
import re

import pytest
from PIL import Image

from app import config
from app.models.book import Book, FreePage, ImageBlock, PresetPage, TextBlock, TrimSize
from app.services import illustrator, job_manager, placeholder, prompt_builder


@pytest.fixture
def book() -> Book:
    return Book(
        title="测试绘本",
        style="卡通",
        characterDescription="一只围着红围巾的小白兔",
        trimSize=TrimSize.from_id("SQ200"),
        pages=[
            PresetPage(text="小兔子出门了。", preset="image-top"),
            PresetPage(text="它遇见了乌龟。", preset="full-bleed-overlay"),
            PresetPage(text="", preset="image-only"),
        ],
    )


# ---------------------------------------------------------------- 提示词拼装


def test_enhance_prompt_includes_everything(book: Book):
    result = prompt_builder.enhance_prompt("小兔子走在森林里", book, book.pages[0])

    assert "一只围着红围巾的小白兔" in result
    assert "小兔子走在森林里" in result
    assert config.IMAGE_STYLES["卡通"] in result
    assert "儿童绘本插画风格" in result


def test_enhance_prompt_has_no_english(book: Book):
    """绘本是给中国儿童看的：拼出来的提示词里不该有英文单词。

    按 ASCII 字母判而不是"必须含中文"：数字、标点、"3-5 岁"这类都是正常的，
    真正会让画面上印出英文的是字母。

    判的是**连续两个字母以上**（即英文单词），不是单个字母：风格文案里的
    "Q萌"是中文里的写法，用户自己在 style-spec.md 里就这么写的，不该被这条
    测试逼着改。
    """
    for page in book.pages:
        result = prompt_builder.enhance_prompt("小兔子在雪地里走", book, page)
        assert not re.search(r"[A-Za-z]{2,}", result), result


def test_no_style_or_hint_contains_english():
    """风格表、构图提示和镜头语言都要是中文 —— 它们会被拼进每一条提示词。"""
    for key, prompt in config.IMAGE_STYLES.items():
        assert not re.search(r"[A-Za-z]{2,}", key), key
        assert not re.search(r"[A-Za-z]{2,}", prompt), (key, prompt)

    for preset, hint in config.PRESET_PROMPT_HINTS.items():
        assert not re.search(r"[A-Za-z]{2,}", hint), (preset, hint)

    # 镜头语言是后加的。忘了并进这里的话，一句 "close-up" 会一路拼到生图
    # 提示词里，画面上就可能印出英文字样 —— 这条测试的价值全在覆盖面上。
    for group, options in config.DESIGN_ELEMENTS.items():
        for option in options:
            assert not re.search(r"[A-Za-z]{2,}", option), (group, option)


# ---------------------------------------------------------------- 镜头语言


def _signature(number: int) -> tuple:
    design = prompt_builder.design_for_page(number)
    return tuple(design[name] for name in config.DESIGN_ORDER)


def test_design_tables_are_complete():
    """三张表要覆盖同一组维度 —— 漏一个就是静默少一个维度。"""
    assert set(config.DESIGN_ELEMENTS) == set(config.DESIGN_STRIDE)
    assert set(config.DESIGN_ELEMENTS) == set(config.DESIGN_ORDER)
    for group, options in config.DESIGN_ELEMENTS.items():
        assert len(options) > 1, f"{group} 只有一个取值，轮转不出变化"


def test_design_stride_is_coprime_with_length():
    """步长必须与组长度互质，否则取不满一轮就开始打转。

    长度 4、步长 2 的话只会在两个值之间来回跳 —— 看上去"有轮转"，
    实际整本书只有两种景别，比不做还糟。
    """
    for group, options in config.DESIGN_ELEMENTS.items():
        _, stride = config.DESIGN_STRIDE[group]
        assert math.gcd(stride, len(options)) == 1, (group, stride, len(options))


def test_design_never_repeats_between_neighbouring_pages():
    """相邻两页的每个维度都必须不同。

    对开预览里相邻两页是并排显示的，撞了会一眼看出"这两页一样"——
    正是这次要解决的"画面太单一"。
    """
    for number in range(1, 30):
        current = prompt_builder.design_for_page(number)
        following = prompt_builder.design_for_page(number + 1)
        for group in config.DESIGN_ORDER:
            assert current[group] != following[group], (number, group, current[group])


def test_design_is_deterministic():
    """同一页码每次挑到同一组。

    随机会撞车，也会让结果没法复现 —— "第 5 页那个角度好，别的页照这个来"
    这种要求就没法提了，重新生成一次整本的画面语言还会全变。
    """
    assert prompt_builder.design_for_page(7) == prompt_builder.design_for_page(7)
    assert _signature(7) == _signature(7)


def test_design_cycle_covers_every_option():
    """跑满一个周期要能取遍每个维度的所有取值，不能有永远轮不到的那几项。"""
    cycle = math.lcm(*(len(options) for options in config.DESIGN_ELEMENTS.values()))
    for group, options in config.DESIGN_ELEMENTS.items():
        seen = {prompt_builder.design_for_page(n)[group] for n in range(1, cycle + 1)}
        assert seen == set(options), (group, sorted(seen))


def test_design_hint_uses_a_fixed_order():
    """语序固定，读起来才像人话，也不受 dict 顺序影响。"""
    design = {name: f"<{name}>" for name in reversed(config.DESIGN_ORDER)}
    assert prompt_builder.design_hint(design) == "，".join(
        f"<{name}>" for name in config.DESIGN_ORDER
    )


def test_enhance_prompt_carries_the_pages_design(book: Book):
    """镜头语言要进最终提示词 —— 这是保底的那一层。

    模型可能没照要求写，用户也可能手改过提示词；只有拼装层是确定性的，
    生图时一定带得上。
    """
    for number in (1, 2, 3):
        result = prompt_builder.enhance_prompt("小兔子在雪地里走", book, book.pages[number - 1])
        hint = prompt_builder.design_hint(prompt_builder.design_for_page(number))
        assert hint in result, (number, hint)


def test_enhance_prompt_design_follows_the_page_number(book: Book):
    """不传页码时按 page 在书里的位置现算，不是一律当第 1 页。"""
    first = prompt_builder.enhance_prompt("scene", book, book.pages[0])
    second = prompt_builder.enhance_prompt("scene", book, book.pages[1])

    assert prompt_builder.design_hint(prompt_builder.design_for_page(1)) in first
    assert prompt_builder.design_hint(prompt_builder.design_for_page(2)) in second


def test_page_number_lookup_uses_id_not_value(book: Book):
    """两页内容完全一样时，按值找会返回靠前那个的下标。

    构造方式是 `model_copy` 再手工换 id：**model_copy 会把 id 一起复制过来**
    （它复制的是已生成的字段值，不重跑 default_factory —— 拆分页面时踩过
    这个坑）。所以这里必须手动换，才能造出"内容相同、id 不同"这一对。
    """
    twin = book.pages[0].model_copy(deep=True)
    twin.id = "p_twin"
    book.pages[1] = twin

    assert prompt_builder._page_number(book, book.pages[1]) == 2


def test_base_prompt_forbids_text_in_the_image(book: Book):
    """画面里不能出现文字。

    这条要求必须落在**确定性拼接**的这一层：只靠 LLM 指令的话，模型看到
    "故事""标题"这类词就会顺手在画面上写一串英文标题（真出过一张写着
    "Little Lanternfish Finds Mom" 的封面）。
    """
    result = prompt_builder.enhance_prompt("小兔子在雪地里走", book, book.pages[0])
    assert "不要出现任何文字" in result


def test_enhance_prompt_adds_preset_hint(book: Book):
    """满版叠字必须要求下方留白，否则文字会压在人脸上。"""
    overlay = prompt_builder.enhance_prompt("scene", book, book.pages[1])
    assert config.PRESET_PROMPT_HINTS["full-bleed-overlay"] in overlay

    top = prompt_builder.enhance_prompt("scene", book, book.pages[0])
    assert config.PRESET_PROMPT_HINTS["image-top"] in top


def test_enhance_prompt_falls_back_when_no_character():
    plain = Book(characterDescription="", pages=[PresetPage(text="x")])
    result = prompt_builder.enhance_prompt("scene", plain, plain.pages[0])
    assert prompt_builder._NO_CHARACTER in result


def test_enhance_prompt_drops_exact_duplicate_parts(book: Book):
    """角色描述被原样写进提示词时，不应在结果里出现两遍。

    去重是整段精确匹配，不做子串判断 —— 子串匹配会误伤真正需要保留的
    相似段落，代价比偶尔重复一段大得多。
    """
    description = book.characterDescription
    result = prompt_builder.enhance_prompt(description, book, book.pages[0])
    assert result.count(description) == 1


def test_page_text_for_prompt_reads_free_page_blocks(book: Book):
    """自由拖拽页的 page.text 可能是空的，文字在 blocks 里。"""
    page = FreePage(
        text="",
        blocks=[
            TextBlock(content="它轻轻推开了门。"),
            ImageBlock(xMm=0, yMm=0, widthMm=10, heightMm=10),
            TextBlock(content="门后是一片湖。"),
        ],
    )
    assert prompt_builder.page_text_for_prompt(page) == "它轻轻推开了门。\n门后是一片湖。"


def test_pages_needing_prompts_skips_existing(book: Book):
    book.pages[0].imagePrompt = "already here"
    assert prompt_builder.pages_needing_prompts(book.pages, [1, 2, 3]) == [2]


def test_pages_needing_prompts_skips_empty_text(book: Book):
    # 第 3 页 text 为空且没有 blocks，生成提示词时模型拿不到任何信息
    assert prompt_builder.pages_needing_prompts(book.pages, [1, 2, 3]) == [1, 2]


# ---------------------------------------------------------------- 重生成提示词


@pytest.fixture
def prompts_client(tmp_path, monkeypatch):
    """一个不发真实请求的 client：把 job 的协程换掉，只记下端点决定了哪些页。"""
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(config, "BOOKS_DIR", tmp_path / "books")
    calls: list[list[int]] = []

    async def _noop() -> None:
        return None

    def _record(_job, _book_id, targets):
        # 在**调用**时记录而不是协程体里：协程是异步跑的，等它执行完再断言
        # 就得靠 sleep 或轮询，测试会变得不可靠。
        calls.append(list(targets))
        return _noop()

    monkeypatch.setattr(illustrator, "run_prompts_job", _record)

    with TestClient(app) as c:
        yield c, calls


def _prompts_book(client) -> str:
    book = client.post(
        "/api/books", json={"title": "重生成", "trimSizeId": "SQ200", "pageCount": 3}
    ).json()
    book_id = book["id"]
    client.put(f"/api/books/{book_id}/story", json={"text": "小兔子出门了。" * 30})
    client.post(f"/api/books/{book_id}/paginate", json={"pageCount": 3, "preset": "image-top"})
    return book_id


def test_prompts_skip_pages_that_already_have_one(prompts_client):
    """默认行为：已有提示词的页跳过 —— 批量生成不该覆盖用户手改过的提示词。"""
    client, calls = prompts_client
    book_id = _prompts_book(client)

    pages = client.get(f"/api/books/{book_id}").json()["pages"]
    client.patch(f"/api/books/{book_id}/pages/1", json={"imagePrompt": "我自己写的"})

    resp = client.post(f"/api/books/{book_id}/prompts/generate", json={})
    assert resp.status_code == 200, resp.text
    assert calls[-1] == [2, 3], "第 1 页已有提示词，不该被重新生成"


def test_prompts_overwrite_regenerates_pages_that_already_have_one(prompts_client):
    """overwrite=true 时要覆盖 —— 否则单页「重新生成」按钮点了没反应。

    踩过的坑：过滤写在 run_prompts_job 里，端点算好 targets 传下去又被过滤
    一遍，于是"重新生成"永远只对空提示词的页生效，而界面上按钮是可点的。
    """
    client, calls = prompts_client
    book_id = _prompts_book(client)
    client.patch(f"/api/books/{book_id}/pages/1", json={"imagePrompt": "我自己写的"})

    resp = client.post(
        f"/api/books/{book_id}/prompts/generate",
        json={"pageNumbers": [1], "overwrite": True},
    )
    assert resp.status_code == 200, resp.text
    assert calls[-1] == [1]


def test_prompts_rejects_when_everything_already_has_one(prompts_client):
    """整本都有提示词且没开 overwrite：明确报错，而不是静默什么都不做。"""
    client, calls = prompts_client
    book_id = _prompts_book(client)
    for number in (1, 2, 3):
        client.patch(f"/api/books/{book_id}/pages/{number}", json={"imagePrompt": "已有"})

    resp = client.post(f"/api/books/{book_id}/prompts/generate", json={})
    assert resp.status_code == 400
    assert "重新生成" in resp.json()["detail"]


# ---------------------------------------------------------------- 占位图


def test_placeholder_is_valid_png():
    data = placeholder.make_placeholder(640, 480, detail="第 1 页", prompt="a rabbit")
    image = Image.open(io.BytesIO(data))
    assert image.format == "PNG"
    assert image.size == (640, 480)


def test_placeholder_clamps_absurd_sizes():
    """生图尺寸来自开本比例，理论上正常；但真传进来个 0 也不能崩。"""
    data = placeholder.make_placeholder(0, 99999)
    image = Image.open(io.BytesIO(data))
    assert image.size == (320, 4096)


def test_placeholder_font_supports_cjk():
    font = placeholder._load_font(40)
    # 能拿到中文字形说明字体支持中文；不支持时会回退成 .notdef（宽度为 0 或方框）
    try:
        mask = font.getmask("图")
        assert mask.size[0] > 0
    except Exception:  # noqa: BLE001
        pytest.skip("本机没有可用中文字体")


def test_size_to_px():
    assert placeholder.size_to_px("1920x2560") == (1920, 2560)
    assert placeholder.size_to_px("garbage") == (1024, 1024)


# ---------------------------------------------------------------- 版本号


def test_next_image_id_starts_at_v1(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BOOKS_DIR", tmp_path)
    assert illustrator.next_image_id("book_test", 1) == "p001_v1"


def test_next_image_id_uses_max_not_count(tmp_path, monkeypatch):
    """删掉 v2 后按文件个数算会重新分配出已存在的 v3，把旧图覆盖掉。"""
    monkeypatch.setattr(config, "BOOKS_DIR", tmp_path)
    images = tmp_path / "book_test" / "images"
    images.mkdir(parents=True)

    for version in (1, 3):
        (images / f"p001_v{version}.png").write_bytes(b"x")

    assert illustrator.next_image_id("book_test", 1) == "p001_v4"


def test_next_image_id_is_per_page(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BOOKS_DIR", tmp_path)
    images = tmp_path / "book_test" / "images"
    images.mkdir(parents=True)
    (images / "p002_v5.png").write_bytes(b"x")

    assert illustrator.next_image_id("book_test", 1) == "p001_v1"
    assert illustrator.next_image_id("book_test", 2) == "p002_v6"


# ---------------------------------------------------------------- 历史


def test_page_history_newest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BOOKS_DIR", tmp_path)
    book_id = "book_test"
    (tmp_path / book_id / "images").mkdir(parents=True)

    for version, stamp in ((1, "2026-01-01T00:00:00+00:00"), (2, "2026-02-01T00:00:00+00:00")):
        illustrator.save_image(
            book_id,
            f"p001_v{version}",
            b"data",
            {"pageNumber": 1, "createdAt": stamp, "prompt": f"v{version}"},
        )

    history = illustrator.page_history(book_id, 1)
    assert [entry["imageId"] for entry in history] == ["p001_v2", "p001_v1"]
    assert history[0]["prompt"] == "v2"


def test_page_history_empty_for_unknown_page(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BOOKS_DIR", tmp_path)
    (tmp_path / "book_test" / "images").mkdir(parents=True)
    assert illustrator.page_history("book_test", 9) == []


# ---------------------------------------------------------------- 页码校验


def test_resolve_page_numbers_dedupes_and_sorts(book: Book):
    numbers, error = illustrator.resolve_page_numbers(book, [3, 1, 2, 1])
    assert error == ""
    assert numbers == [1, 2, 3]


def test_resolve_page_numbers_defaults_to_all(book: Book):
    numbers, error = illustrator.resolve_page_numbers(book, None)
    assert error == ""
    assert numbers == [1, 2, 3]


def test_resolve_page_numbers_rejects_out_of_range(book: Book):
    numbers, error = illustrator.resolve_page_numbers(book, [99, -1])
    assert numbers == []
    assert "1..3" in error


def test_resolve_page_numbers_rejects_empty_book():
    empty = Book(pages=[])
    numbers, error = illustrator.resolve_page_numbers(empty, None)
    assert numbers == []
    assert "分页" in error


# ---------------------------------------------------------------- 服务可用性


def test_doubao_needs_ark_key(monkeypatch):
    from app.services import image_generator

    monkeypatch.setattr(config, "ARK_API_KEY", "")
    assert "ARK_API_KEY" in image_generator.service_unavailable_reason("doubao")

    monkeypatch.setattr(config, "ARK_API_KEY", "k")
    assert image_generator.service_available("doubao")


def test_tongyi_unavailable_when_endpoint_is_not_dashscope(monkeypatch):
    """本项目允许把 API_ENDPOINT 指向火山方舟等兼容端点。

    那种情况下 API_KEY 是别家的 key，发给通义的 DashScope 只会 401。
    必须提前判成不可用 —— 否则用户配完整本图才看到全失败。
    """
    from app.services import image_generator

    monkeypatch.setattr(config, "API_KEY", "some-ark-key")
    monkeypatch.setattr(config, "API_ENDPOINT", "https://ark.cn-beijing.volces.com/api/coding/v3")

    reason = image_generator.service_unavailable_reason("tongyi")
    assert reason, "指向非 DashScope 端点时通义应判为不可用"
    assert "DashScope" in reason
    assert not image_generator.service_available("tongyi")


def test_tongyi_available_on_dashscope(monkeypatch):
    from app.services import image_generator

    monkeypatch.setattr(config, "API_KEY", "sk-dashscope")
    monkeypatch.setattr(config, "API_ENDPOINT", "https://dashscope.aliyuncs.com/compatible-mode/v1")

    assert image_generator.service_available("tongyi")


def test_missing_key_reason_differs_from_provider_mismatch(monkeypatch):
    """两种情况给不同提示：一个让用户去填 key，一个让用户去改配置。"""
    from app.services import image_generator

    monkeypatch.setattr(config, "API_KEY", "")
    monkeypatch.setattr(config, "API_ENDPOINT", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    missing = image_generator.service_unavailable_reason("tongyi")

    monkeypatch.setattr(config, "API_KEY", "some-ark-key")
    monkeypatch.setattr(config, "API_ENDPOINT", "https://ark.cn-beijing.volces.com/api/v3")
    mismatch = image_generator.service_unavailable_reason("tongyi")

    assert missing != mismatch


# ---------------------------------------------------------------- 任务表


def test_job_progress_counts():
    async def scenario():
        job = await job_manager.create("illustrate", "book_x", [1, 2, 3])
        assert (job.total, job.completed, job.failed) == (3, 0, 0)

        await job_manager.mark(job, 1, "done", imageId="p001_v1")
        await job_manager.mark(job, 2, "failed", error="boom")

        snapshot = job.to_dict()
        assert snapshot["completed"] == 1
        assert snapshot["failed"] == 1
        assert snapshot["items"][0]["imageId"] == "p001_v1"
        assert snapshot["items"][1]["error"] == "boom"

    asyncio.run(scenario())


def test_job_publishes_full_snapshot_to_subscribers():
    async def scenario():
        job = await job_manager.create("illustrate", "book_x", [1, 2])
        queue = job_manager.subscribe(job)

        await job_manager.mark(job, 1, "done", imageId="p001_v1")
        payload = queue.get_nowait()

        # 每帧都是完整快照，前端整体替换即可，不怕漏帧
        assert payload["total"] == 2
        assert payload["completed"] == 1
        assert len(payload["items"]) == 2

        job_manager.unsubscribe(job, queue)
        await job_manager.mark(job, 2, "done")
        assert queue.empty()

    asyncio.run(scenario())


def test_slow_subscriber_does_not_block_job():
    """队列满时丢帧而不是阻塞 —— 下一帧是全量快照，不会算错。"""

    async def scenario():
        job = await job_manager.create("illustrate", "book_x", list(range(1, 5)))
        queue = job_manager.subscribe(job)

        for number in range(1, 5):
            for _ in range(20):  # 塞满 64 格
                await job_manager.mark(job, number, "running")
            await job_manager.mark(job, number, "done")

        # 没卡住，且最后状态是对的
        assert job.completed == 4
        assert queue.qsize() <= 64

    asyncio.run(scenario())


def test_job_cancel_is_cooperative():
    async def scenario():
        job = await job_manager.create("illustrate", "book_x", [1])
        assert job_manager.request_cancel(job) is True
        assert job_manager.is_cancelled(job) is True

        await job_manager.set_status(job, "done")
        # 已经结束的任务不能再取消
        assert job_manager.request_cancel(job) is False

    asyncio.run(scenario())


def test_sse_format_is_single_line():
    """data 里出现裸换行会把一帧拆成两帧，前端解析直接崩。"""
    frame = job_manager.sse_format({"message": "第一行\n第二行"})
    assert frame.startswith("event: progress\ndata: ")
    assert frame.endswith("\n\n")
    assert frame.count("\n") == 3  # event 行 + data 行 + 结尾空行


def test_reset_stuck_pages(tmp_path, monkeypatch):
    """进程被杀后 book.json 里残留的 running 要清掉，否则前端一直转圈。"""
    from app.storage import book_store

    monkeypatch.setattr(config, "BOOKS_DIR", tmp_path / "books")

    book = Book(title="卡住的书", pages=[PresetPage(text="x"), PresetPage(text="y")])
    book.pages[0].imageStatus = "running"
    book.pages[0].imagePrompt = "a prompt"
    book.pages[1].imageStatus = "running"
    book_store.create(book)

    assert illustrator.reset_stuck_pages() == 2

    reloaded = book_store.load(book.id)
    assert reloaded.pages[0].imageStatus == "pending"  # 有提示词 → 可以重试
    assert reloaded.pages[1].imageStatus == "none"  # 没提示词 → 回到未开始
