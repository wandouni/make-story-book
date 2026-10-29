"""端到端验证翻页预览与文字溢出提示（Phase 8）。

两件事单测都覆盖不到：

- 翻页预览的"含出血"开关到底有没有改变渲染出来的页面尺寸 —— 得量 iframe
  里那个 `.page` 的实际宽度
- 文字放不下时，iframe 里的 fit-text.js 有没有把溢出 postMessage 回父窗口，
  父窗口有没有接住并弹提示。这条链路跨了文档边界，只有真跑才知道

用法（两个服务都要在跑）：
    uv run python scripts/e2e_preview.py
"""

import asyncio
import os
import re
import sys

import httpx

# 用 localhost 而不是 127.0.0.1：vite 默认只监听 ::1，写 IPv4 会 connection refused
API = os.getenv("MSB_API", "http://127.0.0.1:8000/api")
UI = os.getenv("MSB_UI", "http://localhost:5173")

SHORT_STORY = (
    "小兔子推开木门，外面是一片银白的雪地。\n\n"
    "它踩出第一行脚印，回头看了看，又继续往前走。\n\n"
    "雪越下越大，远处亮起一盏橘色的灯。"
)

# 一整页塞这么多字，A5 的文字框放不下 —— 用来逼出溢出提示。
#
# 注意要**足够**长：fit-text.js 会先把字号降到原大的 55% 才认输。实测 627 字
# 在 13px 下刚好塞进 357px 高的框（scrollHeight 354），差 3px 就不报溢出了。
# 所以这里给到 ~2500 字，远超过降字号能救回来的范围。
LONG_STORY = (
    "小兔子推开木门，外面是一片银白的雪地。它踩出第一行脚印，回头看了看，"
    "又继续往前走。雪越下越大，远处亮起一盏橘色的灯。它想起妈妈说过，"
    "沿着灯光走就能找到回家的路，于是把围巾裹紧了一点，一步一步朝那盏灯走去。"
    "风从耳边刮过，带着细碎的雪粒，打在脸上有点疼。它数着自己的脚步，"
    "一、二、三，数到一百的时候，灯光变得近了。那是一座小木屋，窗子里透出暖黄的光，"
    "烟囱上冒着淡淡的白烟，空气里有柴火和热汤的味道。它抬起手，轻轻敲了敲门。"
) * 12

# 5 段、够分成 5 页。3D 检查要的是"对开 → 对开"的翻页（屏序列 [1] [2,3] [4,5]），
# 上面那个 SHORT_STORY 只有 3 段，分页器最多给出 3 页，凑不出第二屏对开。
FIVE_PAGE_STORY = (
    "小兔子推开木门，外面是一片银白的雪地。\n\n"
    "它踩出第一行脚印，回头看了看，又继续往前走。\n\n"
    "雪越下越大，远处亮起一盏橘色的灯。\n\n"
    "它把围巾裹紧了一点，一步一步朝那盏灯走去。\n\n"
    "灯下是一座小木屋，窗子里透出暖黄的光，烟囱上冒着淡淡的白烟。"
)

# 25 段 → 25 页（对开 13 屏）。拖拽要停在**中间**那几屏上测：
#   - 首屏（封面）是单页屏，单页屏切对开是"直接换、不播动画"，根本没有纸可拖；
#   - 末屏往后翻会被 step() 夹住，翻页请求整个是空操作 —— "没动"看着像弹回成功，
#     其实压根没发生。中间屏才同时有前有后。
# 给到 25 而不是刚好够用：后面还要连翻 6 屏，末屏留得太近会被夹住。
DRAG_STORY = "\n\n".join(
    f"第 {i} 段：小兔子沿着灯光往前走，雪在脚下发出细碎的响声。" for i in range(1, 26)
)

# 3D 显存里纹理数的上界。页面纹理是大头：一页 A5、`scale=2` 的 PNG 约 1118×1588，
# 解成 RGBA 约 7MB。构成大致是：
#   页面纹理  ≈ 11（`TEXTURE_WINDOW=4` 的窗口宽 9，加翻页动画预铺的一两张）
#   页口贴图    4（左右两半垛各有书口面 + 天头地脚面）
#   背景/接影/封面 3
# 实测稳态在 17~18，这里留 6 的余量 —— 卡太紧会因为窗口边缘的波动偶发变红。
#
# 上界写成绝对数而不是"和上一屏比"：翻到书末时可用页变少、纹理数会**下降**，
# 拿相邻两屏比会把"降了"也判成异常。
TEXTURE_CEILING = 24

PX_PER_MM = 96 / 25.4

# 3D 视图的 DEV-only 只读状态（FlipBook3D 里挂在 canvas 上的 __flipbook3d，每帧
# 原地更新）。拖拽的语义断言（"纸是**因为我的拖动**才转的"）用像素做不到 ——
# 自动翻页动画也在改像素，两者在截图里长得一样。
STATE = """() => {
  const c = document.querySelector('.stage3d canvas');
  const s = c && c.__flipbook3d;
  if (!s) return null;
  return {intro: s.intro, introT: s.introT, flipMode: s.flipMode,
          progress: s.progress, spread: s.spread,
          shown: [s.shownLeft, s.shownRight], geometries: s.geometries,
          textures: s.textures};
}"""

# 静止弧度：左页几何两端顶点的 z 位移。见 expect_rest_curl。
RISE = """() => {
  const c = document.querySelector('.stage3d canvas');
  const s = c && c.__flipbook3d;
  if (!s) return null;
  return {spine: s.pageRiseSpine, fore: s.pageRiseFore};
}"""

# 逐帧记录器。**必须在打开弹窗前注入**，理由有两层：
#
# 1. headless 的 rAF 是节流的 —— 没人要帧就不画。等 `wait_for_selector` 回来再
#    开始采样，进场（1.5 秒）已经跑完了，永远只能采到结束后的画面。这个循环自己
#    驱动 rAF，顺带把节流解掉。
# 2. `page.screenshot()` 在这页上要 1.2 秒（WebGL 页面的合成回读，跟裁剪范围无关），
#    比进场本身还长，靠"等 600ms 再截图"是卡不准的。所以改成按帧记录，事后挑。
#
# 亮度用 `gl.readPixels` 而不是 `canvas.toDataURL()` / `drawImage`：那两条都要求
# `preserveDrawingBuffer`，本项目没开（开了有性能代价），拿到的是全 0。
# 而 readPixels 只要在 renderer.render() **之后**的同一帧里读就有效。
#
# `setTimeout(0)` 那行是排序的关键：组件在自己的 rAF 回调末尾登记下一帧，直接
# 从本回调里登记的话记录器永远排在组件前面，读到的就是还没画的空缓冲（实测全 0）。
# 挪到本帧回调批次之后登记，下一帧组件先跑、记录器后跑，读到的才是刚画完那帧。
RECORDER = """() => {
  window.__msb = [];
  const tick = () => {
    if (window.__msbStop) return;
    const c = document.querySelector('.stage3d canvas');
    const s = c && c.__flipbook3d;
    if (c && s) {
      const gl = c.getContext('webgl2') || c.getContext('webgl');
      const W = gl.drawingBufferWidth, H = gl.drawingBufferHeight;
      if (W > 0) {
        const bw = Math.round(W * 0.34), bh = Math.round(H * 0.6);
        const buf = new Uint8Array(bw * bh * 4);
        gl.readPixels(Math.round((W - bw) / 2), Math.round((H - bh) / 2), bw, bh,
                      gl.RGBA, gl.UNSIGNED_BYTE, buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i += 4) {
          sum += 0.299 * buf[i] + 0.587 * buf[i + 1] + 0.114 * buf[i + 2];
        }
        window.__msb.push({
          t: s.introT, intro: s.intro, camZ: s.camZ,
          mean: Math.round(sum / (bw * bh) * 10) / 10,
        });
      }
    }
    setTimeout(() => requestAnimationFrame(tick), 0);
  };
  requestAnimationFrame(tick);
}"""


def fail(msg: str) -> None:
    print(f"✗ {msg}")
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"✓ {msg}")


def make_book(story: str, pages: int, title: str) -> str:
    with httpx.Client(base_url=API, timeout=30) as c:
        book = c.post(
            "/books", json={"title": title, "trimSizeId": "A5", "pageCount": pages}
        ).json()
        book_id = book["id"]
        c.put(f"/books/{book_id}/story", json={"text": story})
        resp = c.post(f"/books/{book_id}/paginate", json={"pageCount": pages, "preset": "image-top"})
        resp.raise_for_status()
    return book_id


async def click_half(page, locator, *, right: bool) -> None:
    """点某个元素（或它内部任意处）的左/右半边。

    用真实鼠标坐标而不是 `locator.click(position=...)`：这个手势靠的就是
    `clientX` 落在舞台的哪一半，得让事件真的带上正确的坐标。
    """
    box = await locator.bounding_box()
    if not box:
        fail("拿不到元素的 bounding box，没法点")
    x = box["x"] + box["width"] * (0.8 if right else 0.2)
    y = box["y"] + box["height"] / 2
    await page.mouse.click(x, y)


async def drag_page(page, canvas, *legs: float, steps: int = 6, start: float = 0.52) -> list[dict]:
    """按住画布右半边（= 往后翻那一侧），按 `legs` 依次拖，最后抬手。

    `legs` 是若干段位移，单位是**行程的比例**（行程 = 容器宽的一半，和组件里
    `drag.travel` 一致）：`drag_page(page, canvas, 0.5, -0.3)` 是"先往左拖半个
    行程、再往右拖回 0.3 个行程"。负数就是往右（反着翻）。

    返回**每段拖完时**的状态快照（抬手之前取的），所以调用方可以拿它断言
    "纸确实是跟着指针走的"。

    起点定在 0.52 处，是两头夹出来的：要落在右半边（`dir` 是按 pointerdown 落在
    容器哪一半定的，左半边就是往前翻），又得给两个方向都留出接近半个视口宽的
    余量（往左最多能走 0.52 个视口宽 = 1.04 个行程，往右 0.48 = 0.96 个行程）。

    四个坑，都是实测踩出来的：

    1. **一段之内别逼帧，一段之后再逼两帧**。逼帧是 `Runtime.evaluate`，它会插到
       排队的输入事件前面去 —— 每步都逼的话，pointermove 要等到下下帧才送达，
       而自动翻页只有 720ms，拖拽就永远接管不了（实测：每步逼帧时整个手势跑完
       `flip` 还是 null，纸一帧都没跟手）。一段之后再逼，攒着的移动会一起送达，
       而进度只取决于**最后一个位置**（`clientX - drag.x0`），中间丢几帧无所谓。
    2. **逼两帧**：第一帧把 pointermove 送达，第二帧 `render()` 才会把新的 progress
       写进 `__flipbook3d` 快照 —— 快照是每帧刷的，不是即时的，只逼一帧读到的是
       上一帧的值。
    3. **步长必须跨过 6px 的 slop**。跨不过去拖拽永远激活不了。所以一段别开太多步。
    4. **视口用 800×600**（见 check_flipbook_3d_drag）：headless 是 SwiftShader
       软件渲染，帧耗时随像素数走。1440×900 时一帧 300ms 上下，移动会被大量合并，
       同一段位移每次跑出来都不一样；800×600 下实测每次都一模一样。

    还有一个构造不出来的手势，见 check_flipbook_3d_drag 的说明：拖拽的基准进度是
    **接管那一刻**的进度（`baseProgress`），而 headless 里接管总要晚上几十上百
    毫秒，所以"从 0 开始的小位移"造不出来，断言只能建立在接管之后的相对位移上。
    """
    box = await canvas.bounding_box()
    if not box:
        fail("拿不到 3D 画布的 bounding box，没法拖")
    travel = box["width"] * 0.5
    x = box["x"] + box["width"] * start
    y = box["y"] + box["height"] * 0.5

    await page.mouse.move(x, y)
    await page.mouse.down()
    states = []
    for leg in legs:
        step = travel * leg / steps
        for _ in range(steps):
            x -= step
            await page.mouse.move(x, y)
        await page.evaluate("1")
        await page.evaluate("1")
        states.append(await page.evaluate(STATE))
    await page.mouse.up()
    return states


def expect_progress(actual: float, want: float, why: str) -> None:
    """断言拖动之后的进度。`want` 由调用方按"接管时的进度 + 位移"算好。

    进度只取决于指针的**最终位置**，所以这个等式是精确的（误差只来自像素取整，
    1/384 的量级），容差 0.02 已经放得很宽。
    """
    if abs(actual - want) > 0.02:
        fail(f"{why}：进度应该是 {want:.3f}，实际 {actual:.3f}")


def pages_in(counter: str) -> list[int | None]:
    """从计数器文字里抠出"该摊着哪两页"。

    '第 4-5 页 / 共 21 页' → [4, 5]；'第 1 / 21 页' → [1, None]。
    单页屏和对开屏的格式不同，这里统一成"长度 2 的列表"好和 `shown` 直接比。

    结尾必须是 `页` **或** `/`：单页屏是「第 2 / 5 页」，页码后面跟的是那个斜杠，
    只认 `页` 的话这里会直接 fail（原来就是这么写的，只是 `expect_screen` 一直在
    对开屏上调用，从没碰到过单页格式）。
    """
    m = re.match(r"第\s*(\d+)\s*(?:-\s*(\d+))?\s*(?:页|/)", counter)
    if not m:
        fail(f"看不懂计数器文字：{counter!r}")
    return [int(m.group(1)), int(m.group(2)) if m.group(2) else None]


async def expect_screen(page, want: tuple[int, int | None], why: str) -> None:
    """同时断言「计数器文字」和「画面上实际摊着的两页」。

    这两个是**两套状态**：计数器在 pointerdown 就前进（点击语义），画面只在纸真的
    翻过去时才动。只断言一边的话，"拖拽弹回后计数器和画面错位"这个 bug 照样过 ——
    而它的表现是用户下一次点击整屏跳过去，很难联想到是拖拽留下的。
    """
    counter = (await page.locator(".flipbook-dialog .counter").inner_text()).strip()
    shown = (await page.evaluate(STATE))["shown"]
    if pages_in(counter) != list(want) or shown != list(want):
        fail(f"{why}：应该是 {list(want)}，计数器说 {pages_in(counter)}，画面摊着 {shown}")


async def expect_rest_curl(page, why: str) -> None:
    """静止时页面有弧度：书脊那一缘抬起来，书口那一缘趴在垛上不动。

    两个理由：

    ① 弧度小的时候截图上根本量不出来。页面近白、光又漫，"法线偏转 → 明暗"这条路
       只剩一两个灰阶 —— 4.5% 那一版就是这么废掉的（改前改后整条横切上蓝通道差 1）。
    ② 方向错了的**表现**特别容易被误判成别的问题。"书口垂下去"（弧度取反）时页面
       会沉进纸垛里 —— 纸垛是块实心 Box，挡在页面前面，现象是中缝两侧露出垛顶、
       只剩书脊附近一条页面还看得见，而垛顶和纸面同色，看着只是"变暗了"，很像
       光照问题。当初为这个查了半天。

    书脊在哪一缘**随版式变**（见 `layout()` 里那段），组件里的钩子已经把这件事翻译
    成"书脊端 / 书口端"了，这里只管断言这两个数。阈值取 `REST_CURL` 的一半。

    **它现在验的是"驱动弯折的 uniform 值对"，不是"GPU 真的把几何弯了"** —— 弯折搬
    到顶点着色器之后顶点缓冲里的 z 恒为 0，读不到了，钩子改成按同一个公式求值。所以
    GLSL 位移那段写错而 TS 侧常量没跟着改的话，这条**不会红**（那种错在画面上是响的，
    留给目视验收）。它保住的是"方向反了"：钩子的书脊端取自**版式**、和 `layout()` 里
    那份各自独立派生，写入端一写反读数就掉到 0。为什么不做轮廓断言见 README。
    """
    got = await page.evaluate(RISE)
    if got["spine"] < 0.06:
        fail(f"{why}：书脊那缘没抬起来（z={got['spine']:.4f}）—— 页面还是块平板")
    if abs(got["fore"]) > 1e-6:
        fail(f"{why}：书口那缘不该有位移，实际 z={got['fore']:.4f} —— 弧度方向反了")


async def check_flipbook(p, errors: list[str]) -> None:
    book_id = make_book(SHORT_STORY, 3, "翻页预览 E2E")
    page = await p.new_page(viewport={"width": 1600, "height": 1000})
    page.on("pageerror", lambda e: errors.append(str(e)))

    await page.goto(f"{UI}/books/{book_id}", wait_until="networkidle")

    await page.get_by_role("button", name="翻页预览").click()
    await page.wait_for_selector(".flipbook-dialog", timeout=15000)

    # 3 页在对开模式下的屏序列是 [1] [2,3]，共 2 屏。
    # 默认是对开 —— 用户要的就是"像看书一样"。
    #
    # 视图**默认是 3D**，平面 iframe 只是 `v-show` 藏着（display: none），
    # `wait_for` 默认要 visible，不先切过去会直接超时。这一整个函数量的是平面
    # 视图的几何（iframe 里的 .page 尺寸），所以第一步就是切回平面。
    await page.get_by_text("平面", exact=True).click()

    frames = page.locator(".flipbook-dialog .frame")
    await page.frame_locator(".flipbook-dialog .frame").first.locator(".page").wait_for(
        timeout=15000
    )
    ok("翻页预览已打开，iframe 渲染出了 .page")

    # ---- 对开：封面单独一屏
    if await frames.count() != 1:
        fail(f"封面屏应该只有 1 页，实际 {await frames.count()} 页")
    if "第 1 / 3 页" not in await page.locator(".counter").inner_text():
        fail(f"封面屏的计数器不对：{await page.locator('.counter').inner_text()}")
    ok("对开模式下封面单独占一屏")

    # ---- 对开：翻到 [2,3]，两页必须严格等高同宽
    await page.get_by_role("button", name="下一屏 →").click()
    await page.wait_for_timeout(900)
    if "第 2-3 页" not in await page.locator(".counter").inner_text():
        fail(f"对开翻屏后计数器不对：{await page.locator('.counter').inner_text()}")
    if await frames.count() != 2:
        fail(f"对开屏应该有 2 页，实际 {await frames.count()} 页")

    # 这条是整个对开功能的成败点：缩放必须由 FlipBook 统一算、两页共用。
    # 各自 measure() 的话，容器按 flex 分列时的亚像素取整会算出两个略不同的
    # zoom，两页就差一两个像素 —— 中缝看着就是错开的。
    left = await frames.nth(0).bounding_box()
    right = await frames.nth(1).bounding_box()
    if abs(left["width"] - right["width"]) > 0.5 or abs(left["height"] - right["height"]) > 0.5:
        fail(
            "对开的两页尺寸不一致 —— 缩放不是共用的："
            f"左 {left['width']:.2f}×{left['height']:.2f}，"
            f"右 {right['width']:.2f}×{right['height']:.2f}"
        )
    # 两页要真的贴在一起（中缝没有缝），否则看着是两张纸不是一本书
    gap = right["x"] - (left["x"] + left["width"])
    if abs(gap) > 0.5:
        fail(f"对开两页之间不该有缝，实际 {gap:.2f}px")
    ok(f"对开两页等高等宽（{left['width']:.1f}×{left['height']:.1f}）且无缝贴合")

    # 只缩不放：页面不该被放大到超过原始尺寸
    if left["width"] > 148.0 * PX_PER_MM + 0.5:
        fail(f"对开时页面被放大了（{left['width']:.1f}px > 原始 {148.0 * PX_PER_MM:.1f}px）")
    ok("对开时只缩不放")

    # 末屏不能再往后
    if await page.get_by_role("button", name="下一屏 →").is_enabled():
        fail("到最后一屏了，「下一屏」还能点")
    ok("翻到末屏后按钮正确禁用")

    # ---- 点左/右半边翻页（平面视图）
    # 这条靠的是 iframe 上的 `pointer-events: none`：点在页面上会穿透到
    # `.book`、再冒泡到舞台。哪天有人把它删了，点击会被 iframe 吞掉，
    # 而现象正是"点左右没反应" —— 只有真点一下才测得出。
    stage = page.locator(".flipbook-dialog .stage")
    await click_half(page, stage, right=False)
    await page.wait_for_timeout(700)
    if "第 1 / 3 页" not in await page.locator(".counter").inner_text():
        fail(f"点舞台左半边没翻回上一屏：{await page.locator('.counter').inner_text()}")

    await click_half(page, stage, right=True)
    await page.wait_for_timeout(700)
    if "第 2-3 页" not in await page.locator(".counter").inner_text():
        fail(f"点舞台右半边没翻到下一屏：{await page.locator('.counter').inner_text()}")
    ok("平面视图点左/右半边能翻屏")

    # ---- 切回单页：沿用原有的尺寸断言
    # 点可见的 label 而不是 role=radio：Element Plus 把真实的 input 藏起来了，
    # 点它会一直报 "intercepts pointer events"
    await page.get_by_text("单页", exact=True).click()
    await page.wait_for_timeout(900)
    if await frames.count() != 1:
        fail(f"单页模式应该只有 1 个 iframe，实际 {await frames.count()} 个")
    if "第 2 / 3 页" not in await page.locator(".counter").inner_text():
        fail(f"切回单页后计数器不对：{await page.locator('.counter').inner_text()}")
    ok("切到单页模式后只剩一页，且位置没丢（还在第 2 页）")

    frame = page.frame_locator(".flipbook-dialog .frame").first
    box = await frame.locator(".page").bounding_box()
    width_mm = box["width"] / PX_PER_MM
    if abs(width_mm - 148.0) > 1.5:
        fail(f"成品视图页面宽度应约 148mm，实际 {width_mm:.1f}mm")
    ok(f"成品视图页面宽度 {width_mm:.1f}mm")

    # 翻页：按钮与键盘都要能用（单页模式是"页"）
    await page.get_by_role("button", name="下一页 →").click()
    await page.wait_for_timeout(400)
    if "第 3 / 3 页" not in await page.locator(".counter").inner_text():
        fail("点「下一页」没翻过去")
    await page.keyboard.press("ArrowLeft")
    await page.wait_for_timeout(400)
    if "第 2 / 3 页" not in await page.locator(".counter").inner_text():
        fail("键盘左方向键没翻页")
    await page.keyboard.press("ArrowRight")
    await page.wait_for_timeout(400)
    if "第 3 / 3 页" not in await page.locator(".counter").inner_text():
        fail("键盘右方向键没翻页")
    ok("按钮与键盘翻页都生效")

    # 切到含出血：四周各 3mm，应变成 154mm
    await page.get_by_text("含出血", exact=True).click()
    await page.wait_for_timeout(800)
    box = await frame.locator(".page").bounding_box()
    bleed_mm = box["width"] / PX_PER_MM
    if abs(bleed_mm - 154.0) > 1.5:
        fail(f"含出血视图页面宽度应约 154mm，实际 {bleed_mm:.1f}mm —— 出血没生效")
    ok(f"含出血视图页面宽度 {bleed_mm:.1f}mm（多出 {bleed_mm - width_mm:.1f}mm）")

    # ---- 含出血 + 对开：两页各自带出血，中缝两侧各一条
    await page.get_by_text("对开", exact=True).click()
    await page.wait_for_timeout(900)
    if await frames.count() != 2:
        fail(f"含出血的对开屏应该有 2 页，实际 {await frames.count()} 页")
    left = await frames.nth(0).bounding_box()
    right = await frames.nth(1).bounding_box()
    if abs(left["width"] - right["width"]) > 0.5:
        fail("含出血时对开两页宽度不一致 —— 共享缩放没生效")
    # 含出血的页面仍然只缩不放：原始宽度 154mm
    if left["width"] > 154.0 * PX_PER_MM + 0.5:
        fail(f"含出血对开时页面被放大了（{left['width']:.1f}px）")
    ok(f"含出血对开两页等宽（各 {left['width'] / PX_PER_MM:.1f}mm），各自带出血")

    # 反向：文字放得下的页不该弹提示。少了这一条，即使提示退化成"总是出现"，
    # 下面那条正面断言照样会过，而用户会很快学会无视它。
    await page.get_by_role("button", name="关闭").click()
    await page.wait_for_timeout(500)
    await page.get_by_text("排版", exact=True).click()
    await page.wait_for_selector(".rail-item", timeout=15000)
    await page.wait_for_timeout(2500)
    if await page.locator(".overflow-notice").count():
        fail("这一页的文字放得下，却弹出了溢出提示 —— 提示不可信")
    ok("文字放得下时不弹提示")

    await page.close()


def png_size(data: bytes) -> tuple[int, int]:
    """从 PNG 头里读宽高（IHDR 紧跟在 8 字节签名 + 4 字节长度 + 4 字节类型之后）。

    只为读两个整数引一个图像库不值得。
    """
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        fail("截图端点返回的不是 PNG")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return width, height


def check_shot_endpoint() -> None:
    """3D 翻页的纹理端点。

    单独在这里测而不是放进 pytest：它要开一次 Chromium 截真页面，比单测慢
    三个数量级，而单测里 Chromium 的可用性又没法保证。
    """
    book_id = make_book(SHORT_STORY, 3, "3D 纹理 E2E")
    url = f"/books/{book_id}/render/page/1/shot.png"

    with httpx.Client(base_url=API, timeout=60) as c:
        resp = c.get(url, params={"mode": "preview", "scale": 2})
        resp.raise_for_status()
        width, height = png_size(resp.content)

        # 截的是 .page 元素本身，不是整个视口 —— 拿视口尺寸（1400×2000）就对不上
        want_w = 148.0 * PX_PER_MM * 2
        want_h = 210.0 * PX_PER_MM * 2
        if abs(width - want_w) > 3 or abs(height - want_h) > 3:
            fail(f"截图尺寸应是 {want_w:.0f}×{want_h:.0f}（A5 的 2 倍），实际 {width}×{height}")
        ok(f"单页截图是页面尺寸的 2 倍像素（{width}×{height}）")

        # 含出血时四周各多 3mm
        bleed = c.get(url, params={"mode": "print", "scale": 2})
        bleed.raise_for_status()
        b_width, _ = png_size(bleed.content)
        if b_width <= width:
            fail("含出血的截图没有变宽 —— mode 没传进去")
        ok(f"含出血截图更宽（{b_width} > {width}）")

        # 缓存命中：第二次要明显更快（文件名带内容指纹，命中时直接返回落盘文件）
        if c.get(url, params={"mode": "preview", "scale": 2}).content != resp.content:
            fail("同一页两次截图的内容不一致")
        ok("同一页重复请求返回同一张图")

        # 越界页码 404，而不是 500
        if c.get(f"/books/{book_id}/render/page/99/shot.png").status_code != 404:
            fail("越界页码的截图没有返回 404")
        # scale 有上限，防止有人拿它当"给我一张 100 倍大图"用
        if c.get(url, params={"scale": 99}).status_code != 422:
            fail("超范围的 scale 没有返回 422")
        ok("越界页码 404、超范围 scale 422")


async def check_flipbook_3d(p, errors: list[str]) -> None:
    """3D 翻页：WebGL 起得来、纹理真的从后端来、翻页不崩。

    用 5 页的书：屏序列是 [1] [2,3] [4,5]，这样才有"对开 → 对开"的翻页
    （封面那一屏是一页独占，切到对开时书的几何整个变，按设计不播动画）。
    """
    book_id = make_book(FIVE_PAGE_STORY, 5, "3D 翻页 E2E")
    page = await p.new_page(viewport={"width": 1440, "height": 900})
    page.on("pageerror", lambda e: errors.append(str(e)))

    # 纹理是从 `/render/page/{n}/shot.png` 来的 —— 这条链路断了的话 3D 里
    # 就是一本白纸书，而画面本身不报错，只有盯请求才看得出来
    shots: list[tuple[str, int]] = []
    page.on(
        "response",
        lambda r: shots.append((r.url, r.status)) if "/shot.png" in r.url else None,
    )

    await page.goto(f"{UI}/books/{book_id}", wait_until="networkidle")
    # 记录器要在**打开弹窗之前**注入，否则进场已经跑完了，见 RECORDER 的注释
    await page.evaluate(RECORDER)
    await page.get_by_role("button", name="翻页预览").click()
    await page.wait_for_selector(".flipbook-dialog", timeout=15000)

    # ---- 默认就是 3D，一个开关都不用点
    #
    # 3D 是 `v-if` 懒挂载的：默认值被改回平面的话这里永远等不到 canvas，
    # 而不是"画布在但没显示"—— 所以这条断言同时钉住了"默认值"和"懒挂载"。
    # 弹窗里有三组 radio（视图 / 版式 / 出血），每组都有一个 is-active，
    # 所以要连文字一起过滤，不能只取 is-active
    if await page.locator(".flipbook-dialog label.el-radio-button.is-active", has_text="3D").count() != 1:
        active = await page.locator(".flipbook-dialog label.el-radio-button.is-active").all_inner_texts()
        fail(f"翻页预览的默认视图应该是 3D，实际高亮的是 {active}")
    if await page.locator(".flipbook-dialog .frame").first.is_visible():
        fail("默认 3D 时平面 iframe 不该是可见的")
    ok("默认进 3D 视图（平面 iframe 藏着）")

    canvas = page.locator(".stage3d canvas")
    await canvas.wait_for(timeout=15000)
    box = await canvas.bounding_box()
    if box["width"] < 200 or box["height"] < 200:
        fail(f"3D 画布尺寸不对：{box['width']:.0f}×{box['height']:.0f}")
    ok(f"3D 视图挂上了 WebGL 画布（{box['width']:.0f}×{box['height']:.0f}）")

    # ---- 进场：封面从右往左掀开
    #
    # 断言用的是**亮度关系**而不是具体像素：材质色会被暖光整体推移，钉死颜色等于
    # 埋雷；而"合着的深色封面 → 掀开后是浅色页面"这个关系与光照无关。
    #
    # 阈值 30（0-255 灰度）。实测多轮：封面帧最暗 120~135、进场结束后 236 上下，
    # 差 100~116 —— 阈值留了 3 倍以上余量。采样区域是画布中央 34%×60%
    #（readPixels 的坐标原点在左下角，取中央就不用管翻转）。
    await page.wait_for_timeout(3000)
    log = await page.evaluate("() => { window.__msbStop = true; return window.__msb; }")
    frames = [f for f in log if f["mean"] == f["mean"]]  # 丢掉画布还没尺寸那几帧
    cover = [f for f in frames if f["intro"] == "cover"]
    settled = [f for f in frames if f["intro"] == "none"]
    if not cover:
        fail(f"进场没有播「掀封面」那一段（记录到 {len(frames)} 帧，intro 全是 none）")
    if min(f["t"] for f in cover) >= 0.5:
        fail(f"只记到进场后半段（最早的 cover 帧 t={min(f['t'] for f in cover):.2f}）—— 采样太晚，这条测不出东西")
    if not settled:
        fail("进场结束后没记到任何一帧")
    dark = min(f["mean"] for f in cover)
    light = max(f["mean"] for f in settled)
    if light - dark < 30:
        fail(f"封面帧和进场结束后的画面一样亮（{dark} vs {light}）—— 封面没画出来，或者进场没播")
    ok(f"进场掀封面：合着的封面亮度 {dark} → 掀开后 {light}（差 {light - dark:.0f}）")

    # ---- 进场：相机从更近、更低的机位退回静止机位
    #
    # 这条**只能**靠 `camZ`。相机距离只影响画面，没有任何语义状态跟着变（翻页有
    # progress、版式有 spread，相机什么都没有），拿像素也分不清"是镜头在退"还是
    # "书在飞"—— 而这两件事同时发生。不挂这个字段的话，`INTRO_CAM_PULL` 那段
    # 代码是写完就从没被执行过的。
    # 阈值 0.05 是**保守**的：真实最大偏移是 `cameraRestZ × INTRO_CAM_PULL`（实测
    # 9.14 × 0.16 ≈ 1.5），但 RECORDER 抓不到进场最早那几帧 —— headless 里 rAF 只有
    # 个位数帧率，`__flipbook3d` 挂上到第一次采样之间已经过了约 1/4 个进场，那时
    # 相机退得只剩一成多。所以这里量到的是**残差**（约 0.14），不是全幅。
    near = min(f["camZ"] for f in cover)
    rest = min(f["camZ"] for f in settled)
    if rest - near < 0.05:
        fail(
            f"进场期间相机没在拉远（cover 阶段最近 {near:.2f}，静止后 {rest:.2f}）"
            f" —— INTRO_CAM_PULL 没生效"
        )
    ok(f"进场相机缓动：{near:.2f} → {rest:.2f}（从更近的机位退回静止机位）")

    # 封面 → [2,3]：单页屏切对开，书的几何整个变，按设计是直接换、不播动画
    next_btn = page.get_by_role("button", name="下一屏 →")
    await next_btn.click()
    await page.wait_for_timeout(1200)
    if "第 2-3 页" not in await page.locator(".counter").inner_text():
        fail(f"3D 里从封面翻到对开没生效：{await page.locator('.counter').inner_text()}")

    # [2,3] → [4,5]：这一屏才是"对开 → 对开"，要真的播翻页动画。
    # 动画跑到一半时画布不能消失（那是 WebGL 上下文丢了的表现）。
    await next_btn.click()
    await page.wait_for_timeout(300)
    if await canvas.count() != 1:
        fail("翻页动画进行中画布就没了")
    await page.wait_for_timeout(3000)
    if "第 4-5 页" not in await page.locator(".counter").inner_text():
        fail(f"3D 模式下翻屏没生效：{await page.locator('.counter').inner_text()}")
    if await canvas.count() != 1:
        fail("翻页之后画布没了 —— WebGL 上下文可能丢了")
    ok("3D 里连翻两屏后画布仍在，计数器跟着走")

    # ---- 纹理确实是从后端渲染端点一页一页取来的
    #
    # 放在翻过两屏之后，因为要断言的正是"**翻到哪一页就去取哪一页**"：翻页时
    # 如果偷懒沿用上一页的纹理，画面上是看不出来的（有纸、有阴影、翻页动画照跑），
    # 只有盯请求才发现。5 页书翻到末屏，1~5 页应该各取过一次。
    if not shots:
        fail("3D 视图没有请求任何单页截图 —— 纹理不是从后端渲染端点来的")
    bad = [s for s in shots if s[1] != 200]
    if bad:
        fail(f"截图请求有失败：{bad[:3]}")
    got = {int(m.group(1)) for url, _ in shots if (m := re.search(r"/page/(\d+)/shot\.png", url))}
    if got != {1, 2, 3, 4, 5}:
        fail(f"翻到末屏时应该把 1~5 页都取过一遍，实际只取了 {sorted(got)}")
    ok(f"3D 从后端渲染端点取了 1~5 页的纹理（{len(shots)} 次请求，全部 200）")

    # ---- 点画布翻页：一次点击只能翻**一屏**
    #
    # 3D 自己处理 pointerdown（emit 给 FlipBook），同时那次点击还会冒泡到舞台的
    # click 上。两边都算的话点一下会翻两屏 —— 从 [4,5] 点左半边会直接落到 [1]。
    await click_half(page, canvas, right=False)
    await page.wait_for_timeout(2500)
    if "第 2-3 页" not in await page.locator(".counter").inner_text():
        fail(
            "3D 里点画布左半边没翻到上一屏："
            f"{await page.locator('.counter').inner_text()}（翻了两屏？）"
        )
    ok("3D 视图点画布左/右半边能翻屏，且一次只翻一屏")

    # ---- 静止弧度（对开）：书脊在中缝，所以左页的书脊在它**右缘**
    await expect_rest_curl(page, "3D 对开屏 [2,3] 的静止弧度")
    ok("3D 对开时两页都有静止弧度，书脊那缘抬起、书口那缘不动")

    # ---- 单页模式下连翻：每一屏都是单页屏
    #
    # 这条专堵 applyTarget 里"单页 → 单页"那条分支。单页屏没有书脊两边，startFlip
    # 一进去就 `if (right === null) return` —— 漏掉这个分支的现象是**画面一动不动**，
    # 而 FlipBook 的计数器照走（计数器在 pointerdown 就前进，是点击语义）。所以只断言
    # 计数器**抓不到**，必须同时看画面上摊着的两页，这正是 expect_screen 干的事。
    # 2 页的书（[1,·] → [2,·]）走的是同一条路 —— 这个 bug 最早就是这么报上来的。
    await page.get_by_text("单页", exact=True).click()
    await page.wait_for_timeout(1500)
    await expect_screen(page, (2, None), "3D 里切到单页模式")

    # ---- 方向键：3D 视图下也要能用（以前只在平面视图测过）
    #
    # 这段**必须紧跟在点完「单页」之后、任何真实鼠标点击之前**。那个
    # `el-radio-button` 内层渲染的是真的 `<input type="radio">`，点一下焦点就落到
    # 它上面；守卫写成 `closest('input, textarea, ...')` 的话此后方向键全被挡掉 ——
    # 而"被挡掉"不报错，用户只会觉得键坏了。
    #
    # 踩过的坑：第一版把这段放在下面那个 `click_half` 循环**之后**，结果旧守卫
    # 照样全绿 —— 因为 `click_half` 用真实鼠标点画布，焦点早从 input 上移走了。
    # 所以焦点状态本身也要断言，见下面那两行；少了它，这段以后被人挪个位置就
    # 又变成一条测不到东西的假测试，而且**不会红**。
    active = await page.evaluate("() => document.activeElement?.className || ''")
    if "el-radio-button__original-radio" not in active:
        fail(f"焦点不在单选按钮的 input 上（activeElement 的 class 是 {active!r}）—— 这条断言测不到那个 bug")

    # **先按右键，顺序不能反。** 左键会走到封面，而封面在单页和对开下都是
    # `[1, None]`，两种版式分不出来 —— 第一版就是这么写的：左键、右键各一下，
    # 把 `preventDefault()` 关掉之后**照样全绿**，因为它测不出"版式被切走了"。
    # 右键从第 2 页走到第 3 页，单页是 `[3, None]`、对开是 `[2, 3]`，这才分得开。
    await page.keyboard.press("ArrowRight")
    await page.wait_for_timeout(2200)
    await expect_screen(page, (3, None), "3D 里按右方向键")
    await page.keyboard.press("ArrowLeft")
    await page.wait_for_timeout(2200)
    await expect_screen(page, (2, None), "3D 里按左方向键")

    # 版式开关不能被方向键带跑。焦点停在 radio 上时，方向键是**浏览器原生**的
    # "在本组单选里移动选中项"，只 `step()` 不 `preventDefault()` 的话，翻页和
    # 切版式会同时发生 —— 实测：从「单页」按左键变成「对开」，而计数器文字碰巧
    # 还是对的（封面两种版式都显示「第 1 / 5 页」）。所以这条必须单独钉。
    if await page.locator(
        ".flipbook-dialog label.el-radio-button.is-active", has_text="单页"
    ).count() != 1:
        got = await page.locator(".flipbook-dialog label.el-radio-button.is-active").all_inner_texts()
        fail(f"方向键把版式开关带跑了：现在高亮的是 {got} —— preventDefault 没生效")
    ok("3D 视图下方向键能翻页，且不会带跑工具条上的版式开关")

    for want in ((3, None), (4, None)):
        await click_half(page, canvas, right=True)
        await page.wait_for_timeout(2200)
        await expect_screen(page, want, f"单页模式下点右半边翻到第 {want[0]} 页")
    ok("3D 单页模式下连翻两屏，画面跟着走（不是只有计数器在动）")

    # ---- 静止弧度（单页）：**装订边换到左边了**
    #
    # 单页屏的身体（stackR/boardBack）挪到 x=0，包边只出在右边 —— 所以装订边成了
    # 左缘。弧度写死"书脊在右缘"的话，现象是**书口翘起来、装订边趴平**，页面从纸垛
    # 上翘出去。两个版式各断言一次，因为"书脊在哪一缘"这件事只有这里会变。
    await expect_rest_curl(page, "3D 单页屏的静止弧度")
    ok("3D 单页时装订边在左，弧度跟着换到了左缘")

    # 切回平面：iframe 是 v-show 藏起来的，不该重新加载（重新加载会白闪一下）
    await page.get_by_text("平面", exact=True).click()
    await page.wait_for_timeout(600)
    if not await page.locator(".flipbook-dialog .frame").first.is_visible():
        fail("切回平面后 iframe 没显示出来")
    if await page.locator(".stage3d canvas").count():
        fail("切回平面后 3D 画布还在 —— 没被卸载，显存会一直攒着")
    ok("切回平面后 iframe 还在，3D 画布已卸载")

    await page.close()


async def check_flipbook_3d_drag(p, errors: list[str]) -> None:
    """3D 里按住纸拖动跟手翻页。

    每一条都同时断言「计数器文字」和「画面摊着的两页」，理由见 `expect_screen`。

    **断言为什么都长成"先拖一段引子、再拖一段测量"的样子**：headless 里从
    pointerdown 到第一次 pointermove 送达要等主线程空出来，而主线程在忙 WebGL
    的一帧（150~300ms），于是拖拽接管的那一刻自动翻页已经跑掉一大截了。而拖拽的
    进度是**相对接管那一刻**累加的（`baseProgress`），所以"从 0 开始的小位移"在这里
    根本构造不出来 —— 写成"拖一点点必须弹回"会变成一条永远测不到东西的假测试。

    能构造的是**接管之后的相对位移**：接管之后每往左拖一个行程的比例，进度就精确地
    涨那么多。所以每个手势都是"引子（把接管消化掉）+ 测量段（等式精确对得上）"，
    而"松手翻不翻"靠的是引子+测量段的累计值 —— 不管接管时进度是多少，累计值都落在
    同一侧，结论因此不依赖环境快慢。

    **速度那两条（甩得够快就翻、抬手前静止了就不算甩）这里测不到，别以为是漏了**：
    阈值是 0.0015/ms，而这里两次 pointermove 之间至少隔一帧（150ms 上下），
    算出来的速度比阈值小一个量级 —— 造不出"甩"，"静止了不算甩"那条也就没有
    区分度（有它没它结果一样）。这两条留给人工。
    """
    book_id = make_book(DRAG_STORY, 25, "3D 拖拽 E2E")
    # 800×600 而不是 1440×900：headless 是软件渲染，帧耗时随像素数走。1440×900
    # 一帧要 300ms 上下，pointermove 被合并得太多，同一段位移每次跑出来的进度都
    # 不一样；800×600 实测稳定。拖拽测的是语义不是观感，画布小不影响结论。
    page = await p.new_page(viewport={"width": 800, "height": 600})
    page.on("pageerror", lambda e: errors.append(str(e)))

    await page.goto(f"{UI}/books/{book_id}", wait_until="networkidle")
    await page.get_by_role("button", name="翻页预览").click()
    canvas = page.locator(".stage3d canvas")
    await canvas.wait_for(timeout=15000)
    await page.wait_for_timeout(2500)  # 等进场（1.5s）跑完

    next_btn = page.get_by_role("button", name="下一屏 →")
    for _ in range(2):
        await next_btn.click()
        await page.wait_for_timeout(1500)
    await expect_screen(page, (4, 5), "拖拽用例的起点")
    ok("3D 里走到中间屏 [4,5]（前后都有内容）")

    # ---- ① 指针接管 + 纸跟手 + 松手位置决定去留
    #
    # 三段：往左 0.5 个行程（这一段是"引子"，拖拽在这一段里接管）、再往左 0.3
    # （这一段的位移和进度变化必须**精确对上**）、最后往右 0.8 把纸拖回起点。
    #
    #   - 第一段之后 `flipMode` 必须是 drag —— 指针真的接管了，不是在放自动动画；
    #   - 第二段的等式把"行程 = 容器宽的一半"这个映射钉死了，也把"拖拽只是装装
    #     样子、其实还在放动画"挡在外面。**只有第二段能这么断言**：接管发生在
    #     第一段里，被接管吃掉的那一小截位移是算不出来的（引子那一段只要求方向对）；
    #   - 最后往右拖回 0.8 个行程，进度必定落到 0.2 以下（`clamp01(p - 0.8) ≤ 0.2`）
    #     → 松手必须弹回。
    #
    # 弹回这条同时是"计数器要跟着退回去"的哨兵：计数器在 pointerdown 就前进了
    # （点击语义），只退画面不退计数器的话，用户下一次点击会整屏跳过去。
    states = await drag_page(page, canvas, 0.5, 0.3, -0.8)
    if states[0]["flipMode"] != "drag":
        fail(f"按住拖的时候翻页模式应该是 drag，实际 {states[0]['flipMode']!r} —— 指针没接管")
    expect_progress(states[1]["progress"], min(1.0, states[0]["progress"] + 0.3), "再往左拖 0.3 个行程")
    if states[2]["progress"] > 0.2:
        fail(f"往右拖回 0.8 个行程之后进度还有 {states[2]['progress']:.2f}")
    await page.wait_for_timeout(1500)
    await expect_screen(page, (4, 5), "拖过去又拖回来（松手时没过半）")
    ok(
        f"指针接管，纸跟手（再往左 0.3 个行程 → 进度正好 +0.300），"
        f"松手没过半 → 弹回 [4,5]（计数器和画面都对得上）"
    )

    # ---- ② 拖过一半松手 → 翻过去
    #
    # 两段各 0.5 个行程：不管接管那一刻进度是多少（0 到 1 都可能），累加 0.5 之后
    # 必定 ≥ 0.5，所以"翻过去"是必然的，不依赖环境快慢。
    states = await drag_page(page, canvas, 0.5, 0.5)
    if states[1]["progress"] < 0.5:
        fail(f"往左拖了整整一个行程，进度才 {states[1]['progress']:.2f} —— 纸没跟手")
    await page.wait_for_timeout(1500)
    await expect_screen(page, (6, 7), "拖过一半松手")
    ok(f"拖到 {states[1]['progress']:.2f} 松手 → 翻到 [6,7]")

    # ---- ③ 反着拖 → 必须弹回
    #
    # 这条钉的是**方向映射**：`dir` 是按 pointerdown 落在容器哪一半定的，往后翻的纸
    # 只有往左拖才涨进度。映射写反的话这里进度会一路涨到 1、纸直接翻过去。
    # 往右拖 0.9 个行程，接管时的进度再高也压到 0.25 以下 —— 不依赖接管那一刻的进度。
    states = await drag_page(page, canvas, -0.9)
    if states[0]["progress"] > 0.35:
        fail(f"反着拖的时候进度不该涨，实际 {states[0]['progress']:.2f} —— 方向映射反了")
    await page.wait_for_timeout(1500)
    await expect_screen(page, (6, 7), "反着拖松手")
    ok("反着拖 90% 个行程松手 → 弹回 [6,7]（方向映射没反）")

    # ---- ④ 几何不泄漏：连翻 6 屏，几何数必须一模一样
    #
    # 漏 dispose 的话这个数会一路涨，而画面完全正常、不报错 —— 只有反复开关预览
    # 攒到上下文丢失才暴露。纹理数会涨（页面在陆续加载），所以只钉几何。
    # 弯折搬进顶点着色器之后几何里没有逐版式的东西了，`layout()` 只改 scale、不再
    # 重建 —— 所以这个数现在是**全程恒定**的（连换版式都不变），比以前更死。
    first = await page.evaluate(STATE)
    counts = [first["geometries"]]
    tex_counts = [first["textures"]]
    for _ in range(6):
        await next_btn.click()
        await page.wait_for_timeout(1200)
        state = await page.evaluate(STATE)
        counts.append(state["geometries"])
        tex_counts.append(state["textures"])
    if len(set(counts)) != 1:
        fail(f"连翻 6 屏几何数变了 —— 有几何没被释放：{counts}")
    await expect_screen(page, (18, 19), "连翻 6 屏之后")
    ok(f"连翻 6 屏几何数稳定在 {counts[0]}（无泄漏）")

    # ---- ④b 纹理滑动窗口：翻过一段之后不能再跟着页码涨
    #
    # 这个 Map 原本**只增不减**，换版式那处的 `clear()` 连 dispose 都没做 —— 25 页
    # 翻一遍攒下约 170MB 显存，反复开关预览一路涨到上下文丢失，而画面全程正常。
    # 窗口生效的话，翻过半径之后纹理数就到稳态。这里多翻了 6 屏、页码走了 12 页，
    # 远超窗口，所以末值必须落在上界内。
    if tex_counts[-1] > TEXTURE_CEILING:
        fail(
            f"翻过 6 屏后纹理数是 {tex_counts[-1]}，超过上界 {TEXTURE_CEILING}"
            f" —— 滑动窗口没生效：{tex_counts}"
        )
    ok(f"翻过 6 屏纹理数 {tex_counts[0]} → {tex_counts[-1]}（上界 {TEXTURE_CEILING}）")

    # ---- ⑤ 翻页动画还在跑的时候再点一下 → 那次点击不能丢，也不能多翻
    #
    # 走的是 `onPointerDown` 里 `deferred` 那条路：flip 存在时按下**不立刻** emit
    #（否则拖动松手时 finishFlip 又链式执行 pending，一次拖动翻两屏），
    # 等抬手还没越过 slop 才补发。连点两下应该正好前进两屏。
    for _ in range(2):
        await click_half(page, canvas, right=True)
    await page.wait_for_timeout(3000)
    await expect_screen(page, (22, 23), "连点两下")
    ok("翻页动画中再点一下不丢也不多（[18,19] 连点两下 → [22,23]）")

    await page.close()


async def check_reduced_motion(p, errors: list[str]) -> None:
    """系统开了"减弱动态效果"：不飞入、不掀封面，翻页直接换。

    **这条必须自己 `emulate_media`。** headless 默认是 `no-preference`，不设的话
    跑的是普通路径、`reducedMotion` 恒为 false —— 也就是说没这条断言的话，那个
    分支是"写完就从没被执行过"的代码，而它恰恰是给最需要它的人用的。

    翻页要挑**对开 → 对开**那一步（[2,3] → [4,5]）：首屏 [1] → [2,3] 是单页切
    对开，本来就不播动画，拿它测等于在测既有行为。
    """
    book_id = make_book(FIVE_PAGE_STORY, 5, "减弱动效 E2E")
    page = await p.new_page(viewport={"width": 1280, "height": 860})
    page.on("pageerror", lambda e: errors.append(str(e)))
    await page.emulate_media(reduced_motion="reduce")
    # **必须逐帧记录，不能"等一会儿再读一次"。** 进场只有 1.5 秒，而等画布挂上
    # 本身就要几百毫秒，`wait_for` 加一个固定等待之后进场早跑完了 —— 那时 `intro`
    # 恒为 'none'，断言在任何实现下都是绿的（实测过：把 `reducedMotion` 写死成
    # false 让它照常播进场，这条断言照样绿）。
    await page.add_init_script("""
      window.__intros = [];
      const tick = () => {
        const c = document.querySelector('.stage3d canvas');
        const s = c && c.__flipbook3d;
        if (s && window.__intros[window.__intros.length - 1] !== s.intro) {
          window.__intros.push(s.intro);
        }
        requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    """)
    await page.goto(f"{UI}/books/{book_id}", wait_until="networkidle")
    await page.get_by_role("button", name="翻页预览").click()
    await page.locator(".stage3d canvas").wait_for(timeout=15000)
    await page.wait_for_timeout(2000)  # 比进场（1.5s）长，确保该播的早就播了

    intros = await page.evaluate("() => window.__intros")
    if not intros:
        fail("一直没读到 3D 状态，画布可能没挂上")
    if any(seen != "none" for seen in intros):
        fail(f"开了减弱动态效果还在播进场：出现过 {intros}")
    ok(f"减弱动态效果：进场整个跳过（intro 全程只有 none，不飞入也不掀封面）")

    nxt = page.get_by_role("button", name="下一屏 →")
    await nxt.click()
    await page.wait_for_timeout(600)
    await expect_screen(page, (2, 3), "减弱动态效果下走到第一屏对开")

    # 这一步在普通模式下**会**播翻页动画（对开 → 对开）。逐帧盯 720ms（FLIP_MS）
    # 那么久，出现任何一次 flipMode 就说明动画还是播了。
    await nxt.click()
    saw_flip = None
    for _ in range(24):
        mode = (await page.evaluate(STATE))["flipMode"]
        if mode is not None:
            saw_flip = mode
        await page.wait_for_timeout(30)
    if saw_flip is not None:
        fail(f"开了减弱动态效果还在播翻页动画：flipMode={saw_flip}")
    await expect_screen(page, (4, 5), "减弱动态效果下对开翻对开")
    ok("减弱动态效果：翻页直接换（全程没出现过 flipMode），页码照走")
    await page.close()


async def check_preview_owns_keyboard(p, errors: list[str]) -> None:
    """全屏预览开着时，键盘归预览 —— 底下的编辑器不许跟着动。

    `FlipBook` 和 `EditStep` 各自在 `window` 上挂了 keydown，**两边都吃左右方向键**，
    而 `preventDefault()` 拦不住另一个监听器（它只取消浏览器默认行为）。实测过一次
    `ArrowRight`：计数器从「第 1 / 5 页」翻到「第 2-3 页」，**同一个块**的 `xMm` 也从
    0 变成了 1。不报错、不弹提示，用户只会觉得"块自己动了"，而且那一下还进了撤销栈。

    必须用**自由页**才有得测：预设页上方向键本来就不做任何事（`if (!freePage) return`），
    拿预设页测等于什么都没测。
    """
    book_id = make_book(FIVE_PAGE_STORY, 5, "预览抢键 E2E")
    with httpx.Client(base_url=API, timeout=30) as c:
        c.post(f"/books/{book_id}/pages/1/to-free").raise_for_status()
        before = [b["xMm"] for b in c.get(f"/books/{book_id}").json()["pages"][0]["blocks"]]

    page = await p.new_page(viewport={"width": 1400, "height": 900})
    page.on("pageerror", lambda e: errors.append(str(e)))
    await page.goto(f"{UI}/books/{book_id}", wait_until="networkidle")
    await page.get_by_text("排版", exact=True).click()
    await page.wait_for_selector(".rail-item", timeout=15000)
    await page.locator(".rail-item").nth(0).click()
    await page.wait_for_selector(".block-layer .block", timeout=15000)

    # 必须**真的选中**一个块：没选中时 EditStep 那个监听器自己就提前返回了，
    # 这条断言会绿，而 bug 原封不动还在
    await page.locator(".block-layer .block").first.click()
    if await page.locator(".block-layer .block.selected").count() != 1:
        fail("点了一下没选中块 —— 这条断言测不出东西")

    await page.get_by_role("button", name="翻页预览").click()
    await page.wait_for_timeout(2500)
    await page.keyboard.press("ArrowRight")
    await page.wait_for_timeout(1500)
    if "第 2-3 页" not in await page.locator(".flipbook-dialog .counter").inner_text():
        fail(f"预览里按右方向键没翻页：{await page.locator('.flipbook-dialog .counter').inner_text()}")

    with httpx.Client(base_url=API, timeout=30) as c:
        after = [b["xMm"] for b in c.get(f"/books/{book_id}").json()["pages"][0]["blocks"]]
    moved = [(i, a, b) for i, (a, b) in enumerate(zip(before, after)) if abs(a - b) > 1e-6]
    if moved:
        fail(f"预览开着时按方向键把底下的块挪了：{moved} —— 编辑器不该抢键")
    ok("预览开着时方向键只翻页，底下的块一动不动")

    await page.close()


async def check_overflow(p, errors: list[str]) -> None:
    book_id = make_book(LONG_STORY, 1, "溢出提示 E2E")
    page = await p.new_page(viewport={"width": 1600, "height": 1000})
    page.on("pageerror", lambda e: errors.append(str(e)))

    await page.goto(f"{UI}/books/{book_id}", wait_until="networkidle")
    await page.get_by_text("排版", exact=True).click()
    await page.wait_for_selector(".rail-item", timeout=15000)
    # 等 fit-text.js 跑完一轮（它要等 document.fonts.ready 之后才定稿）
    await page.wait_for_timeout(2500)

    await page.locator(".overflow-notice").wait_for(timeout=15000)
    ok("文字放不下时，编辑页弹出了溢出提示")

    # 确认 iframe 里确实被标了溢出（提示不是误报）
    frame = page.frame_locator(".frame")
    if await frame.locator(".has-overflow").count() == 0:
        fail("父窗口报了溢出，但 iframe 里没有任何 .has-overflow —— 提示是误报")
    ok("iframe 里确实有 .has-overflow 标记，与提示一致")

    await page.close()


async def main() -> None:
    from playwright.async_api import async_playwright

    errors: list[str] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        check_shot_endpoint()
        await check_flipbook(browser, errors)
        await check_flipbook_3d(browser, errors)
        await check_flipbook_3d_drag(browser, errors)
        await check_reduced_motion(browser, errors)
        await check_preview_owns_keyboard(browser, errors)
        await check_overflow(browser, errors)

        if errors:
            fail(f"页面有 JS 报错: {errors[:3]}")
        ok("全程无 JS 报错")

        await browser.close()

    print("\n全部通过。")


if __name__ == "__main__":
    asyncio.run(main())
