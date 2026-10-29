"""端到端验证自由拖拽编辑器（Phase 6）。

不是单元测试：真的开一个 Chromium，点真实按钮、拖真实鼠标，再回读
后端落盘的数据。单测能证明"块的数据结构对"，证明不了"拖一下真的动了"
—— 覆盖层与 iframe 的坐标系、指针事件的接管、缩放折算，这些只有跑起来
才知道。

用法（两个服务都要在跑）：
    uv run python scripts/e2e_editor.py
"""

import asyncio
import os
import sys

import httpx

# 用 localhost 而不是 127.0.0.1：vite 默认只监听 ::1，写 IPv4 会 connection refused
API = os.getenv("MSB_API", "http://127.0.0.1:8000/api")
UI = os.getenv("MSB_UI", "http://localhost:5173")
STORY = (
    "小兔子推开木门，外面是一片银白的雪地。\n\n"
    "它踩出第一行脚印，回头看了看，又继续往前走。\n\n"
    "雪越下越大，远处亮起一盏橘色的灯。"
)


def fail(msg: str) -> None:
    print(f"✗ {msg}")
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"✓ {msg}")


def prepare_book(pages_wanted: int = 3, title: str = "编辑器 E2E") -> str:
    """建书 + 写故事 + 分页，全部走真实接口。

    STORY 只够分 3 页，要更多页就把正文重复几遍 —— 分页是按长度切的，
    正文不够长时给多少页都是白搭。
    """
    # 向上取整：pages_wanted=3 时必须还是**一份** STORY，多重复一遍会改变
    # 每页的字数，上面那些排版断言就跟着漂了
    story = STORY * ((pages_wanted + 2) // 3)
    with httpx.Client(base_url=API, timeout=30) as c:
        book = c.post(
            "/books", json={"title": title, "trimSizeId": "A5", "pageCount": pages_wanted}
        ).json()
        book_id = book["id"]
        c.put(f"/books/{book_id}/story", json={"text": story})
        resp = c.post(
            f"/books/{book_id}/paginate", json={"pageCount": pages_wanted, "preset": "image-top"}
        )
        resp.raise_for_status()
        pages = resp.json()["pages"]
        if len(pages) != pages_wanted:
            fail(f"分页结果应 {pages_wanted} 页，实际 {len(pages)}")
    return book_id


def get_book(book_id: str) -> dict:
    with httpx.Client(base_url=API, timeout=30) as c:
        return c.get(f"/books/{book_id}").json()


def get_page(book_id: str, number: int) -> dict:
    return get_book(book_id)["pages"][number - 1]


def get_default_preset() -> str:
    """后端认定的默认版式。界面上那个初始值必须跟它一致。"""
    with httpx.Client(base_url=API, timeout=30) as c:
        return c.get("/meta/defaults").json()["preset"]


def preset_label(preset_id: str) -> str:
    with httpx.Client(base_url=API, timeout=30) as c:
        for item in c.get("/meta/presets").json():
            if item["id"] == preset_id:
                return item["label"]
    fail(f"/meta/presets 里没有 {preset_id}")


async def drag_slider(page, field, fraction: float) -> str:
    """把某个 .field 里的滑块拖到跑道 fraction 处，返回输入框显示的文本。

    必须用真实鼠标拖：el-slider 的毛病只在**松手那一刻**现形 —— 拖动中内部值
    是对的，松手时它用 props.modelValue 把自己重置回去。用 JS 改值、或者只
    点一下跑道，都会绕过这条路径而测不出来。
    """
    # 先滚进视口。属性面板很长，"字号"这类字段常在折叠线以下 ——
    # bounding_box() 照样返回坐标（视口外的 y），但鼠标拖到那个位置什么也碰不到，
    # 于是测试会安静地"拖了个寂寞"：界面不动、PATCH 不发，断言只说"后端还是旧值"。
    await field.scroll_into_view_if_needed()
    runway = await field.locator(".el-slider__runway").bounding_box()
    btn = await field.locator(".el-slider__button").bounding_box()
    if not runway or not btn:
        fail("取不到滑块的位置")
    cx = btn["x"] + btn["width"] / 2
    cy = btn["y"] + btn["height"] / 2
    target_x = runway["x"] + runway["width"] * fraction
    await page.mouse.move(cx, cy)
    await page.mouse.down()
    # 分几步移动：一步到位会绕过 pointermove 的节流路径
    for i in range(1, 6):
        await page.mouse.move(cx + (target_x - cx) * i / 5, cy)
    await page.mouse.up()
    await page.wait_for_timeout(900)  # 等 PATCH 落盘
    return await field.locator(".el-slider__input input").input_value()


async def check_default_preset(p, errors: list[str]) -> None:
    """还没分过页的书，版式下拉的初始值必须是后端那个默认版式。

    只有"一页都还没有"的书才看得出这个值 —— 已经有页面的书会按第 1 页的
    preset 回填，测的是另一条路径。前端把它写死成某个版式名的话，
    改 config.DEFAULT_PRESET 界面上不会有任何变化（用户点「分页」发出去的
    正是界面上这个值），所以这条值得单独钉一下。
    """
    with httpx.Client(base_url=API, timeout=30) as c:
        book_id = c.post(
            "/books", json={"title": "默认版式 E2E", "trimSizeId": "A5", "pageCount": 3}
        ).json()["id"]
        c.put(f"/books/{book_id}/story", json={"text": STORY})

    page = await p.new_page(viewport={"width": 1600, "height": 1000})
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        await page.goto(f"{UI}/books/{book_id}", wait_until="networkidle")
        await page.get_by_text("故事", exact=True).click()
        await page.wait_for_selector(".controls .el-select", timeout=15000)

        shown = (await page.locator(".controls .el-select").inner_text()).strip()
        expected = preset_label(get_default_preset())
        if shown != expected:
            fail(f"版式下拉初始值是「{shown}」，应为后端默认的「{expected}」")
        ok(f"没分过页的书，版式默认选中「{shown}」")
    finally:
        await page.close()


async def check_story_tab(p, errors: list[str]) -> None:
    """故事步骤：左窄右宽的分栏、整页不滚、页面列表内部滚、批量删除删对页。

    跑真浏览器的理由和以前一样：批量删除删的是不是**勾中的那几页**只有回读
    后端才知道（页码在删除过程中会整体前移，正序删会删错页，而页数照样对得上）。
    另外三条是这次重构的核心不变量，也只有真量 bounding box 才证明得了 ——
    单看代码看不出 `.story` 和 `.body` 的底边差了 40px，也看不出某天有人给
    .app-shell 加了 transform 把 fixed 的弹窗搞坏。
    """
    # 12 页：右栏两列就是 6 行，一定超过视口可用高度，"列表内部滚"这条
    # 断言才是确定性的（6 页的话可能刚好装得下，断言会随机挂）
    book_id = prepare_book(12, "故事页 E2E")
    page = await p.new_page(viewport={"width": 1600, "height": 1000})
    page.on("pageerror", lambda e: errors.append(str(e)))

    await page.goto(f"{UI}/books/{book_id}", wait_until="networkidle")
    # 打开书落在「设置」步骤，故事页要自己点进去
    await page.get_by_text("故事", exact=True).click()
    await page.wait_for_selector(".page-row", timeout=15000)
    await page.wait_for_timeout(400)

    # ---- 左窄右宽，左栏被 clamp 住
    left = await page.locator(".text-col").bounding_box()
    right = await page.locator(".work-col").bounding_box()
    if left["x"] + left["width"] > right["x"] + 1:
        fail("左右栏重叠 —— 不是左窄右宽的分栏")
    if not 379 <= left["width"] <= 621:
        fail(f"左栏宽 {left['width']:.0f}px，超出 clamp(380px, 34%, 620px)")
    if right["width"] <= left["width"]:
        fail(f"右栏 {right['width']:.0f}px 不比左栏 {left['width']:.0f}px 宽 —— 没改成左窄右宽")
    if await page.locator(".text-col textarea").count() != 1:
        fail("故事原文不在左栏里")
    ok(f"左窄右宽：左栏 {left['width']:.0f}px / 右栏 {right['width']:.0f}px")

    # ---- 宽屏下左栏不再变宽（clamp 的上限生效）
    await page.set_viewport_size({"width": 2600, "height": 1000})
    await page.wait_for_timeout(400)
    wide = await page.locator(".text-col").bounding_box()
    if wide["width"] > 621:
        fail(f"2600px 宽屏下左栏涨到了 {wide['width']:.0f}px —— clamp 的上限没生效")
    ok(f"2600px 宽屏下左栏仍是 {wide['width']:.0f}px（封顶 620）")
    await page.set_viewport_size({"width": 1600, "height": 1000})
    await page.wait_for_timeout(400)

    # ---- 预览已经删掉了（产品决定：右栏改成放分页配置和页面列表）。
    # 钉住它，免得哪天又被加回来。
    if await page.locator(".story iframe").count():
        fail("故事页又出现了 iframe 预览")

    # ---- 整页不滚 + 列表自己滚 + .story 精确铺满 .body 的内边距盒
    state = await page.evaluate(
        """() => {
          const doc = document.scrollingElement;
          const body = document.querySelector('.body');
          const story = document.querySelector('.story');
          const list = document.querySelector('.list-card .el-card__body');
          const before = list.scrollTop;
          list.scrollTop = 99999;
          return {
            docScrolls: doc.scrollHeight > doc.clientHeight + 1,
            bodyScrolls: body.scrollHeight > body.clientHeight + 1,
            gap: Math.round(body.getBoundingClientRect().bottom
                            - story.getBoundingClientRect().bottom),
            listScrolls: list.scrollHeight > list.clientHeight + 1,
            listMoved: list.scrollTop > before,
          };
        }"""
    )
    if state["docScrolls"] or state["bodyScrolls"]:
        fail(f"故事页整页在滚 —— 固定高度的链条没接上：{state}")
    # gap 必须是 0。负 margin 靠 flex-grow 补偿，写成 height: 100% 的话
    # 百分比解析的是内容盒，这里会是 40。
    if state["gap"] != 0:
        fail(
            f".story 没铺满 .body 的内边距盒，底部差 {state['gap']}px"
            "（.body 是不是忘了 display: flex？.story 是不是写成了 height: 100%？）"
        )
    if not state["listScrolls"] or not state["listMoved"]:
        fail(f"页面列表没有内部滚动 —— 卡片区没接住剩余高度：{state}")
    ok("整页不滚，滚动只发生在页面列表内部，.story 与 .body 底边对齐")

    # ---- 批量删除：勾第 2、4 页
    total_before = len(get_book(book_id)["pages"])
    keep_ids = [
        p_["id"]
        for i, p_ in enumerate(get_book(book_id)["pages"], 1)
        if i not in (2, 4)
    ]

    for index in (1, 3):
        await page.locator(".page-row").nth(index).locator(".el-checkbox").click()
    await page.wait_for_timeout(300)

    button = page.get_by_role("button", name="删除选中 (2)")
    if await button.count() != 1:
        fail("勾了两页，却没出现「删除选中 (2)」按钮")
    await button.click()
    # 必须限定在确认框里：每一页卡片上也有一个「删除」按钮，直接按名字找会
    # 命中 12 个元素（strict mode 直接报错）
    await page.locator(".el-message-box__btns").get_by_role("button", name="删除").click()
    await page.wait_for_timeout(1200)

    pages_after = get_book(book_id)["pages"]
    if len(pages_after) != total_before - 2:
        fail(f"应该剩 {total_before - 2} 页，实际 {len(pages_after)} 页")
    # 这条才是重点：删掉的必须是勾中的那两页。正序删的话页数也对，但删错页。
    if [p_["id"] for p_ in pages_after] != keep_ids:
        fail("批量删除删错了页 —— 剩下的页面 id 与预期不符（多半是没倒序删）")

    # ---- 删完之后列表按**服务端状态**重渲染：行数、连续页码、文字都对得上。
    # （以前这条是查预览 iframe 请求的是第几页，预览没了，改成查列表本身 ——
    #  一样能抓到"删完还停在旧状态"的问题。）
    rows = page.locator(".page-row")
    if await rows.count() != len(pages_after):
        fail(f"列表有 {await rows.count()} 行，服务端是 {len(pages_after)} 页")
    numbers = await page.locator(".page-no").all_inner_texts()
    want = [f"第 {i} 页" for i in range(1, len(pages_after) + 1)]
    if numbers != want:
        fail(f"删除后页码没有连续重排：{numbers}")
    last_text = (await rows.nth(len(pages_after) - 1).locator("textarea").input_value()).strip()
    if last_text != pages_after[-1]["text"].strip():
        fail("最后一页的文本与服务端不一致 —— 删除后没有按服务端状态重渲染")
    ok(f"批量删除删掉的是勾中的第 2、4 页，剩 {len(pages_after)} 页且列表已重排")

    await page.close()


async def check_book_list(p, errors: list[str]) -> None:
    """列表页：前端搜索 + 分页 + 卡片区内部滚 + 整页不滚。

    这块以前是零覆盖 —— 所有 e2e 都直接 goto("/books/{id}")，没有一个访问 "/"。
    而搜索和分页全是前端逻辑，项目里没有前端单测设施（package.json 里没有
    vitest），所以只能在这儿钉。

    先搜索再验分页：数据目录里本来就有一堆别的书（历次 e2e 留下的），
    不先过滤的话"翻到第 2 页剩几张"根本算不出来。
    """
    prefix = "E2E列表"
    with httpx.Client(base_url=API, timeout=30) as c:
        ids = [
            c.post("/books", json={"title": f"{prefix}{i:02d}"}).json()["id"] for i in range(13)
        ]
    try:
        # 视口高度是刻意压到 420 的：默认 12 张卡在 1600px 宽下排 4 列 3 行，
        # 而这几本测试书的开本是默认的 SQ200（方形），缩略图 96×96，卡片约 130px，
        # 三行 ~420px。420 的视口留给卡片区的只有 ~230px，无论开本怎么变都装不下
        # —— "卡片区内部滚"这条断言才是确定性的。用 900px 的话三行装得下，
        # 断言会随机挂。也不能再矮：低于 ~250px 会先触发 .page 自己的兜底滚动，
        # 卡片区反而拿到 min-height 就不再滚了。
        page = await p.new_page(viewport={"width": 1600, "height": 420})
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(f"{UI}/", wait_until="networkidle")
        # 必须等卡片真的渲染出来再量。listBooks() 要把数据目录里每一本书都读盘
        # 反序列化一遍，历次 e2e 攒下的书多起来之后这个空窗期是肉眼可见的 ——
        # 停在 el-skeleton 上量的话 scrollHeight == clientHeight，断言会假红。
        await page.wait_for_selector(".card", timeout=30000)
        await page.wait_for_timeout(300)

        # ---- 整页不滚，滚动只发生在卡片区
        state = await page.evaluate(
            """() => {
              const doc = document.scrollingElement;
              const grid = document.querySelector('.grid-area');
              const before = grid.scrollTop;
              grid.scrollTop = 99999;
              return {
                docScrolls: doc.scrollHeight > doc.clientHeight + 1,
                gridScrolls: grid.scrollHeight > grid.clientHeight + 1,
                gridMoved: grid.scrollTop > before,
                sizes: grid.scrollHeight + '/' + grid.clientHeight,
                cards: document.querySelectorAll('.card').length,
                viewport: window.innerWidth + 'x' + window.innerHeight,
              };
            }"""
        )
        if state["docScrolls"]:
            fail(f"列表页整页在滚 —— 卡片区没接住剩余高度：{state}")
        if not state["gridScrolls"] or not state["gridMoved"]:
            fail(f"卡片区没有内部滚动：{state}")
        ok("列表页整页不滚，滚动只发生在卡片区内部")

        # ---- 搜索：13 本都命中，默认 12 条/页 → 第 1 页 12 张
        await page.locator(".search input").fill(prefix)
        await page.wait_for_timeout(400)
        if await page.locator(".card").count() != 12:
            fail(f"默认每页应 12 张，实际 {await page.locator('.card').count()} 张")
        ok("搜索命中 13 本，第 1 页显示 12 张")

        # ---- 翻到第 2 页：剩 1 张，且卡片区滚动位置归零
        # （不归零的话，从滚到底的第 1 页翻过来会看到一片空白）
        await page.locator(".el-pagination .btn-next").click()
        await page.wait_for_timeout(400)
        count = await page.locator(".card").count()
        if count != 1:
            fail(f"翻到第 2 页应剩 1 张，实际 {count} 张")
        top = await page.evaluate("() => document.querySelector('.grid-area').scrollTop")
        if top != 0:
            fail(f"翻页后卡片区没回到顶部（scrollTop={top}）—— 第 2 页会看着像空白")
        ok("翻到第 2 页只剩 1 张，且卡片区滚动位置归零")

        # ---- 切成 24 条/页：13 本一页装下，页码回到第 1 页
        await page.locator(".el-pagination .el-select").click()
        await page.wait_for_timeout(300)
        await page.get_by_role("option", name="24条/页").click()
        await page.wait_for_timeout(400)
        count = await page.locator(".card").count()
        if count != 13:
            fail(f"切到 24 条/页后应显示全部 13 本，实际 {count} 张")
        ok("切到 24 条/页后 13 本一页装下")

        # ---- 搜不到时：0 张卡 + 明确的空状态（不能是"还没有绘本"那种，
        #      那会让用户以为书被删了）
        await page.locator(".search input").fill("绝不存在的书名xyz")
        await page.wait_for_timeout(400)
        if await page.locator(".card").count() != 0:
            fail("搜不到时不该有卡片")
        if not await page.get_by_text("没有匹配").count():
            fail("搜不到时没有「没有匹配」的空状态提示")
        ok("搜不到时有 0 张卡和专门的空状态")

        await page.close()
    finally:
        with httpx.Client(base_url=API, timeout=30) as c:
            for book_id in ids:
                c.delete(f"/books/{book_id}")


async def main() -> None:
    from playwright.async_api import async_playwright

    book_id = prepare_book()
    ok(f"建书并分页成功: {book_id}")

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1600, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        await page.goto(f"{UI}/books/{book_id}", wait_until="networkidle")

        # ---- 进排版步骤
        await page.get_by_text("排版", exact=True).click()
        await page.wait_for_selector(".rail-item", timeout=15000)
        ok("排版步骤已打开")

        # ---- 预设页的文字样式面板：拖字号
        # 用户报的"设置按钮不生效"就出在这里。el-slider 松手时 emit 的 change
        # 带的是 props.modelValue（旧值），拖动中数字明明在变、一松手弹回原值，
        # 提交上去的也是原值。所以不能只看"界面点得动"，要回读后端核对。
        before_font = get_page(book_id, 1)["options"]["fontSizePt"]
        shown = await drag_slider(
            page, page.locator(".side .field").filter(has_text="字号"), 0.8
        )
        after_font = get_page(book_id, 1)["options"]["fontSizePt"]
        if after_font == before_font:
            fail(f"拖了字号滑块，后端还是 {before_font} —— 新值根本没提交上去")
        # 界面显示的和落盘的必须一致，否则就是"松手弹回旧值"
        if abs(float(shown) - after_font) > 0.51:
            fail(f"滑块显示 {shown}，后端存的却是 {after_font} —— 松手后弹回了旧值")
        ok(f"预设页字号滑块生效：{before_font} → {after_font}（界面显示 {shown}）")

        # 顺带验一下同面板的另一个滑块，别只修了字号那一个
        before_pad = get_page(book_id, 1)["options"]["paddingMm"]
        await drag_slider(page, page.locator(".side .field").filter(has_text="页边距"), 0.7)
        after_pad = get_page(book_id, 1)["options"]["paddingMm"]
        if after_pad == before_pad:
            fail(f"拖了页边距滑块，后端还是 {before_pad}")
        ok(f"页边距滑块生效：{before_pad} → {after_pad}")

        # ---- 预设页上直接拖动：自动转自由拖拽，并把这一下接住
        #
        # 这里要验的是整条链路：拖动越过阈值 → 页面展开 → 手势被接过去 →
        # 落点对得上 → 抓到的块被选中（八个缩放手柄出现，也就是"放大缩小"）。
        # 任何一环断了，现象都是"拖不动"。
        #
        # 基线用第 3 页取：它和第 2 页的 preset、options 完全一样，而展开几何
        # 只由这两者决定（见 presets.py 的 IMAGE_ROW_RATIO），与文本长度无关。
        trim_w = get_book(book_id)["trimSize"]["widthMm"]
        with httpx.Client(base_url=API, timeout=30) as c:
            baseline = c.post(f"/books/{book_id}/pages/3/to-free").json()["page"]["blocks"]
            c.post(f"/books/{book_id}/pages/3/to-preset", json={"preset": "image-top"})

        await page.locator(".rail-item").nth(1).click()
        await page.wait_for_selector(".block-layer.can-grab", timeout=10000)
        if get_page(book_id, 2)["mode"] != "preset":
            fail("第 2 页本该是预设页")

        layer_box = await page.locator(".block-layer").bounding_box()
        if layer_box is None:
            fail("取不到覆盖层的位置")
        # 覆盖层的宽度（CSS px）代表 trim_w 毫米 —— 换算比例直接从它来，
        # 不用去猜当前 zoom 是多少
        mm_per_px = trim_w / layer_box["width"]

        # 只按下、抬起来，不移动：**不许**把页面转成自由拖拽。阈值就是为
        # "点一下看看"设的，否则用户想选中点什么都会顺手改掉页面的模式。
        click_x = layer_box["x"] + layer_box["width"] / 2
        click_y = layer_box["y"] + layer_box["height"] * 0.75
        await page.mouse.move(click_x, click_y)
        await page.mouse.down()
        await page.mouse.move(click_x + 1, click_y)  # 1px，在阈值内
        await page.mouse.up()
        await page.wait_for_timeout(600)
        if get_page(book_id, 2)["mode"] != "preset":
            fail("只是点了一下，页面就被转成自由拖拽了 —— 拖动阈值没生效")
        ok("预设页上单击不会误触发转换")

        # 真的拖。image-top 的文字槽在下半页（页高的 55% 往下），起点取页高 75%
        # 处，落在文字块中间。
        drag_x, drag_y = -120.0, -60.0
        await page.mouse.move(click_x, click_y)
        await page.mouse.down()
        for i in range(1, 6):
            await page.mouse.move(
                click_x + drag_x * i / 5, click_y + drag_y * i / 5
            )
        await page.mouse.up()
        await page.wait_for_timeout(1200)  # 转换 + PATCH 都要落盘

        page2 = get_page(book_id, 2)
        if page2["mode"] != "free":
            fail("在预设页上拖动之后，页面没有自动转成自由拖拽")
        ok("预设页上拖动后自动转为自由拖拽")

        # 起手抓的必须是文字块 —— 落在文字槽里却抓走了图片块，说明命中判定错了
        moved = next(b for b in page2["blocks"] if b["type"] == "text")
        base = next(b for b in baseline if b["type"] == "text")
        dx = moved["xMm"] - base["xMm"]
        dy = moved["yMm"] - base["yMm"]
        want_dx = drag_x * mm_per_px
        want_dy = drag_y * mm_per_px
        if abs(dx - want_dx) > 1.0 or abs(dy - want_dy) > 1.0:
            fail(
                f"文字块没落在鼠标的位置：期望 ({want_dx:.1f}, {want_dy:.1f}) mm，"
                f"实际 ({dx:.1f}, {dy:.1f}) mm"
            )
        ok(f"这一下被接住了：文字块移动 ({dx:.1f}, {dy:.1f}) mm，与鼠标位移相符")

        # 图片块没被碰到 —— 抓错了块的话它会跟着动
        img_now = next(b for b in page2["blocks"] if b["type"] == "image")
        img_base = next(b for b in baseline if b["type"] == "image")
        if (img_now["xMm"], img_now["yMm"]) != (img_base["xMm"], img_base["yMm"]):
            fail("拖动文字时图片块也动了 —— 命中的不是起手抓到的那一块")

        # 抓到的块要处于选中态，八个缩放手柄才会出现。用户要的"放大缩小"
        # 就落在这里：转换完不用再点一下，手柄直接在手上。
        if await page.locator(".block-layer .block.selected").count() != 1:
            fail("拖动后没有选中被抓的块 —— 缩放手柄不会出现")
        handles = await page.locator(".block-layer .handle").count()
        if handles != 8:
            fail(f"选中了块却只有 {handles} 个缩放手柄，没法放大缩小")
        ok("被抓的块已选中，八个缩放手柄就位")

        # ---- 转自由拖拽
        await page.locator(".rail-item").nth(0).click()
        await page.wait_for_timeout(300)

        before = get_page(book_id, 1)
        assert before["mode"] == "preset", before["mode"]

        await page.get_by_role("button", name="转为自由拖拽").click()
        await page.wait_for_selector(".block-layer .block", timeout=15000)

        blocks = get_page(book_id, 1)["blocks"]
        ok(f"已转为自由页，展开出 {len(blocks)} 个块")

        # 覆盖层渲染出的块数要和后端数据一致 —— 不一致说明坐标层没接上
        rendered = await page.locator(".block-layer .block").count()
        if rendered != len(blocks):
            fail(f"覆盖层块数 {rendered} != 数据里的 {len(blocks)}")

        # ---- 拖动文字块
        text_block = next(b for b in blocks if b["type"] == "text")
        target = page.locator(f'.block[data-block-id="{text_block["id"]}"]')
        box = await target.bounding_box()
        if box is None:
            fail("取不到文字块的位置")

        start_x = box["x"] + box["width"] / 2
        start_y = box["y"] + box["height"] / 2
        await page.mouse.move(start_x, start_y)
        await page.mouse.down()
        # 分几步移动：一步到位会绕过 pointermove 的节流路径
        for step in range(1, 6):
            await page.mouse.move(start_x - 6 * step, start_y - 4 * step)
        await page.mouse.up()
        await page.wait_for_timeout(800)  # 等 PATCH 落盘

        after = next(b for b in get_page(book_id, 1)["blocks"] if b["id"] == text_block["id"])
        dx = after["xMm"] - text_block["xMm"]
        dy = after["yMm"] - text_block["yMm"]
        if dx == 0 and dy == 0:
            fail("拖动后坐标没变 —— 覆盖层没收到指针事件，或 PATCH 没发出去")
        if dx > 0 or dy > 0:
            fail(f"拖动方向反了：鼠标往左上拖，块却移动了 ({dx}, {dy}) mm")
        ok(f"拖动生效：文字块移动了 ({dx}, {dy}) mm")

        # ---- 撤销
        await page.keyboard.press("Control+z")
        await page.wait_for_timeout(800)
        undone = next(b for b in get_page(book_id, 1)["blocks"] if b["id"] == text_block["id"])
        if (undone["xMm"], undone["yMm"]) != (text_block["xMm"], text_block["yMm"]):
            fail(f"撤销没回到原位：{undone['xMm']},{undone['yMm']}")
        ok("Ctrl+Z 撤销回原位")

        # ---- 重做
        await page.keyboard.press("Control+Shift+z")
        await page.wait_for_timeout(800)
        redone = next(b for b in get_page(book_id, 1)["blocks"] if b["id"] == text_block["id"])
        if (redone["xMm"], redone["yMm"]) != (after["xMm"], after["yMm"]):
            fail("重做没回到拖动后的位置")
        ok("Ctrl+Shift+Z 重做")

        # ---- 加块
        count_before = len(get_page(book_id, 1)["blocks"])
        await page.get_by_role("button", name="加文字块").click()
        await page.wait_for_timeout(800)
        count_after = len(get_page(book_id, 1)["blocks"])
        if count_after != count_before + 1:
            fail(f"加块失败：{count_before} → {count_after}")
        ok("加文字块成功，且自动选中（右侧出现属性面板）")
        if await page.locator(".inspector").count() == 0:
            fail("新块没有自动选中，属性面板没出现")

        # ---- 自由块的属性面板：拖字号（同一个 el-slider 毛病，另一处面板）
        new_block = get_page(book_id, 1)["blocks"][-1]
        before_size = new_block["style"]["fontSizePt"]
        shown = await drag_slider(
            page, page.locator(".inspector .field").filter(has_text="字号（pt）"), 0.75
        )
        new_block = get_page(book_id, 1)["blocks"][-1]
        after_size = new_block["style"]["fontSizePt"]
        if after_size == before_size:
            fail(f"拖了块字号滑块，后端还是 {before_size}")
        if abs(float(shown) - after_size) > 0.51:
            fail(f"滑块显示 {shown}，后端存的却是 {after_size}")
        # 手动调过字号就该关掉自动缩字，否则用户调好的会被 fit-text 改回去
        if new_block["style"]["autoFit"]:
            fail("手动改了字号，autoFit 却没关 —— 字号会被自动缩字覆盖")
        ok(f"自由块字号滑块生效：{before_size} → {after_size}（界面显示 {shown}，autoFit 已关）")

        # ---- 预览 iframe 真的加载了（不是白屏）
        frame = page.frame_locator(".frame")
        await frame.locator(".page").wait_for(timeout=10000)
        ok("预览 iframe 渲染出了 .page")

        # ---- 转回预设
        # ElMessageBox 是 DOM 弹窗，不是浏览器原生 dialog —— 没有 dialog 事件，
        # 得自己去点那个按钮
        await page.get_by_role("button", name="转回预设").click()
        await page.get_by_role("button", name="丢弃并转回").click()
        await page.wait_for_timeout(1200)
        # 落到哪个版式也要对：自由页没有 preset 字段，转回哪个只能取默认值，
        # 而这个默认值前端是从 /meta/defaults 拿的。只断言 mode 的话，
        # 前端写死一个过时的版式名照样能过。
        expected = get_default_preset()
        back = get_page(book_id, 1)
        if back["mode"] != "preset":
            fail("转回预设失败")
        if back["preset"] != expected:
            fail(f"转回预设的版式应是默认值 {expected}，实际 {back['preset']}")
        ok(f"转回预设成功，且落在默认版式 {expected} 上")

        await check_default_preset(browser, errors)

        # 故事步骤单独开一本书，免得它的批量删除影响上面那些排版断言
        await check_story_tab(browser, errors)

        # 列表页放最后：它会往数据目录里塞 13 本书，跑完自己删掉，
        # 但别让它在前面把「最近更新」的排序搅了
        await check_book_list(browser, errors)

        if errors:
            fail(f"页面有 JS 报错: {errors[:3]}")
        ok("全程无 JS 报错")

        await browser.close()

    print("\n全部通过。")


if __name__ == "__main__":
    asyncio.run(main())
