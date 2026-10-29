"""智能排版的编排：选页、问模型、落笔、撤销。

**不碰真的图片分析**：`blank_candidates` / `region_luminance` 被打桩成几个固定
的候选框。理由和别处一样 —— 那部分归 `test_layout_analysis.py`，这里要测的是
"拿到候选之后怎么办"。真的跑一遍图像分析会让每个用例都依赖一张合成图，
而断言的点（选了哪个框、写了什么字段、撤销回到哪）跟图片长什么样无关。

**也不发任何网络请求**：`chat_json` 被打桩。这是本仓第二处 stub 文本模型
（第一处是 test_illustrate 的 prompts_client），范式照抄它。
"""

import json

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app
from app.models.book import (
    Book,
    FreePage,
    ImageBlock,
    PresetPage,
    TextBlock,
    TextStyle,
    TrimSize,
)
from app.services import auto_layout, job_manager, layout_analysis
from app.storage import book_store, paths

TRIM_W, TRIM_H = 148.0, 210.0  # A5 竖版

SENT = "小兔子推开木门，外面是一片银白的雪地。"


# ---------------------------------------------------------------- 打桩


@pytest.fixture
def books_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BOOKS_DIR", tmp_path / "books")
    return tmp_path / "books"


@pytest.fixture(autouse=True)
def clean_jobs():
    """任务表是模块级的全局状态，不隔离的话 409 那条用例会污染后面的。"""
    job_manager._jobs.clear()  # noqa: SLF001
    yield
    job_manager._jobs.clear()  # noqa: SLF001


# 两个候选：0 号在**下半张**（更空、更亮），1 号在上半张。测试里反复用到
# "下半张"这个特征 —— A5 高 210mm，下半张就是 y > 105。
CAND_DOWN = layout_analysis.Candidate(
    x=0.05, y=0.70, w=0.90, h=0.20, score=0.10, kind="box", lum=0.92, detail=0.02
)
CAND_UP = layout_analysis.Candidate(
    x=0.05, y=0.05, w=0.90, h=0.20, score=0.30, kind="box", lum=0.80, detail=0.20
)


@pytest.fixture
def candidates(monkeypatch):
    """把候选框和区域亮度换成固定值，绕开真图片。"""

    def _install(*cands, lum=0.92):
        chosen = list(cands) or [CAND_DOWN, CAND_UP]
        monkeypatch.setattr(
            auto_layout.layout_analysis, "blank_candidates", lambda *a, **k: list(chosen)
        )
        monkeypatch.setattr(
            auto_layout.layout_analysis, "region_luminance", lambda *a, **k: lum
        )
        return chosen

    return _install


@pytest.fixture
def model(monkeypatch):
    """脚本化的文本模型：记下每一次的 prompt，按脚本返回。

    `replies` 是一个队列（逐页降级路径要按顺序取），`reply` 是固定返回值。
    两个都不设时返回 None —— 那正是"模型没听懂"的默认行为。
    """
    seen: list[str] = []
    script: dict = {"reply": None, "replies": None}

    async def _fake(system: str, user: str, temperature: float = 0.2):
        seen.append(user)
        if script["replies"] is not None:
            return script["replies"].pop(0)
        return script["reply"]

    monkeypatch.setattr(auto_layout.text_generator, "chat_json", _fake)
    return seen, script


def _reply_for(pages: list[int], box: int = 1, color: str = "#FFFFFF") -> dict:
    return {"页": [{"页号": n, "框号": box, "颜色": color} for n in pages]}


# ---------------------------------------------------------------- 造书


def _full_bleed(text: str = SENT, image_id: str | None = "p001_v1") -> PresetPage:
    return PresetPage(text=text, preset="full-bleed-overlay", imageId=image_id)


def _free(text: str = SENT, image_id: str | None = "p002_v1", with_text: bool = True) -> FreePage:
    blocks = []
    if with_text:
        blocks.append(
            TextBlock(
                id="b_text",
                xMm=8,
                yMm=8,
                widthMm=60,
                heightMm=40,
                content=text,
                # 底衬 + 垂直居中：这两样是**版式**，智能排版不该碰它们。
                style=TextStyle(
                    paddingMm=4,
                    backgroundColor="#FFFFFF",
                    borderRadiusMm=3,
                    verticalAlign="middle",
                ),
            )
        )
    blocks.append(
        ImageBlock(
            id="b_img", xMm=0, yMm=0, widthMm=TRIM_W, heightMm=TRIM_H, imageId=image_id
        )
    )
    return FreePage(imageId=image_id, blocks=blocks)


def _book(pages) -> Book:
    return Book(title="智能排版", trimSize=TrimSize.from_id("A5"), bleedMm=3, pages=pages)


# ---------------------------------------------------------------- 选页


def test_applicable_pages_picks_and_skips_exactly_the_right_ones():
    """六种情况一次钉死。**精确列表**，不是"包含" —— 多配一页（比如把
    image-top 也配了）和少配一页一样是 bug，而"包含"断言两种都放行。"""
    book = _book(
        [
            _full_bleed(),  # 1 目标
            PresetPage(text=SENT, preset="image-top", imageId="p002_v1"),  # 2 版式不需要
            _full_bleed(text="   "),  # 3 没有文字
            _full_bleed(image_id=None),  # 4 还没有插图
            _free(),  # 5 目标
            _free(with_text=False),  # 6 没有文字块
            _free(image_id=None),  # 7 还没有插图
        ]
    )

    targets, skips = auto_layout.applicable_pages(book)

    assert [t.number for t in targets] == [1, 5]
    assert skips == [
        (2, "版式不需要"),
        (3, "没有文字"),
        (4, "还没有插图"),
        (6, "没有文字块"),
        (7, "还没有插图"),
    ]


def test_free_page_targets_the_largest_text_block():
    """自由页可能有副标题、页码。搬走小块比不搬更糟 —— 那些是用户精心摆的。"""
    page = _free()
    page.blocks.insert(
        0,
        TextBlock(id="b_small", xMm=0, yMm=0, widthMm=20, heightMm=10, content="副标题"),
    )
    book = _book([page])

    targets, _ = auto_layout.applicable_pages(book)

    assert [t.block_id for t in targets] == ["b_text"]


def test_free_page_uses_the_image_blocks_box_not_the_page():
    """插图块只有半页时，候选框要相对**那块图**算，不是相对整页。

    用整页算的话，候选框的 y 会按页高归一化，落到图片下面的空白里去 ——
    那儿没有图，"压在图上"这件事根本不成立。
    """
    page = _free()
    page.blocks[-1].yMm = 0.0
    page.blocks[-1].heightMm = 100.0  # 图片只占上半张
    book = _book([page])

    targets, _ = auto_layout.applicable_pages(book)

    assert targets[0].box == (0.0, 0.0, TRIM_W, 100.0)


def test_skip_summary_groups_by_reason():
    skips = [(1, "版式不需要"), (2, "版式不需要"), (3, "还没有插图")]
    assert auto_layout.skip_summary(skips) == "版式不需要 2 页，还没有插图 1 页"


# ---------------------------------------------------------------- 提示词


def test_prompt_shows_every_candidate_in_millimetres(candidates):
    candidates()
    book = _book([_full_bleed()])
    plan = auto_layout._plan("book_x", auto_layout.applicable_pages(book)[0][0])

    prompt = auto_layout.build_prompt([plan], TRIM_W, TRIM_H)

    # 候选 0 在 y=0.70 → 147mm（A5 高 210）
    assert "上 147 毫米" in prompt
    assert "1 号" in prompt and "2 号" in prompt
    assert "宽 133 毫米" in prompt  # 0.90 * 148 = 133.2 → 133


def test_prompt_only_sends_an_excerpt_of_the_text(candidates):
    """30 页的书每页发全文会到几十 KB，而模型只需要判断语气和篇幅感。"""
    candidates()
    long_text = SENT * 5
    book = _book([_full_bleed(text=long_text)])
    plan = auto_layout._plan("book_x", auto_layout.applicable_pages(book)[0][0])

    prompt = auto_layout.build_prompt([plan], TRIM_W, TRIM_H)

    assert SENT * 5 not in prompt
    assert long_text[: config.LAYOUT_TEXT_EXCERPT_CHARS] in prompt
    # 字数报的是**全文**的字数，不是节选的字数 —— 那是给模型判断篇幅感的
    assert f"正文 {len(long_text)} 个字" in prompt


def test_prompt_has_no_english_outside_the_json_example(candidates):
    """与 test_illustrate 的英文扫描对称。

    唯一的拉丁字母在最后那行 JSON 示例里（`#FFFFFF`），所以抠掉它再扫。
    """
    import re

    candidates()
    book = _book([_full_bleed()])
    plan = auto_layout._plan("book_x", auto_layout.applicable_pages(book)[0][0])

    prompt = auto_layout.build_prompt([plan], TRIM_W, TRIM_H)
    without_example = prompt.split("只输出一个 JSON 对象")[0]

    assert re.search(r"[A-Za-z]{2,}", without_example) is None, without_example


def test_prompt_numbers_candidates_from_one_on_every_page(candidates):
    """每页都从 1 号开始，而且写明了 —— 30 页连着编号时模型抄错两位数
    是**静默**的（落在合法范围内，看着像正常选择）。"""
    candidates()
    book = _book([_full_bleed(), _full_bleed()])
    targets, _ = auto_layout.applicable_pages(book)
    plans = [auto_layout._plan("book_x", t) for t in targets]

    prompt = auto_layout.build_prompt(plans, TRIM_W, TRIM_H)
    # 结尾那句说明里也有一个 "1 号"，只数正文部分
    body = prompt.split("请为每一页挑一个编号")[0]

    assert body.count("1 号") == 2, body
    assert "每一页的位置都从 1 号开始编号" in prompt


# ---------------------------------------------------------------- 解析


@pytest.mark.parametrize(
    "reply,expected",
    [
        ({"页": [{"页号": 1, "框号": 3, "颜色": "#FFFFFF"}]}, {1: (2, "#FFFFFF")}),
        # 英文别名：换个模型/版本就开始回 page/box 是常事，不该整本降级
        ({"pages": [{"page": 1, "box": 3, "color": "#fff"}]}, {1: (2, "#fff")}),
        # `index` 单用是页号（见下面那条拒绝用例）
        ({"results": [{"index": 1, "position": 2}]}, {1: (1, "")}),
        # 只有一页时模型可能不套外层
        ({"页号": 1, "框号": 2, "颜色": "#000000"}, {1: (1, "#000000")}),
        # 顶层直接是数组
        ([{"页号": 1, "框号": 1}], {1: (0, "")}),
        # "第 1 页" / "3 号" 这种带字的也认
        ({"页": [{"页号": "第 1 页", "框号": "3 号"}]}, {1: (2, "")}),
        # 大小写不敏感
        ({"页": [{"PageNumber": 1, "Box": 2, "Color": "#ABC"}]}, {1: (1, "#ABC")}),
        # 缺颜色：不整条丢掉，颜色交给算法
        ({"页": [{"页号": 1, "框号": 1}]}, {1: (0, "")}),
    ],
)
def test_parse_decisions_accepts_the_shapes_models_actually_send(reply, expected):
    got = auto_layout.parse_decisions(reply)
    assert {n: (d.index, d.color) for n, d in got.items()} == expected


@pytest.mark.parametrize(
    "reply",
    [
        None,
        "一段话",
        {},
        {"页": []},
        # 框号是 0 基的（模型少减了一次）→ 当越界，**不猜**
        {"页": [{"页号": 1, "框号": 0}]},
        # 缺框号
        {"页": [{"页号": 1}]},
        # 缺页号
        {"页": [{"框号": 1}]},
        # 条目不是对象
        {"页": ["1 号"]},
        # **`index` 是页号，不是框号**（_PAGE_KEYS 里有它、_BOX_KEYS 里没有）。
        # 这一行两个数字都在合法范围内，认错了会**静默**写错位置 —— 所以
        # 宁可认不出来丢掉、走算法兜底。这是一条设计决定，不是巧合。
        {"页": [{"page": 1, "index": 3}]},
    ],
)
def test_parse_decisions_drops_what_it_cannot_understand(reply):
    assert auto_layout.parse_decisions(reply) == {}


# ---------------------------------------------------------------- 落笔


async def _run(books_dir, book, candidates_fixture, model_fixture, *, reply=None, **kw):
    """建书 → 跑一次任务 → 返回 (job, 重新读回来的书)。"""
    book_store.create(book)
    numbers = [t.number for t in auto_layout.applicable_pages(book)[0]]
    job = await job_manager.create("layout", book.id, numbers)
    _, script = model_fixture
    if reply is not None:
        script["reply"] = reply
    await auto_layout.run_layout_job(job, book.id)
    return job, book_store.load(book.id)


async def test_job_writes_the_model_choice_into_the_preset_page(books_dir, candidates, model):
    candidates()
    book = _book([_full_bleed()])

    job, fresh = await _run(
        books_dir, book, candidates, model, reply=_reply_for([1], box=1, color="#FFFFFF")
    )

    assert job.status == "done"
    options = fresh.pages[0].options
    assert options.overlayRect is not None
    assert options.overlayRect.yMm == pytest.approx(0.70 * TRIM_H)  # 下半张
    assert options.overlayRect.widthMm == pytest.approx(0.90 * TRIM_W)
    # 字号/行距是算出来的，不是模型给的
    assert options.fontSizePt > 0
    assert 1.3 <= options.lineHeight <= 1.7
    # 字体按用户的"默认楷体"来
    assert options.fontFamily == config.DEFAULT_FONT_FAMILY


async def test_job_takes_the_second_candidate_when_the_model_says_two(books_dir, candidates, model):
    """模型的**选择**必须真的被用上 —— 只测"写了个矩形"的话，永远用第一个
    候选也能过。"""
    candidates()
    book = _book([_full_bleed()])

    _, fresh = await _run(
        books_dir, book, candidates, model, reply=_reply_for([1], box=2)
    )

    assert fresh.pages[0].options.overlayRect.yMm == pytest.approx(0.05 * TRIM_H)  # 上半张


async def test_job_falls_back_to_the_algorithm_when_the_model_says_nothing(
    books_dir, candidates, model
):
    candidates()
    book = _book([_full_bleed()])

    job, fresh = await _run(books_dir, book, candidates, model, reply=None)

    assert job.status == "done"
    # 算法第一选择 = 分数最低的那个 = 下半张
    assert fresh.pages[0].options.overlayRect.yMm == pytest.approx(0.70 * TRIM_H)
    assert job.items[0].error == "模型未响应，已按算法最优位置放置"


async def test_job_rejects_an_out_of_range_box_without_clamping(
    books_dir, candidates, model
):
    """越界说明模型在瞎猜。clamp 到最后一个候选只会把文字贴到页角上，
    还不如退回算法的最优选择。"""
    candidates()
    book = _book([_full_bleed()])

    job, fresh = await _run(
        books_dir, book, candidates, model, reply=_reply_for([1], box=99)
    )

    assert fresh.pages[0].options.overlayRect.yMm == pytest.approx(0.70 * TRIM_H)
    assert "越界" in job.items[0].error


async def test_job_degrades_to_one_call_per_page_when_the_batch_loses_a_page(
    books_dir, candidates, model
):
    """批量少给一页 → 逐页问。不降级的话，那一页会静默退回算法位置，用户
    看到的是"大部分页智能配了、零星几页是老的横带"，归因不到模型身上。"""
    candidates()
    book = _book([_full_bleed(), _full_bleed()])
    seen, script = model
    script["replies"] = [
        _reply_for([1]),  # 批量：少给了第 2 页
        _reply_for([1]),  # 逐页第 1 页
        _reply_for([2], box=2),  # 逐页第 2 页
    ]

    job, fresh = await _run(books_dir, book, candidates, model)

    assert len(seen) == 3, f"应当是一次批量 + 两次逐页，实际调了 {len(seen)} 次"
    assert fresh.pages[1].options.overlayRect.yMm == pytest.approx(0.05 * TRIM_H)
    assert job.status == "done"


async def test_job_writes_the_free_page_block_geometry(books_dir, candidates, model):
    candidates()
    book = _book([_free()])

    _, fresh = await _run(books_dir, book, candidates, model, reply=_reply_for([1]))

    block = next(b for b in fresh.pages[0].blocks if b.id == "b_text")
    assert block.yMm == pytest.approx(0.70 * TRIM_H)
    assert block.widthMm == pytest.approx(0.90 * TRIM_W)
    assert block.style.fontSizePt > 0
    assert block.style.fontFamily == config.DEFAULT_FONT_FAMILY
    # 底衬、圆角、垂直居中是**版式**，必须原样留着 —— 落笔时重建一个 TextStyle
    # 会把它们全部重置成默认值，满版页的白板就没了（那是用户看得见的变化）。
    assert block.style.paddingMm == 4
    assert block.style.backgroundColor == "#FFFFFF"
    assert block.style.borderRadiusMm == 3
    assert block.style.verticalAlign == "middle"


async def test_job_marks_unapplicable_pages_as_skipped(books_dir, candidates, model):
    """进度条要显示整本书。只统计目标页的话，"1/1 完成"而另外两页没动，
    用户会以为功能坏了。"""
    candidates()
    book = _book(
        [_full_bleed(), PresetPage(text=SENT, preset="image-top", imageId="x"), _full_bleed(image_id=None)]
    )
    book_store.create(book)
    job = await job_manager.create("layout", book.id, [1, 2, 3])
    _, script = model
    script["reply"] = _reply_for([1])

    await auto_layout.run_layout_job(job, book.id)

    assert job.completed == 1
    assert job.skipped == 2
    assert job.to_dict()["skipped"] == 2
    reasons = {item.pageNumber: item.error for item in job.items}
    assert reasons[2] == "版式不需要"
    assert reasons[3] == "还没有插图"
    assert "跳过 2 页" in job.message


async def test_job_keeps_going_when_there_is_no_api_key(books_dir, candidates, model, monkeypatch):
    """没配 key 是**合法的降级模式**，不是失败：纯算法路径本来就有意义，
    而且 e2e 和没 key 的机器都依赖它。"""
    monkeypatch.setattr(config, "API_KEY", "")
    candidates()
    book = _book([_full_bleed()])

    job, fresh = await _run(books_dir, book, candidates, model)

    assert job.status == "done"
    assert "未配置文本模型" in job.message
    assert fresh.pages[0].options.overlayRect is not None


async def test_job_leaves_the_page_alone_when_the_image_cannot_be_read(
    books_dir, monkeypatch, model
):
    """有 imageId 但文件读不出来（被删了/截断了）：跳过，不是失败。"""
    monkeypatch.setattr(
        auto_layout.layout_analysis, "blank_candidates", lambda *a, **k: []
    )
    book = _book([_full_bleed()])
    book_store.create(book)
    job = await job_manager.create("layout", book.id, [1])

    await auto_layout.run_layout_job(job, book.id)

    assert job.status == "done"
    assert job.skipped == 1
    assert book_store.load(book.id).pages[0].options.overlayRect is None


async def test_job_writes_nothing_at_all_when_cancelled(books_dir, candidates, model):
    """取消要**一个字节都不写** —— 没写就不需要撤销，也就不该留下快照。"""
    candidates()
    book = _book([_full_bleed()])
    book_store.create(book)
    job = await job_manager.create("layout", book.id, [1])
    job_manager.request_cancel(job)
    _, script = model
    script["reply"] = _reply_for([1])

    await auto_layout.run_layout_job(job, book.id)

    assert job.status == "cancelled"
    assert book_store.load(book.id).pages[0].options.overlayRect is None
    assert not paths.layout_undo_file(book.id).exists()


# ---------------------------------------------------------------- 撤销


async def _snapshot_pages(books_dir, book, candidates_fixture, model_fixture, reply):
    """跑一次任务，返回 (跑之前的 pages, 跑之后的书)。"""
    before = [p.model_dump(mode="json") for p in book.pages]
    job, after = await _run(books_dir, book, candidates_fixture, model_fixture, reply=reply)
    assert job.status == "done", job.message
    return before, after


async def test_undo_restores_the_pages_exactly(books_dir, candidates, model):
    candidates()
    book = _book([_full_bleed(), _free()])

    before, after = await _snapshot_pages(
        books_dir, book, candidates, model, _reply_for([1, 2])
    )
    assert [p.model_dump(mode="json") for p in after.pages] != before  # 真的改了

    restored, skipped = auto_layout.restore_snapshot(after, auto_layout.read_snapshot(book.id))

    assert (restored, skipped) == (2, 0)
    assert [p.model_dump(mode="json") for p in after.pages] == before


async def test_undo_does_not_resurrect_pages_the_user_deleted(books_dir, candidates, model):
    """**整体替换 `book.pages` 是绝对不行的**：用户跑完之后删了页、加了页、
    重新分页，整体替换会把它们全抹掉 —— 那是不可接受的数据丢失。"""
    candidates()
    book = _book([_full_bleed(), _full_bleed()])

    _, after = await _snapshot_pages(books_dir, book, candidates, model, _reply_for([1, 2]))
    after.pages.pop()  # 用户删掉了第 2 页
    after.pages.append(_full_bleed(text="新加的一页", image_id=None))

    restored, skipped = auto_layout.restore_snapshot(after, auto_layout.read_snapshot(book.id))

    # 1 恢复（还活着的那页）、1 跳过（新加的那页在快照里没有）
    assert (restored, skipped) == (1, 1)
    assert len(after.pages) == 2, "被删的页不该复活，新加的页不该消失"
    assert after.pages[1].text == "新加的一页"


async def test_undo_goes_back_to_the_state_before_the_most_recent_run(
    books_dir, candidates, model
):
    """跑一次 → 手动调一页 → 再跑一次 → 撤销，应当恢复"手动调过的那一页"，
    而不是抹掉它回到最初。按钮文案是「撤销**本次**智能配置」，语义就是如此。"""
    candidates()
    book = _book([_full_bleed()])
    book_store.create(book)
    _, script = model

    script["reply"] = _reply_for([1], box=1)
    job = await job_manager.create("layout", book.id, [1])
    await auto_layout.run_layout_job(job, book.id)

    # 用户手动改了一页
    manual = book_store.load(book.id)
    manual.pages[0].options.color = "#123456"
    book_store.save(manual)
    after_manual = [p.model_dump(mode="json") for p in book_store.load(book.id).pages]

    # 再跑一次
    script["reply"] = _reply_for([1], box=2)
    job = await job_manager.create("layout", book.id, [1])
    await auto_layout.run_layout_job(job, book.id)
    assert book_store.load(book.id).pages[0].options.color != "#123456"

    # 撤销 → 回到"第一次之后 + 手动调整"
    latest = book_store.load(book.id)
    auto_layout.restore_snapshot(latest, auto_layout.read_snapshot(book.id))
    assert [p.model_dump(mode="json") for p in latest.pages] == after_manual


async def test_snapshot_survives_a_process_restart(books_dir, candidates, model):
    """快照在磁盘上，不是内存里 —— 重启后撤销按钮还得能用。"""
    candidates()
    book = _book([_full_bleed()])
    await _snapshot_pages(books_dir, book, candidates, model, _reply_for([1]))

    raw = json.loads(paths.layout_undo_file(book.id).read_text("utf-8"))

    assert raw["pages"][0]["options"]["overlayRect"] is None  # 存的是**跑之前**的样子
    assert raw["pageCount"] == 1
    assert raw["createdAt"]


def test_snapshot_info_reports_availability(books_dir):
    book = _book([_full_bleed()])
    book_store.create(book)

    assert auto_layout.snapshot_info(book.id) == {
        "available": False,
        "createdAt": None,
        "pages": 0,
    }

    auto_layout.write_snapshot(book.id, book)
    info = auto_layout.snapshot_info(book.id)
    assert info["available"] is True and info["pages"] == 1


# ---------------------------------------------------------------- 端点


@pytest.fixture
def client(books_dir, monkeypatch):
    """TestClient，但**把任务体换成空操作**。

    `POST /layout/auto` 会 `asyncio.create_task` 把任务丢到后台，而 TestClient
    的事件循环在请求返回后还活着 —— 不挡住的话，任务会带着真 API key 发请求、
    并在测试断言期间偷偷改 book.json。端点测试要验的是"建了任务、返回了什么"，
    任务体本身由上面的用例直接 `await` 驱动，两件事分开测。
    """

    async def _noop(job, book_id):
        await job_manager.set_status(job, "done", "（测试里不真跑）")

    monkeypatch.setattr(auto_layout, "run_layout_job", _noop)
    with TestClient(app) as c:
        yield c


def test_auto_endpoint_creates_a_job_for_the_whole_book(client, candidates):
    candidates()
    book = _book([_full_bleed(), PresetPage(text=SENT, preset="image-top", imageId="x")])
    book_store.create(book)

    resp = client.post(f"/api/books/{book.id}/layout/auto")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # items 是**整本书**：跳过的那页也有格子，进度条上显示成灰点
    assert body["total"] == 2
    job = job_manager.get(body["jobId"])
    assert job is not None and job.kind == "layout"


def test_auto_endpoint_rejects_a_book_with_nothing_to_configure(client, candidates):
    candidates()
    book = _book([PresetPage(text=SENT, preset="image-top", imageId="x")])
    book_store.create(book)

    resp = client.post(f"/api/books/{book.id}/layout/auto")

    assert resp.status_code == 400
    assert "版式不需要 1 页" in resp.json()["detail"]


def test_preview_endpoint_reports_the_candidates(client, candidates):
    candidates()
    book = _book([_full_bleed()])
    book_store.create(book)

    body = client.get(f"/api/books/{book.id}/layout/preview/1").json()

    assert body["applicable"] is True
    assert len(body["candidates"]) == 2
    assert body["candidates"][0]["yMm"] == pytest.approx(147.0)
    assert body["candidates"][0]["fontSizePt"] > 0


def test_preview_endpoint_says_why_a_page_is_not_applicable(client, candidates):
    candidates()
    book = _book([PresetPage(text=SENT, preset="text-only", imageId=None)])
    book_store.create(book)

    body = client.get(f"/api/books/{book.id}/layout/preview/1").json()

    assert body == {"applicable": False, "reason": "版式不需要", "candidates": []}


def test_preview_endpoint_writes_nothing(client, candidates):
    """它免费、确定、可反复看，靠的就是不写不调模型。"""
    candidates()
    book = _book([_full_bleed()])
    book_store.create(book)
    before = paths.book_file(book.id).read_text("utf-8")

    client.get(f"/api/books/{book.id}/layout/preview/1")

    assert paths.book_file(book.id).read_text("utf-8") == before


def test_undo_endpoint_404s_without_a_snapshot(client):
    book = _book([_full_bleed()])
    book_store.create(book)

    assert client.get(f"/api/books/{book.id}/layout/undo").json() == {
        "available": False,
        "createdAt": None,
        "pages": 0,
    }
    resp = client.post(f"/api/books/{book.id}/layout/undo")
    assert resp.status_code == 404
    assert "只保留最近一次" in resp.json()["detail"]


def test_undo_endpoint_restores_and_then_disappears(client, candidates, model):
    """**一次**撤销，不是 redo 栈：第二次点必然 404，按钮此时也已经隐藏。"""
    candidates()
    book = _book([_full_bleed()])
    book_store.create(book)
    before = [p.model_dump(mode="json") for p in book.pages]

    auto_layout.write_snapshot(book.id, book)
    changed = book_store.load(book.id)
    changed.pages[0].options.color = "#FF0000"
    book_store.save(changed)

    resp = client.post(f"/api/books/{book.id}/layout/undo")
    assert resp.status_code == 200, resp.text
    assert resp.json()["restored"] == 1
    assert [p.model_dump(mode="json") for p in book_store.load(book.id).pages] == before

    assert client.get(f"/api/books/{book.id}/layout/undo").json()["available"] is False
    assert client.post(f"/api/books/{book.id}/layout/undo").status_code == 404


async def test_undo_endpoint_conflicts_with_a_running_job(client, candidates):
    """撤销和任务同时写同一本书 → 用户看到"撤销了但只撤销了一半"。"""
    candidates()
    book = _book([_full_bleed()])
    book_store.create(book)
    auto_layout.write_snapshot(book.id, book)
    await job_manager.create("layout", book.id, [1])  # 还没跑完

    resp = client.post(f"/api/books/{book.id}/layout/undo")

    assert resp.status_code == 409
    assert paths.layout_undo_file(book.id).exists(), "被挡住时不该把快照吃掉"


async def test_undo_endpoint_ignores_a_finished_job(client, candidates):
    """只有**未终态**的 job 才挡人。跑完的 job 还留在表里是正常的，
    把它也当成"正在运行"的话，用户跑完一次就再也撤销不了了。"""
    candidates()
    book = _book([_full_bleed()])
    book_store.create(book)
    auto_layout.write_snapshot(book.id, book)

    job = await job_manager.create("layout", book.id, [1])
    await job_manager.set_status(job, "done", "完事了")

    assert client.post(f"/api/books/{book.id}/layout/undo").status_code == 200
