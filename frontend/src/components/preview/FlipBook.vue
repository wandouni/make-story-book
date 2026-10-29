<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'

import PagePreview from '@/components/editor/PagePreview.vue'
import FlipBook3D from '@/components/preview/FlipBook3D.vue'
import { generateCover } from '@/api/illustrate'
import {
  COVER_CANVAS_H,
  COVER_CANVAS_W,
  coverBgUrl,
  paintCover,
} from '@/components/preview/coverArt'
import { useJobStream } from '@/composables/useJobStream'
import { PX_PER_MM } from '@/composables/useMmScale'
import { previewOpen } from '@/composables/previewOpen'

/**
 * 整本翻页预览。
 *
 * 直接复用 PagePreview —— 它指向的是后端的渲染端点，也就是导出 PDF 时
 * Playwright 打开的同一份 HTML。所以这里翻到的每一页，印出来就是这个样子。
 *
 * 切换"印刷版"会连**出血**一起显示（页面四周多出 3mm），这是唯一能在导出前
 * 看见成品边界的地方 —— 满版图和叠字到底有没有铺到位，看这里最直接。
 *
 * 对开（左右两页）是**纯前端**的两个 iframe 并排：后端仍然是逐页渲染，
 * 和 PDF 逐页导出的形态一致。中缝那条 6mm 在含出血模式下正是两页各自的
 * 出血带，不是"多出来的边"——所以那个模式下不画书脊阴影，见下面的 CSS。
 */
const props = defineProps<{
  modelValue: boolean
  bookId: string
  pageCount: number
  trimWidthMm: number
  trimHeightMm: number
  bleedMm: number
  /** 只给 3D 视图画封面用（平面视图是逐页截图，用不上）。 */
  bookTitle?: string
  bookAuthor?: string
  /** 封面书名的字体，同样是只给 3D 用。见 FlipBook3D 的这两个 prop。 */
  fontFamily?: string
  fontUrl?: string
  /** 已有 AI 封面背景时传入；没有则打开预览会自动生成一张。 */
  coverImageId?: string
}>()

const emit = defineEmits<{
  'update:modelValue': [boolean]
  /** AI 封面生成完成，把新 imageId 交给父组件写回书数据。 */
  coverGenerated: [string]
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

/**
 * 当前页，1-based。**对开模式下它仍然是页码而不是屏序号** —— 这样切换
 * 单页/对开时读者的位置不会丢（在第 3 页切到对开看到 [2,3]，切回单页还在
 * 第 3 页）。屏序号由它派生，见下面几个 computed。
 */
const current = ref(1)
/** 预览版 = 裁切后的成品；印刷版 = 带出血的那份。 */
const mode = ref<'preview' | 'print'>('preview')
/**
 * 平面 = 两个真 iframe（保真，用来核对细节）；3D = WebGL 翻页（看整体观感）。
 *
 * 默认 3D：3D 现在是第一眼看到的那个"像一本书"的画面 —— 有厚度、有书脊、有影子，
 * 进场会掀封面、翻页可以按住拖。平面视图仍然在开关里、仍然更锐利（"预览永不跑版"
 * 这条保证落在它身上），核对排版细节时切过去，但那是第二眼的事。
 */
const view = ref<'flat' | '3d'>('flat')

const coverJob = useJobStream()

// ---------------------------------------------------------------- 屏

/** 当前屏的左页。封面（第 1 页）单独一屏；之后 3→2、5→4 地两两成组。 */
const leftPage = computed(() => {
  const n = Math.min(Math.max(current.value, 1), Math.max(props.pageCount, 1))
  if (n <= 1) return n
  return n - (n % 2)
})

/** 右页。落单（封面、末页）时为 null。 */
const rightPage = computed(() => {
  // 封面独占一屏 —— 第 1 页的右边不配页，否则会变成 [1,2] 这种"封面和第一页
  // 并排"，跟用户选定的"封面单独"完全不是一回事
  if (leftPage.value <= 1) return null
  const next = leftPage.value + 1
  return next <= props.pageCount ? next : null
})

/** 真的并排两页时才画中缝。 */
const isSpread = computed(() => rightPage.value !== null)
const shownPages = computed(() => (isSpread.value ? 2 : 1))

/**
 * 屏序号 / 总屏数。单页模式下退化成"页码 / 总页数"，于是翻页与按钮的禁用
 * 判断两边共用同一套，不用按模式分叉。
 *
 * 8 页 → 屏 [1] [2,3] [4,5] [6,7] [8]，共 1 + ceil(7/2) = 5 屏。
 */
const screenIndex = computed(() =>
  leftPage.value > 1 ? leftPage.value / 2 + 1 : leftPage.value,
)
const screenCount = computed(() =>
  props.pageCount > 1 ? 1 + Math.ceil((props.pageCount - 1) / 2) : props.pageCount,
)

const counterText = computed(() => {
  const right = rightPage.value
  if (!right) return `第 ${leftPage.value} / ${props.pageCount} 页`
  return `第 ${leftPage.value}-${right} 页 / 共 ${props.pageCount} 页`
})

function step(delta: number) {
  const target = screenIndex.value + delta
  if (target < 1 || target > screenCount.value) return
  // 屏 → 页码：封面是第 1 页，第 k 屏（k ≥ 2）的左页是 2(k-1)
  current.value = target > 1 ? 2 * (target - 1) : target
}

// ---------------------------------------------------------------- 尺寸与缩放

const pageSize = computed(() => {
  const bleed = mode.value === 'print' ? props.bleedMm : 0
  return {
    widthMm: props.trimWidthMm + 2 * bleed,
    heightMm: props.trimHeightMm + 2 * bleed,
  }
})

/** 舞台四周的留白。对开时**只算一次** —— 两个 PagePreview 各自的 16px
 *  会在中缝叠成 32px，中缝就比真实书脊宽了一倍。 */
const STAGE_PADDING = 16

const stage = ref<HTMLElement | null>(null)
const stageW = ref(0)
const stageH = ref(0)

const leftPane = ref<InstanceType<typeof PagePreview> | null>(null)
const rightPane = ref<InstanceType<typeof PagePreview> | null>(null)

// ---- 平面首屏封面
/** 平面首屏要显示真正的封面（AI 背景+书名作者），而不是第 1 页内容。 */
const coverCanvas = ref<HTMLCanvasElement | null>(null)
let coverBgImg: HTMLImageElement | null = null
let coverBgSrc = ''
let coverFontReady = false

/** 封面字体是异步的，加载完重画一次（否则第一眼是系统字体那版）。 */
function ensureCoverFont() {
  if (coverFontReady) return
  const family = props.fontFamily || 'Noto Serif SC'
  const url = props.fontUrl || '/api/assets/fonts/noto-serif-sc-700.woff2'
  new FontFace(family, `url(${url})`, { weight: '700' })
    .load()
    .then((loaded) => {
      document.fonts.add(loaded)
      coverFontReady = true
      drawFlatCover()
    })
    .catch(() => {
      // 取不到就留系统字体版
    })
}

function drawFlatCover() {
  const canvas = coverCanvas.value
  if (!canvas) return
  ensureCoverFont()
  const ctx = canvas.getContext('2d')!
  paintCover(ctx, COVER_CANVAS_W, COVER_CANVAS_H, {
    title: props.bookTitle ?? '',
    author: props.bookAuthor ?? '',
    fontFamily: props.fontFamily,
    bg: coverBgImg,
  })
}

function loadFlatCoverBg() {
  const imageId = props.coverImageId?.trim()
  if (!imageId) {
    coverBgImg = null
    coverBgSrc = ''
    drawFlatCover()
    return
  }
  const src = coverBgUrl(props.bookId, imageId)
  if (coverBgSrc === src && coverBgImg) {
    // 背景已缓存：直接重画。**这里必须画** —— 封面 canvas 是 v-if，
    // 翻回第一页会重新挂载成空白画布，提前 return 会导致封面全空。
    drawFlatCover()
    return
  }
  coverBgSrc = src
  const img = new Image()
  img.onload = () => {
    if (coverBgSrc !== src) return
    coverBgImg = img
    drawFlatCover()
  }
  img.src = src
  drawFlatCover()
}

const showFlatCover = computed(() => leftPage.value === 1 && rightPage.value === null)

/**
 * 算缩放时按几页算：内容屏固定按 2 页，封面/落单屏按 1 页。
 *
 * 之前恒取 2，导致单页封面只拿到"半屏宽度"算出的 zoom，封面尺寸被算小、
 * 与 wrapper 尺寸对不上，进而顶出滚动条。
 */
const zoomSlots = computed(() => (isSpread.value ? 2 : 1))

/**
 * 两页共用的缩放，由 FlipBook 统一测量后算出。
 *
 * 这是对开能对齐的关键：两个 PagePreview 各自 measure() 的话，容器宽度按
 * flex 分、半像素取整会算出两个不同的 zoom → 两页不等高、中缝错位；更糟的是
 * `.book` 的宽高是由这个 zoom 反推的，PagePreview 再去量这个"已经被缩过的
 * 容器"会形成正反馈自缩，几帧内页面缩成一条线。
 *
 * 传了 zoom 之后 PagePreview 内部的 measure() 结果被忽略
 * （它那边是 `scale = props.zoom ?? fitZoom`），所以反馈环不存在。
 */
const zoom = computed(() => {
  const naturalW = pageSize.value.widthMm * PX_PER_MM
  const naturalH = pageSize.value.heightMm * PX_PER_MM
  const availW = stageW.value - STAGE_PADDING * 2
  const availH = stageH.value - STAGE_PADDING * 2
  // 还没量到尺寸（弹窗刚打开、内容还没挂载）时先给 1，ResizeObserver 一到就纠正
  if (availW <= 0 || availH <= 0) return 1
  // 只缩不放，和 PagePreview 的 fitZoom 是同一条规则
  return Math.min(1, availW / (naturalW * zoomSlots.value), availH / naturalH)
})

/**
 * `.book` 的宽高由 zoom 反推后写成 px，两个格子宽度**精确相等** ——
 * 用 `1fr 1fr` 让 grid 自己分列的话，亚像素取整会把中缝顶开一两个像素。
 */
const bookStyle = computed(() => {
  const w = pageSize.value.widthMm * PX_PER_MM * zoom.value
  const h = pageSize.value.heightMm * PX_PER_MM * zoom.value
  return {
    width: `${w * shownPages.value}px`,
    height: `${h}px`,
    gridTemplateColumns: `repeat(${shownPages.value}, ${w}px)`,
  }
})

function applyPageSize() {
  const { widthMm, heightMm } = pageSize.value
  leftPane.value?.setPageSize(widthMm, heightMm)
  rightPane.value?.setPageSize(widthMm, heightMm)
}

/**
 * 舞台在弹窗里，而 el-dialog 是**懒渲染**的（第一次打开才挂载），所以
 * onMounted 跑的时候 stage 还是 null。用 template ref 的 watcher 才能等到它。
 * 关掉弹窗时 Element Plus 不销毁内容（没开 destroy-on-close），所以这个
 * watcher 实际只跑一次，observer 一直活着。
 */
watch(stage, (el, _old, onCleanup) => {
  if (!el) return
  const sync = () => {
    stageW.value = el.clientWidth
    stageH.value = el.clientHeight
  }
  sync()
  const observer = new ResizeObserver(sync)
  observer.observe(el)
  onCleanup(() => observer.disconnect())
})

// 尺寸、版式、或者"右页有没有挂出来"一变就要重设。
//
// isSpread 必须在监听列表里：右页是 v-if 出来的，翻到 [2,3] 那一屏时它才挂载，
// 而那一刻 pageSize 和 layout 都没变 —— 只看这两个的话新挂出来的右页会一直
// 停在默认的 210×297，iframe 按 A4 铺开再被缩放，看着比左页小一圈、比例也不对
// （实测左 559×794、右 794×1123，正好是 A5 与 A4 的差别）。
//
// nextTick 也是必须的：watch 是 pre-flush，nextTick 回调在整个 flush
// （含子组件的 onMounted）之后执行，那时 rightPane 才拿得到。
watch([pageSize, isSpread], () => nextTick(applyPageSize))

watch(visible, (open) => {
  // 让 EditStep 那个 window keydown 让路（见 previewOpen.ts）
  previewOpen.value = open
  if (!open) return
  current.value = 1
  nextTick(applyPageSize)
  nextTick(loadFlatCoverBg)

  // 没有 AI 封面就自动生成一张：封面是默认首屏，背景要切合故事主题。
  // 3D 组件在图到位前先用布面底色兜底，生成完通过 coverImageId prop 自动换图。
  if (!props.coverImageId) {
    generateCover(props.bookId)
      .then(({ jobId }) => {
        coverJob.watch(jobId, (done) => {
          const imageId = done.items[0]?.imageId
          if (done.status === 'done' && imageId) emit('coverGenerated', imageId)
        })
      })
      .catch(() => {
        // 生图服务不可用等情况：静默留布面封面，不挡住翻书
      })
  }
})

// 封面 canvas 挂载后（以及生图完成后）绘制。coverImageId 变了重新加载背景。
watch(coverCanvas, () => loadFlatCoverBg())
watch(() => props.coverImageId, () => loadFlatCoverBg())

// 书是异步加载的，挂载时开本可能还没到
watch(() => props.trimWidthMm, () => nextTick(applyPageSize))

// 页数变少时把 current 夹回来。弹窗开着的时候基本不会发生，但生图任务结束时
// bookStore.reload() 是异步的，不夹会请求到 404 的页码。
watch(
  () => props.pageCount,
  (count) => {
    if (count > 0 && current.value > count) current.value = count
  },
)

// ---------------------------------------------------------------- 点击与键盘

/**
 * 点舞台的左/右半边翻页 —— 和 3D 视图的手势一致（那边点画布也是这个规则），
 * 用户学一次两边都管用。
 *
 * 平面视图的 iframe 是 `pointer-events: none`（见 PagePreview 里的说明），
 * 点在页面上会穿透到 `.book`、再冒泡到这里，所以不用再铺一层透明垫子。
 * 反过来说，**别去删 iframe 上那行 pointer-events** —— 删了点击会被 iframe
 * 吞掉，现象正是"点左右没反应"。
 */
function onStageClick(event: MouseEvent) {
  // 3D 自己处理 pointerdown（见 FlipBook3D 的 onPointerDown）。这里再算一次的话，
  // 点一下会翻两屏 —— 它那边 emit 上来的和这里冒泡上来的是同一次点击。
  if (view.value !== 'flat') return
  const el = stage.value
  if (!el) return
  const rect = el.getBoundingClientRect()
  if (event.clientX - rect.left > rect.width / 2) step(1)
  else step(-1)
}

/**
 * 只挡**文字录入**元素，不挡所有 `input`。
 *
 * 原来这里是 `closest('input, textarea, [contenteditable="true"]')`，把工具条上那
 * 6 个 `el-radio-button` 也算进去了 —— 它们内层渲染的是真的
 * `<input type="radio" class="el-radio-button__original-radio">`，用户点过任何一个
 * 之后焦点就留在上面，**此后方向键被静默挡掉**。也就是说"方向键翻页"以前是看运气的：
 * 开预览先点过版式开关的人，键是死的。
 *
 * 用白名单而不是"排除 radio/checkbox"：漏掉一个非文字控件（以后加个开关、滑块）
 * 就又是一次静默失灵，而这正是要修的那类 bug。反过来漏掉一种文字框只是那个框里
 * 方向键被抢，代价小得多。
 */
const TEXT_ENTRY = [
  'textarea',
  '[contenteditable="true"]',
  'input:not([type])',
  'input[type="text"]',
  'input[type="search"]',
  'input[type="number"]',
  'input[type="email"]',
  'input[type="password"]',
  'input[type="tel"]',
  'input[type="url"]',
].join(', ')

function onKeydown(event: KeyboardEvent) {
  if (!visible.value) return
  const el = event.target as HTMLElement | null
  if (el?.closest(TEXT_ENTRY)) return

  const dir = event.key === 'ArrowLeft' ? -1 : event.key === 'ArrowRight' ? 1 : 0
  if (!dir) return
  // 弹窗是全屏的，但底下的工作区仍然可滚 —— 不拦的话方向键会连带把它滚走
  event.preventDefault()
  step(dir)
}

window.addEventListener('keydown', onKeydown)
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  // 组件被卸载（比如切走了步骤）时弹窗跟着没了，标志位不能留着 true，
  // 否则 EditStep 的方向键会一直失灵
  previewOpen.value = false
})
</script>

<template>
  <el-dialog
    v-model="visible"
    fullscreen
    :show-close="false"
    class="flipbook-dialog"
    :title="counterText"
  >
    <template #header>
      <div class="bar">
        <el-button-group>
          <el-button :disabled="screenIndex <= 1" @click="step(-1)">
            ← 上一屏
          </el-button>
          <el-button :disabled="screenIndex >= screenCount" @click="step(1)">
            下一屏 →
          </el-button>
        </el-button-group>

        <span class="counter">{{ counterText }}</span>

        <el-radio-group v-model="view" size="small">
          <el-radio-button value="flat">平面</el-radio-button>
          <el-radio-button value="3d">3D</el-radio-button>
        </el-radio-group>

        <el-radio-group v-model="mode" size="small">
          <el-radio-button value="preview">成品</el-radio-button>
          <el-radio-button value="print">含出血</el-radio-button>
        </el-radio-group>

        <span v-if="mode === 'print'" class="bleed-hint">
          每页四周多出 {{ bleedMm }}mm，裁掉后才是成品<template v-if="isSpread">；中缝两侧各一条</template>
        </span>

        <div class="spacer" />

        <el-button text @click="visible = false">关闭</el-button>
      </div>
    </template>

    <div
      ref="stage"
      class="stage"
      :class="{ 'is-3d': view === '3d' }"
      @click="onStageClick"
    >
      <!--
        3D 视图自己管相机和缩放，不吃 stage 的留白和 zoom —— 所以它是
        v-if 单独挂的，不是套在 .book 里面。
      -->
      <FlipBook3D
        v-if="view === '3d'"
        :book-id="bookId"
        :page-count="pageCount"
        :left-page="leftPage"
        :right-page="rightPage"
        :mode="mode"
        :trim-width-mm="trimWidthMm"
        :trim-height-mm="trimHeightMm"
        :bleed-mm="bleedMm"
        :book-title="bookTitle"
        :book-author="bookAuthor"
        :font-family="fontFamily"
        :font-url="fontUrl"
        :cover-image-id="coverImageId"
        @prev="step(-1)"
        @next="step(1)"
      />

      <!--
        平面视图用 v-show 而不是 v-if：切到 3D 再切回来时 iframe 不该重新加载，
        否则每次切回来都白闪一下。宽高由 JS 按 zoom 反推后写在内联样式上，
        这里不要再写 width/height。
      -->
      <div
        v-show="view === 'flat'"
        class="book"
        :class="{ 'is-spread': isSpread, 'is-print': mode === 'print', 'is-cover': showFlatCover }"
        :style="bookStyle"
      >
        <canvas
          v-if="showFlatCover"
          ref="coverCanvas"
          class="cover-canvas"
          :width="768"
          :height="1080"
        />
        <!--
          左边这个实例**一直**是同一个（v-if 只挂在右页上），单页/对开来回切时
          它不会被销毁重建；页码没变的话 iframe 就不会白重载一次。
        -->
        <PagePreview
          v-show="!showFlatCover"
          ref="leftPane"
          class="pane"
          :book-id="bookId"
          :page-number="leftPage"
          :mode="mode"
          :zoom="zoom"
        />
        <PagePreview
          v-if="isSpread"
          ref="rightPane"
          class="pane"
          :book-id="bookId"
          :page-number="rightPage ?? leftPage"
          :mode="mode"
          :zoom="zoom"
        />
      </div>
    </div>
  </el-dialog>
</template>

<style scoped>
.bar {
  display: flex;
  align-items: center;
  gap: 12px;
  /* 多了一组控件，窄屏上宁可挤也不要换行 —— 换行会把弹窗头部撑高，
     下面 body 的高度就少算了 */
  flex-wrap: nowrap;
  white-space: nowrap;
}

.counter {
  font-size: 13px;
  color: #606266;
  /* 文案在"第 8 页"和"第 2-3 页"之间跳，定个最小宽度免得两边按钮跟着抖 */
  min-width: 132px;
}

.bleed-hint {
  font-size: 12px;
  color: #e6a23c;
}

.spacer {
  flex: 1;
}

/* 舞台：自己量尺寸算 zoom，两页共用同一个值。PagePreview 自带的留白和灰底
   被 .pane 覆盖掉，留白全局只算这一次。 */
.stage {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  overflow: hidden;
  background: #eceef1;
  /* 整块舞台都能点左右半边翻页（和 3D 一致），所以光标也得跟着变 ——
     不然这个手势完全靠猜 */
  cursor: pointer;
}

/* 3D 场景自己画背景（暖米色渐变），外面再留一圈灰边就成了两块拼不上的底色 */
.stage.is-3d {
  padding: 0;
}

.book {
  position: relative;
  display: grid;
  /* 白底兜底：万一分列时差了半个像素，中缝露出来的是白纸而不是灰底 */
  background: #fff;
}

/* 干掉 PagePreview 自带的 padding / 底色 / 滚动条。选择器带上 .book 是为了
   压过 .preview-box 自己的 scoped 规则（.book .pane 比 .preview-box 高一级） */
.book .pane {
  padding: 0;
  /* hidden 而不是 auto：缩放即便差 1px 也不该出滚动条。.pane 是 .preview-box，
     需要同时压过它内部的默认 overflow:auto（这里选择器更高一级）。 */
  overflow: hidden !important;
  background: transparent;
}

/* 封面 canvas 占满单页；父级 .book 尺寸已按 zoom 缩放，canvas 用 100% 填充。 */
.cover-canvas {
  width: 100%;
  height: 100%;
  display: block;
}

/* 整本书投一次影。两个 .paper 各投一圈的话，中缝会叠出一条双倍暗带 */
.book.is-spread {
  box-shadow: 0 6px 28px rgb(0 0 0 / 18%);
}

.book.is-spread .pane :deep(.paper) {
  box-shadow: none;
}

/* 书脊：中缝两侧各压一道浅影，折线处留一点亮 —— 纸张在书脊处是拱起来的，
   一路黑到底看着像两页被切开，不像一本书 */
.book.is-spread::after {
  content: '';
  position: absolute;
  inset: 0 auto 0 50%;
  width: 32px;
  transform: translateX(-50%);
  pointer-events: none;
  background: linear-gradient(
    90deg,
    rgb(0 0 0 / 0%) 0%,
    rgb(0 0 0 / 7%) 30%,
    rgb(0 0 0 / 16%) 46%,
    rgb(0 0 0 / 4%) 50%,
    rgb(0 0 0 / 16%) 54%,
    rgb(0 0 0 / 7%) 70%,
    rgb(0 0 0 / 0%) 100%
  );
}

/* 含出血时中缝两侧各有一条 3mm 出血带，正是要盯着看的地方 —— 压渐变等于把它
   糊掉。这里只留一条虚线标出折线位置，出血和裁切标记原样可见。 */
.book.is-spread.is-print::after {
  width: 0;
  background: none;
  border-left: 1px dashed rgb(0 0 0 / 30%);
}
</style>

<style>
/* 弹窗自带的内边距会把预览挤小，这里去掉 —— 预览区自己管留白 */
.flipbook-dialog {
  /* 用 flex 撑满，不写死 calc(100vh - 56px)：工具条多一组控件后一旦换行，
     写死的高度就少算了，预览底部会被切掉（现象是弹窗里多出一条滚动条） */
  display: flex;
  flex-direction: column;
}

.flipbook-dialog .el-dialog__header {
  flex-shrink: 0;
}

.flipbook-dialog .el-dialog__body {
  padding: 0;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}
</style>
