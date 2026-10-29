<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'

import type { Block } from '@/types/book'

/**
 * 覆盖在预览 iframe 上的拖拽层。
 *
 * 它和 iframe 处在**同一个**被 scale 的容器里（见 PagePreview 的插槽），
 * 所以这里的坐标就是纯 mm→px，不用管缩放。反过来说：鼠标位移是屏幕像素，
 * 换算成 mm 时要除以 scale，这个除法集中在 `mmPerPx` 一处。
 *
 * 拖拽过程中**只改本地 draft**，不发请求。指针松开才 emit 一次 change，
 * 由父组件落库。否则一次拖动会打出上百个 PATCH。
 *
 * ---------------------------------------------------------------- 预设页
 *
 * 预设页没有块（位置由 CSS grid/flex 决定，没有可拖的坐标），这时这一层退化成
 * 一块**整页可抓的垫子**：抓住拖一下，就把这一页展开成自由拖拽，并**把这一次
 * 手势接过去**继续用。
 *
 * 为什么要把手势接过去：转换是异步的，不接的话用户得拖两下 —— 第一下只完成
 * 转换、页面纹丝不动，看起来就是"拖不动"。所以：
 *
 *   pointerdown  记下起点，等位移越过阈值（不然"点一下看看"也会把模式改掉）
 *   pointermove  越过阈值才发起转换，之后一直累计位移
 *   转换完成     拿**起点**去命中新展开出来的块 —— 预设与自由两种渲染在视觉上
 *                是等价的（有实测的等价性测试守着），所以拿起点判定是准的
 *   pointerup    把累计位移作为一次 PATCH 提交
 *
 * 选"展开成自由拖拽"而不是给预设页加个偏移量：展开之后这一页就拥有了整套自由
 * 排版能力 —— 拖位置、八个手柄改大小、旋转、逐块样式，用户要的"放大缩小"自然
 * 就有了，不用再为预设页单独做一套半吊子的缩放。
 */

const PX_PER_MM = 96 / 25.4
/** 块的最小边长，避免拖成负宽高。 */
const MIN_MM = 5
/** 拖动时至少留这么多 mm 在页面内，防止把块甩到看不见的地方再也选不中。 */
const KEEP_VISIBLE_MM = 12
/** 存盘前取整到 0.1mm，免得 JSON 里全是 123.45678901 这种浮点噪声。 */
const PRECISION = 10

/** 位移超过这么多像素才算"拖动"，否则只当作点击。 */
const CONVERT_THRESHOLD_PX = 4

const props = defineProps<{
  blocks: Block[]
  selectedId: string | null
  pageWidthMm: number
  pageHeightMm: number
  /** 与 PagePreview 的缩放一致。鼠标位移要按它折算回 mm。 */
  scale: number
  disabled?: boolean
  /**
   * 把当前页展开成自由拖拽，返回展开后的块（失败返回 null）。
   *
   * 只在**没有块**（预设页）且用户真的开始拖时才调用 —— 传函数而不是 emit，
   * 是因为这里必须 await 它拿到块去做命中判定，emit 拿不到返回值。
   */
  convertPage?: () => Promise<Block[] | null>
}>()

const emit = defineEmits<{
  'update:selectedId': [string | null]
  /** 拖拽/缩放结束。payload 只含几何字段，父组件直接 PATCH。 */
  change: [string, BoxPatch]
}>()

interface BoxPatch {
  xMm?: number
  yMm?: number
  widthMm?: number
  heightMm?: number
}

interface Box {
  id: string
  type: 'text' | 'image'
  xMm: number
  yMm: number
  widthMm: number
  heightMm: number
  rotationDeg: number
  zIndex: number
  locked: boolean
}

/** 拖拽中的临时几何，覆盖在 props.blocks 之上。 */
const draft = ref<Record<string, BoxPatch>>({})

const boxes = computed<Box[]>(() =>
  props.blocks.map((b) => {
    const d = draft.value[b.id]
    return {
      id: b.id,
      type: b.type,
      xMm: d?.xMm ?? b.xMm,
      yMm: d?.yMm ?? b.yMm,
      widthMm: d?.widthMm ?? b.widthMm,
      heightMm: d?.heightMm ?? b.heightMm,
      rotationDeg: b.rotationDeg,
      zIndex: b.zIndex,
      locked: b.locked,
    }
  }),
)

// ---------------------------------------------------------------- 几何

const round = (v: number) => Math.round(v * PRECISION) / PRECISION

/** 屏幕像素 → mm。scale 为 0 时（容器还没量出来）退化成不缩放。 */
const mmPerPx = computed(() => 1 / (PX_PER_MM * (props.scale || 1)))

function clampAxis(value: number, size: number, pageMm: number): number {
  // 允许块超出页面（满版图要靠它），但至少留 KEEP_VISIBLE_MM 在页内
  const min = KEEP_VISIBLE_MM - size
  const max = pageMm - KEEP_VISIBLE_MM
  return Math.min(Math.max(value, min), max)
}

function moveBox(origin: BoxPatch & Required<BoxPatch>, dx: number, dy: number): BoxPatch {
  return {
    xMm: round(clampAxis(origin.xMm + dx, origin.widthMm, props.pageWidthMm)),
    yMm: round(clampAxis(origin.yMm + dy, origin.heightMm, props.pageHeightMm)),
    widthMm: origin.widthMm,
    heightMm: origin.heightMm,
  }
}

function resizeBox(
  origin: Required<BoxPatch>,
  dir: string,
  dx: number,
  dy: number,
): BoxPatch {
  let { xMm: x, yMm: y, widthMm: w, heightMm: h } = origin

  if (dir.includes('e')) w += dx
  if (dir.includes('s')) h += dy
  if (dir.includes('w')) {
    x += dx
    w -= dx
  }
  if (dir.includes('n')) {
    y += dy
    h -= dy
  }

  // 撞到最小尺寸时钉住对面的边，否则块会在触底后继续往反方向爬
  if (w < MIN_MM) {
    if (dir.includes('w')) x = origin.xMm + origin.widthMm - MIN_MM
    w = MIN_MM
  }
  if (h < MIN_MM) {
    if (dir.includes('n')) y = origin.yMm + origin.heightMm - MIN_MM
    h = MIN_MM
  }

  return { xMm: round(x), yMm: round(y), widthMm: round(w), heightMm: round(h) }
}

// ---------------------------------------------------------------- 交互

interface Drag {
  id: string
  /** 'move' 或八个方位之一：n/s/e/w 的组合 */
  mode: string
  startX: number
  startY: number
  origin: Required<BoxPatch>
}

let drag: Drag | null = null

function onPointerDown(e: PointerEvent, box: Box, mode: string) {
  if (props.disabled || box.locked) return
  // 阻止默认行为，否则拖动时会顺手选中页面上的文字
  e.preventDefault()
  e.stopPropagation()

  emit('update:selectedId', box.id)

  ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
  drag = {
    id: box.id,
    mode,
    startX: e.clientX,
    startY: e.clientY,
    origin: {
      xMm: box.xMm,
      yMm: box.yMm,
      widthMm: box.widthMm,
      heightMm: box.heightMm,
    },
  }
}

function onPointerMove(e: PointerEvent) {
  if (!drag) return
  const dx = (e.clientX - drag.startX) * mmPerPx.value
  const dy = (e.clientY - drag.startY) * mmPerPx.value
  const next =
    drag.mode === 'move'
      ? moveBox(drag.origin, dx, dy)
      : resizeBox(drag.origin, drag.mode, dx, dy)
  draft.value = { ...draft.value, [drag.id]: next }
}

function onPointerUp() {
  if (!drag) return
  const { id } = drag
  drag = null

  const patch = draft.value[id]
  const { [id]: _dropped, ...rest } = draft.value
  draft.value = rest

  // 父组件收到后先同步改本地状态再发请求，所以这里清掉 draft 不会闪回原位
  if (patch) emit('change', id, patch)
}

// ---------------------------------------------------------------- 预设页：整页抓取

interface ConvertDrag {
  /** 起点在页面内的像素坐标（相对覆盖层左上角），用来命中块。 */
  startPageX: number
  startPageY: number
  /** 起点的屏幕坐标，位移都相对它算。 */
  startX: number
  startY: number
  lastX: number
  lastY: number
  /** 已经发起转换，别重复发。 */
  converting: boolean
  /** 指针还按着吗 —— 松手早于转换完成时要补交。 */
  down: boolean
  /**
   * 转换完成后命中的块，**连同它当时的几何一起存下来**。
   *
   * 不回头去 props.blocks 里找：转换返回时组件可能还没重渲染，那一刻 props
   * 还是空的。存下来就不用管渲染和 await 谁先谁后。
   */
  adopted: { id: string; xMm: number; yMm: number; widthMm: number; heightMm: number } | null
}

let convertDrag: ConvertDrag | null = null

/** 跟着鼠标走的虚框，让用户看清这一下抓住的是哪一块。 */
const ghost = ref<{ xMm: number; yMm: number; widthMm: number; heightMm: number } | null>(null)

const layer = ref<HTMLElement | null>(null)

/** 覆盖层左上角为原点的页面内像素坐标。 */
function toPagePx(e: PointerEvent) {
  const rect = layer.value?.getBoundingClientRect()
  if (!rect) return { x: 0, y: 0 }
  return { x: e.clientX - rect.left, y: e.clientY - rect.top }
}

/** 这一路走了多远（mm）。 */
function convertDelta(g: ConvertDrag) {
  const k = mmPerPx.value
  return { dx: (g.lastX - g.startX) * k, dy: (g.lastY - g.startY) * k }
}

/** 起点落在哪一块上。重叠时取 zIndex 最大的那块 —— 也就是用户看见的那块。 */
function hitTest(blocks: Block[], pageX: number, pageY: number) {
  const xMm = pageX / PX_PER_MM
  const yMm = pageY / PX_PER_MM
  return (
    blocks
      .filter(
        (b) => xMm >= b.xMm && xMm <= b.xMm + b.widthMm && yMm >= b.yMm && yMm <= b.yMm + b.heightMm,
      )
      .sort((a, b) => b.zIndex - a.zIndex)[0] ?? null
  )
}

function drawGhost() {
  const g = convertDrag
  if (!g?.adopted) return
  const { dx, dy } = convertDelta(g)
  ghost.value = {
    xMm: g.adopted.xMm + dx,
    yMm: g.adopted.yMm + dy,
    widthMm: g.adopted.widthMm,
    heightMm: g.adopted.heightMm,
  }
}

/** 拆掉手势、摘掉 window 监听。不提交任何东西。 */
function teardownConvertDrag() {
  window.removeEventListener('pointermove', onConvertMove)
  window.removeEventListener('pointerup', onConvertUp)
  window.removeEventListener('pointercancel', onConvertUp)
  convertDrag = null
  ghost.value = null
}

/** 收尾并把这一下的位移提交出去。 */
function endConvertDrag() {
  const g = convertDrag
  if (!g) return
  teardownConvertDrag()

  if (!g.adopted) return
  const { dx, dy } = convertDelta(g)
  emit('change', g.adopted.id, {
    xMm: round(g.adopted.xMm + dx),
    yMm: round(g.adopted.yMm + dy),
  })
}

async function runConvert(g: ConvertDrag) {
  const blocks = await props.convertPage?.()
  // 转换期间手势可能已经作废了（切了页、组件卸载）
  if (convertDrag !== g) return

  const hit = blocks?.length ? hitTest(blocks, g.startPageX, g.startPageY) : null
  if (!hit) {
    // 起点没落在任何块上（比如点在页边留白）—— 页面已经转好了，但这一下不挪东西
    endConvertDrag()
    return
  }

  g.adopted = { id: hit.id, xMm: hit.xMm, yMm: hit.yMm, widthMm: hit.widthMm, heightMm: hit.heightMm }
  // 选中它，转换完成后八个缩放手柄就直接在手上 —— 用户要的"放大缩小"在这里
  emit('update:selectedId', hit.id)
  drawGhost()
  // 松手早于转换完成：这会儿才轮到我们收尾
  if (!g.down) endConvertDrag()
}

function onConvertMove(e: PointerEvent) {
  const g = convertDrag
  if (!g) return
  g.lastX = e.clientX
  g.lastY = e.clientY
  // 越过阈值才真的转换：不然"点一下看看"也会把页面的模式改掉
  if (!g.converting && Math.hypot(e.clientX - g.startX, e.clientY - g.startY) >= CONVERT_THRESHOLD_PX) {
    g.converting = true
    void runConvert(g)
  }
  drawGhost()
}

function onConvertUp() {
  const g = convertDrag
  if (!g) return
  g.down = false
  // 转换还在路上就交给 runConvert 收尾，这里不能提前拆掉手势
  if (!g.converting || g.adopted) endConvertDrag()
}

function onLayerPointerDown(e: PointerEvent) {
  // 点在空白处（不是某个块）→ 取消选中
  if (e.target !== e.currentTarget) return
  emit('update:selectedId', null)

  // 预设页（没有块）上没别的可抓，整页就是把手。自由页的空白处不接管 ——
  // 那种页面上"拖空白"没有任何合理语义，抢过来只会妨碍取消选中。
  if (!props.convertPage || props.disabled || props.blocks.length || convertDrag) return
  e.preventDefault()
  // 抓住指针：转换期间鼠标很容易被甩到窗口外或侧栏上，没捕获就会丢掉
  // pointerup，手势挂在那里不动。松手时浏览器自动释放，不用手动 release。
  layer.value?.setPointerCapture(e.pointerId)
  const p = toPagePx(e)
  convertDrag = {
    startPageX: p.x,
    startPageY: p.y,
    startX: e.clientX,
    startY: e.clientY,
    lastX: e.clientX,
    lastY: e.clientY,
    converting: false,
    down: true,
    adopted: null,
  }
  window.addEventListener('pointermove', onConvertMove)
  window.addEventListener('pointerup', onConvertUp)
  window.addEventListener('pointercancel', onConvertUp)
}

// 卸载（切页、退出编辑）时手势还在：直接拆掉，不提交 —— 那一下本来就没落定
onBeforeUnmount(() => {
  if (convertDrag) teardownConvertDrag()
})

// ---------------------------------------------------------------- 渲染

const HANDLES = [
  { dir: 'nw', left: '0%', top: '0%', cursor: 'nwse-resize' },
  { dir: 'n', left: '50%', top: '0%', cursor: 'ns-resize' },
  { dir: 'ne', left: '100%', top: '0%', cursor: 'nesw-resize' },
  { dir: 'e', left: '100%', top: '50%', cursor: 'ew-resize' },
  { dir: 'se', left: '100%', top: '100%', cursor: 'nwse-resize' },
  { dir: 's', left: '50%', top: '100%', cursor: 'ns-resize' },
  { dir: 'sw', left: '0%', top: '100%', cursor: 'nesw-resize' },
  { dir: 'w', left: '0%', top: '50%', cursor: 'ew-resize' },
] as const

const boxStyle = (b: Box) => ({
  left: `${b.xMm * PX_PER_MM}px`,
  top: `${b.yMm * PX_PER_MM}px`,
  width: `${b.widthMm * PX_PER_MM}px`,
  height: `${b.heightMm * PX_PER_MM}px`,
  transform: `rotate(${b.rotationDeg}deg)`,
  zIndex: String(b.zIndex + 1),
})

/**
 * 选中框和手柄要按 1/scale 反向缩放，否则开本缩到 40% 时手柄只有 3px，
 * 根本点不中。描边宽度同理。
 */
const invScale = computed(() => 1 / (props.scale || 1))
const outlineWidth = computed(() => `${invScale.value}px`)

const handleStyle = (h: (typeof HANDLES)[number]) => ({
  left: h.left,
  top: h.top,
  cursor: h.cursor,
  transform: `translate(-50%, -50%) scale(${invScale.value})`,
})

/** 预设页抓取时的跟随虚框。 */
const ghostStyle = computed(() => {
  const g = ghost.value
  if (!g) return undefined
  return {
    left: `${g.xMm * PX_PER_MM}px`,
    top: `${g.yMm * PX_PER_MM}px`,
    width: `${g.widthMm * PX_PER_MM}px`,
    height: `${g.heightMm * PX_PER_MM}px`,
  }
})
</script>

<template>
  <div
    ref="layer"
    class="block-layer"
    :class="{ 'can-grab': !!convertPage && blocks.length === 0 }"
    @pointerdown="onLayerPointerDown"
  >
    <div
      v-for="b in boxes"
      :key="b.id"
      class="block"
      :class="[`is-${b.type}`, { selected: b.id === selectedId, locked: b.locked }]"
      :style="boxStyle(b)"
      :data-block-id="b.id"
      @pointerdown="onPointerDown($event, b, 'move')"
      @pointermove="onPointerMove"
      @pointerup="onPointerUp"
      @pointercancel="onPointerUp"
    >
      <span class="tag">
        {{ b.type === 'text' ? '文字' : '图片' }}
        <template v-if="b.locked">· 已锁定</template>
      </span>

      <template v-if="b.id === selectedId && !b.locked && !disabled">
        <span
          v-for="h in HANDLES"
          :key="h.dir"
          class="handle"
          :style="handleStyle(h)"
          @pointerdown.stop="onPointerDown($event, b, h.dir)"
          @pointermove="onPointerMove"
          @pointerup="onPointerUp"
          @pointercancel="onPointerUp"
        />
      </template>
    </div>

    <!-- 预设页抓取时跟着鼠标走的虚框：转换是异步的，没有它这一段就是"拖了没反应" -->
    <div v-if="ghostStyle" class="ghost" :style="ghostStyle" />
  </div>
</template>

<style scoped>
.block-layer {
  position: absolute;
  inset: 0;
  /*
    必须接管指针事件：下面的 iframe 是 pointer-events: none，没人会跟它抢。
    空白区域也要能收到 pointerdown，否则点空白取消选中就失效了。
  */
  pointer-events: auto;
}

/* 预设页：整页可抓。cursor 是这里唯一能告诉用户"这块能拖"的东西 */
.block-layer.can-grab {
  cursor: grab;
  touch-action: none;
}

.block-layer.can-grab:active {
  cursor: grabbing;
}

.ghost {
  position: absolute;
  box-sizing: border-box;
  border: v-bind(outlineWidth) dashed #409eff;
  background: rgb(64 158 255 / 8%);
  pointer-events: none;
}

.block {
  position: absolute;
  box-sizing: border-box;
  outline: v-bind(outlineWidth) dashed rgb(64 158 255 / 55%);
  cursor: move;
  touch-action: none;
}

.block.selected {
  outline: v-bind(outlineWidth) solid #409eff;
}

.block.locked {
  cursor: not-allowed;
  outline-color: rgb(144 147 153 / 60%);
}

.tag {
  position: absolute;
  top: 0;
  left: 0;
  padding: 1px 4px;
  font-size: 10px;
  line-height: 1.5;
  color: #fff;
  background: rgb(64 158 255 / 85%);
  pointer-events: none;
  white-space: nowrap;
}

.block.locked .tag {
  background: rgb(144 147 153 / 85%);
}

.handle {
  position: absolute;
  width: 9px;
  height: 9px;
  background: #fff;
  border: v-bind(outlineWidth) solid #409eff;
  box-sizing: border-box;
}
</style>
