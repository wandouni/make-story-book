<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { PX_PER_MM } from '@/composables/useMmScale'

/**
 * 单页预览。
 *
 * iframe 指向后端的渲染端点 —— 和导出 PDF 时 Playwright 打开的是同一份
 * HTML。所以这里看到的版式就是印出来的版式，不存在"预览好看导出跑版"。
 * 详见 README「架构要点」。
 */
const props = withDefaults(
  defineProps<{
    bookId: string
    pageNumber: number
    mode?: 'preview' | 'print'
    /** 值一变就重新加载 iframe。保存成功后 +1 即可。 */
    reloadKey?: number
    /** 手动缩放；传 null 表示自动适配容器。 */
    zoom?: number | null
  }>(),
  { mode: 'preview', reloadKey: 0, zoom: null },
)

const frame = ref<HTMLIFrameElement | null>(null)
const box = ref<HTMLElement | null>(null)
const pageSize = ref({ widthMm: 210, heightMm: 297 })
const fitZoom = ref(1)
let observer: ResizeObserver | null = null

const src = computed(() => {
  const { bookId, pageNumber, mode, reloadKey } = props
  return `/api/books/${bookId}/render/page/${pageNumber}?mode=${mode}&v=${reloadKey}`
})

/** iframe 按 96dpi 铺开，再用 transform 缩放，避免改 iframe 尺寸触发重排。 */
const frameStyle = computed(() => ({
  width: `${pageSize.value.widthMm * PX_PER_MM}px`,
  height: `${pageSize.value.heightMm * PX_PER_MM}px`,
}))

const scale = computed(() => props.zoom ?? fitZoom.value)

const wrapStyle = computed(() => ({
  width: `${pageSize.value.widthMm * PX_PER_MM * scale.value}px`,
  height: `${pageSize.value.heightMm * PX_PER_MM * scale.value}px`,
}))

function measure() {
  const el = box.value
  if (!el) return
  const w = el.clientWidth - 32
  const h = el.clientHeight - 32
  if (w <= 0 || h <= 0) return

  const naturalW = pageSize.value.widthMm * PX_PER_MM
  const naturalH = pageSize.value.heightMm * PX_PER_MM
  // 只缩不放：小开本放大到糊没有意义，留白更接近真实阅读感
  fitZoom.value = Math.min(1, w / naturalW, h / naturalH)
}

/** 开本尺寸来自父组件，变了要重新算缩放。 */
function setPageSize(widthMm: number, heightMm: number) {
  pageSize.value = { widthMm, heightMm }
  measure()
}

defineExpose({ setPageSize, scale })

onMounted(() => {
  measure()
  observer = new ResizeObserver(measure)
  if (box.value) observer.observe(box.value)
})

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
})

// 换页时 iframe 会重新加载，缩放不用重算（开本没变）
watch(() => props.pageNumber, measure)
</script>

<template>
  <div ref="box" class="preview-box">
    <div class="paper" :style="wrapStyle">
      <iframe
        ref="frame"
        :src="src"
        :style="{ ...frameStyle, transform: `scale(${scale})` }"
        class="frame"
        title="页面预览"
      />
      <!--
        覆盖层与 iframe 在**同一个**被 scale 的容器里，所以它只需要一次
        mm→px 乘法（PX_PER_MM），缩放交给浏览器。若把覆盖层放到容器外面，
        每次拖拽都得乘一遍 scale，且缩放变化时要重算所有块的位置。
      -->
      <div class="overlay-layer">
        <slot :scale="scale" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.preview-box {
  flex: 1;
  min-height: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: auto;
  padding: 16px;
  background: #eceef1;
}

.paper {
  position: relative;
  flex-shrink: 0;
  box-shadow: 0 2px 12px rgb(0 0 0 / 14%);
  background: #fff;
}

.frame {
  border: 0;
  display: block;
  transform-origin: top left;
  /*
    编辑模式下鼠标必须能穿过 iframe 落到覆盖层上。**不要删这行** ——
    删掉之后拖拽会完全失效，而现象是"点了没反应"，很难联想到是 iframe
    把事件吞了。iframe 是只读预览，本来也不需要接收鼠标事件。
  */
  pointer-events: none;
}

.overlay-layer {
  position: absolute;
  inset: 0;
  /*
    这一层本身不收事件，是否接管由插槽内容自己决定（BlockOverlay 的根元素
    设了 pointer-events: auto）。插槽为空时它就是个纯装饰层。
  */
  pointer-events: none;
}
</style>
