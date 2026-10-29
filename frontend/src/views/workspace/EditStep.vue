<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, nextTick, onMounted, onBeforeUnmount, ref, watch } from 'vue'

import { cancelJob } from '@/api/jobs'
import { autoLayout, getLayoutUndo, undoLayout } from '@/api/layout'
import type { UpdateBlockRequest } from '@/api/pages'
import JobProgressBar from '@/components/common/JobProgressBar.vue'
import BlockInspector from '@/components/editor/BlockInspector.vue'
import BlockOverlay from '@/components/editor/BlockOverlay.vue'
import PagePreview from '@/components/editor/PagePreview.vue'
import PresetPanel from '@/components/editor/PresetPanel.vue'
import TextStylePanel from '@/components/editor/TextStylePanel.vue'
import { previewOpen } from '@/composables/previewOpen'
import { useJobStream } from '@/composables/useJobStream'
import { useUndoRedo } from '@/composables/useUndoRedo'
import { useBookStore } from '@/stores/book'
import { useMetaStore } from '@/stores/meta'
import { isFreePage, isPresetPage, type Block, type PresetPageOptions } from '@/types/book'
import { isTerminal } from '@/types/job'

const bookStore = useBookStore()
const meta = useMetaStore()

const book = computed(() => bookStore.book)
const pages = computed(() => book.value?.pages ?? [])

const current = ref(1)
const saving = ref(false)
/** 每次保存成功 +1，让预览 iframe 重新加载。 */
const reloadKey = ref(0)
const selectedBlockId = ref<string | null>(null)

/**
 * 智能排版任务跑着的时候，**整个面板都要禁用**。
 *
 * 它是一次性整本落盘（见 `auto_layout.run_layout_job`），跟用户的手动编辑
 * 抢同一本书。不禁用的话，用户可以在任务写完的瞬间覆盖掉它，或者反过来。
 */
const { job, watch: watchJob, close: closeStream } = useJobStream()
const jobRunning = computed(() => job.value !== null && !isTerminal(job.value))
const busy = computed(() => saving.value || jobRunning.value)

const preview = ref<InstanceType<typeof PagePreview> | null>(null)

const page = computed(() => pages.value[current.value - 1])
const presetPage = computed(() => (page.value && isPresetPage(page.value) ? page.value : null))
const freePage = computed(() => (page.value && isFreePage(page.value) ? page.value : null))
const freeBlocks = computed<Block[]>(() => freePage.value?.blocks ?? [])
const selectedBlock = computed(
  () => freeBlocks.value.find((b) => b.id === selectedBlockId.value) ?? null,
)

// ---------------------------------------------------------------- 文字溢出

/**
 * fit-text.js 在 iframe 里跑完自动缩字后，把仍然放不下的块 id postMessage
 * 回来。这是三重防线的最后一道：CSS `overflow: hidden` 兜底不跑版、自动缩字
 * 尽量塞下，实在塞不下就得让用户知道 —— 否则文字会被**静默裁掉**，
 * 而人要等到打印出来才发现。
 *
 * iframe 一次只渲染一页，所以只要有 id 回来就一定是当前这一页，不用拿 id
 * 去比对：预设页回传的是 page.id、自由页回传的是 block.id，两者语义不同，
 * 硬比对反而容易出错。
 */
const overflowIds = ref<string[]>([])
const hasOverflow = computed(() => overflowIds.value.length > 0)

function onMessage(event: MessageEvent) {
  const data = event.data as { type?: string; ids?: unknown } | null
  if (!data || data.type !== 'msb:overflow') return
  overflowIds.value = Array.isArray(data.ids) ? (data.ids as string[]) : []
}

onMounted(() => {
  window.addEventListener('message', onMessage)
  const trim = book.value?.trimSize
  if (trim) preview.value?.setPageSize(trim.widthMm, trim.heightMm)
})

onBeforeUnmount(() => window.removeEventListener('message', onMessage))

// 换页或重新加载时先清空：旧的溢出状态属于上一页，留着会误报
watch([current, reloadKey], () => {
  overflowIds.value = []
})

// 书是异步加载的，挂载时可能还没到；到了再设一次尺寸
watch(
  () => book.value?.trimSize.id,
  () => {
    const trim = book.value?.trimSize
    if (trim) nextTick(() => preview.value?.setPageSize(trim.widthMm, trim.heightMm))
  },
)

watch(current, () => {
  // 换页时若还有未落盘的改动，先等它写完再切，避免视觉上"改完又弹回去"
  void flush()
  // 选中状态不跨页：第 3 页选的块和第 5 页的块 id 不同，留着只会指向空气
  selectedBlockId.value = null
})

/** 保证 current 不越界（删页、重新分页之后可能超出）。 */
watch(
  () => pages.value.length,
  (len) => {
    if (len && current.value > len) current.value = len
  },
)

// ---------------------------------------------------------------- 保存

let pending: Promise<unknown> | null = null

async function flush() {
  if (!pending) return
  try {
    await pending
  } catch {
    // 错误提示由 axios 拦截器统一弹出
  }
}

async function apply(payload: { preset?: string; options?: PresetPageOptions }) {
  if (!page.value) return
  const number = current.value
  saving.value = true
  pending = bookStore.updatePageStyle(number, payload)
  try {
    await pending
    reloadKey.value += 1
  } catch {
    // 失败时把服务端状态拉回来，避免界面停在一个并未保存的版式上
    await bookStore.reload()
  } finally {
    pending = null
    saving.value = false
  }
}

function changePreset(preset: string) {
  if (!page.value || presetPage.value?.preset === preset) return
  void apply({ preset })
}

function changeOptions(patch: Partial<PresetPageOptions>) {
  const options = presetPage.value?.options
  if (!options) return
  // 后端要的是完整 options 对象，这里合并后整体提交
  void apply({ options: { ...options, ...patch } })
}

// ---------------------------------------------------------------- 自由页

// 撤销栈存的是**块**而不是整本书：整书快照会在别的步骤（生图、重新分页）
// 刷新 store 时变得陈旧，撤销一下就把过期的页面结构贴回来了。
const {
  canUndo,
  canRedo,
  record,
  undo: undoSnapshot,
  redo: redoSnapshot,
  clear: clearHistory,
} = useUndoRedo<Block[]>(() => page.value?.id ?? null)

async function convertToFree() {
  if (!page.value || saving.value) return
  saving.value = true
  try {
    await bookStore.toFreePage(current.value)
    clearHistory()
    selectedBlockId.value = null
    reloadKey.value += 1
    ElMessage.success('已转为自由拖拽。版面看起来没变，现在可以直接拖动了。')
  } finally {
    saving.value = false
  }
}

/**
 * 在预设页上**直接拖动**时走的转换，返回展开后的块。
 *
 * 和上面的 convertToFree 有两处刻意的不同，都是为了不打断正在进行的手势：
 * 不弹消息（一次拖动弹一条太吵），不清选中（块马上要被选中并亮出缩放手柄）。
 */
async function convertByDrag(): Promise<Block[] | null> {
  if (!presetPage.value || saving.value) return null
  saving.value = true
  try {
    const converted = await bookStore.toFreePage(current.value)
    clearHistory()
    reloadKey.value += 1
    return converted?.mode === 'free' ? converted.blocks : null
  } catch {
    await bookStore.reload()
    return null
  } finally {
    saving.value = false
  }
}

async function convertToPreset() {
  const blocks = freeBlocks.value
  try {
    await ElMessageBox.confirm(
      `这一页有 ${blocks.length} 个块，转回预设版式后它们的位置、旋转和逐块样式` +
        '都会被丢弃，只保留正文和图片。此操作**不能**撤销。',
      '转回预设版式',
      { type: 'warning', confirmButtonText: '丢弃并转回', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户取消
  }

  // 自由页没有 preset 字段，转回哪个版式只能取默认值。**别写死** ——
  // 这里和「分页」用的是同一个默认（见 stores/meta.ts 的 defaultPreset）
  const target = meta.defaultPreset
  if (!target) {
    ElMessage.warning('版式清单还没加载好，请稍后再试')
    return
  }

  saving.value = true
  try {
    await bookStore.toPresetPage(current.value, target)
    clearHistory()
    selectedBlockId.value = null
    reloadKey.value += 1
    ElMessage.success('已转回预设版式，可在上方重新选择版式')
  } finally {
    saving.value = false
  }
}

async function changeBlock(id: string, patch: UpdateBlockRequest) {
  if (!freePage.value) return
  // 先记快照再改，撤销才能回到"改之前"
  record(freeBlocks.value)
  try {
    await bookStore.patchBlock(current.value, id, patch)
    reloadKey.value += 1
  } catch {
    await bookStore.reload()
  }
}

/** 层级不在这里算：置顶要取所有块的最大 zIndex，那是父组件才知道的信息。 */
function changeLayer(id: string, dir: 'front' | 'back') {
  const zs = freeBlocks.value.map((b) => b.zIndex)
  const zIndex = dir === 'front' ? Math.max(0, ...zs) + 1 : Math.min(0, ...zs) - 1
  void changeBlock(id, { zIndex })
}

async function removeBlock(id: string) {
  if (!freePage.value) return
  record(freeBlocks.value)
  try {
    await bookStore.removeBlock(current.value, id)
    if (selectedBlockId.value === id) selectedBlockId.value = null
    reloadKey.value += 1
  } catch {
    await bookStore.reload()
  }
}

async function addBlock(type: 'text' | 'image') {
  if (!freePage.value || saving.value) return
  record(freeBlocks.value)
  try {
    const updated = await bookStore.addBlock(current.value, {
      type,
      // 落在页面上偏左上一点的位置，而不是贴边 —— 贴边容易被误认为裁掉了
      xMm: 15,
      yMm: 15,
      widthMm: type === 'text' ? 70 : 60,
      heightMm: type === 'text' ? 40 : 60,
      // 空文字块是有意的：默认塞进整页正文的话，加第二个块时会莫名其妙
      // 多出一份正文。加完自动选中，右侧就是内容输入框。
      content: type === 'text' ? '' : undefined,
    })
    const added = updated?.mode === 'free' ? updated.blocks.at(-1) : null
    if (added) selectedBlockId.value = added.id
    reloadKey.value += 1
  } catch {
    await bookStore.reload()
  }
}

// ---------------------------------------------------------------- 撤销重做

async function restore(target: Block[] | null) {
  if (!target) return
  await bookStore.restorePageBlocks(current.value, target)
  selectedBlockId.value = null
  reloadKey.value += 1
}

async function undo() {
  if (!freePage.value) return
  await restore(undoSnapshot(freeBlocks.value))
}

async function redo() {
  if (!freePage.value) return
  await restore(redoSnapshot(freeBlocks.value))
}

// ---------------------------------------------------------------- 快捷键

const NUDGE: Record<string, [number, number]> = {
  ArrowLeft: [-1, 0],
  ArrowRight: [1, 0],
  ArrowUp: [0, -1],
  ArrowDown: [0, 1],
}

function onKeydown(e: KeyboardEvent) {
  // 翻页预览是全屏弹窗，但它和这里一样挂在 window 上，两边都会吃到方向键。
  // 不挡的话一次 ArrowRight 既翻屏、又把选中的块挪 1mm —— 静默改数据，还进撤销栈。
  if (previewOpen.value) return
  if (!freePage.value) return

  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'z') {
    e.preventDefault()
    void (e.shiftKey ? redo() : undo())
    return
  }

  const block = selectedBlock.value
  if (!block) return

  // 正在输入框里打字时别抢键 —— 否则在正文里按 Delete 会删掉整个块
  const el = e.target as HTMLElement | null
  if (el?.closest('input, textarea, [contenteditable="true"]')) return

  if (e.key === 'Delete' || e.key === 'Backspace') {
    e.preventDefault()
    void removeBlock(block.id)
    return
  }

  const dir = NUDGE[e.key]
  if (!dir) return
  e.preventDefault()
  // 按住 Shift 走大步，便于精确对齐到某个位置
  const step = e.shiftKey ? 10 : 1
  const round1 = (v: number) => Math.round(v * 10) / 10
  void changeBlock(block.id, {
    xMm: round1(block.xMm + dir[0] * step),
    yMm: round1(block.yMm + dir[1] * step),
  })
}

onMounted(() => window.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))

// ---------------------------------------------------------------- 批量套用

async function applyToAll() {
  const options = presetPage.value?.options
  const preset = presetPage.value?.preset
  if (!options || !preset) return

  const targets = pages.value
    .map((p, i) => ({ p, n: i + 1 }))
    .filter(({ p }) => isPresetPage(p))
  if (targets.length <= 1) {
    ElMessage.info('没有其它预设页可以套用')
    return
  }

  // `overlayRect` **不跟着套用**：它是"这一页的插图哪儿空"的答案，不是样式。
  //
  // `overlayPosition` 是个规则（auto/top/bottom），换一页照样成立，所以照抄；
  // 矩形是个值，它只对当前这张插图成立。抄过去的结果是：后面每一页都按第一页
  // 的空白位置放字 —— 而那张图的空白在别处，字就压到主体上了，恰恰是这个功能
  // 要避免的事。套用过去等于把「智能配置」一键降级成「智能配置错了」。
  const style = { ...options, overlayRect: null }

  saving.value = true
  try {
    // 逐页串行：并发写同一本书会互相覆盖（后端按整本落盘）
    for (const { n } of targets) {
      await bookStore.updatePageStyle(n, { preset, options: style })
    }
    reloadKey.value += 1
    ElMessage.success(`已套用到 ${targets.length} 个预设页`)
  } catch {
    await bookStore.reload()
  } finally {
    saving.value = false
  }
}

// ---------------------------------------------------------------- 智能排版

/** 有没有可撤销的智能配置。按钮靠它决定显示与否 —— 刷新页面后必须还准。 */
const undoAvailable = ref(false)

async function refreshUndoAvailable() {
  const id = book.value?.id
  if (!id) return
  try {
    undoAvailable.value = (await getLayoutUndo(id)).available
  } catch {
    // 拉不到就当没有：一个点了必然 404 的按钮比没有按钮更糟
    undoAvailable.value = false
  }
}

// 书是异步加载的，所以 watch 而不是 onMounted 时读一次
watch(() => book.value?.id, () => void refreshUndoAvailable(), { immediate: true })

async function runAutoLayout() {
  if (!book.value || busy.value) return
  try {
    const { jobId } = await autoLayout(book.value.id)
    // 任务跑着的时候撤销按钮不该出现：后端会 409 挡住，界面上也不该给这个选项
    undoAvailable.value = false

    watchJob(jobId, async (finished) => {
      // 服务端是唯一真源：写入是任务最后一次性发生的，中途没有可预览的东西
      await bookStore.reload()
      reloadKey.value += 1
      // ★ 自由页的撤销栈（useUndoRedo<Block[]>）里存的是**任务之前**的块快照，
      // 任务改过块之后它们已经过期。不清的话按 Ctrl+Z 会把排版前的几何贴回来，
      // 看起来像"撤销了智能排版但只撤销了半页"。convertToFree / convertByDrag
      // 踩过同一个坑，照做。
      clearHistory()
      selectedBlockId.value = null
      await refreshUndoAvailable()

      if (finished.status === 'failed') {
        ElMessage.error(finished.message || '智能排版失败')
      } else if (finished.status === 'cancelled') {
        ElMessage.info('已取消，书籍没有改动')
      } else if (finished.completed === 0) {
        // 一页都没动。用 warning 而不是 success —— 说"成功"是骗人的
        ElMessage.warning(finished.message || '没有可以智能排版的页面')
      } else {
        ElMessage.success(finished.message)
      }
    })
  } catch {
    // 400（没有可配置的页）的 detail 里带跳过原因汇总，拦截器已经弹过了
    await refreshUndoAvailable()
  }
}

async function onCancelLayout() {
  if (!job.value) return
  await cancelJob(job.value.id)
  ElMessage.info('已请求取消，本次不会写入任何改动')
}

async function undoAutoLayout() {
  if (!book.value || busy.value) return
  saving.value = true
  try {
    const result = await undoLayout(book.value.id)
    await bookStore.reload()
    // 撤销把块几何贴回去了，撤销栈里那些快照的语义也跟着变了，清掉最省心
    clearHistory()
    selectedBlockId.value = null
    reloadKey.value += 1
    ElMessage.success(
      result.skipped
        ? `已撤销，恢复 ${result.restored} 页（${result.skipped} 页已不在书里）`
        : `已撤销，恢复 ${result.restored} 页`,
    )
  } catch {
    // 404（快照没了）/ 409（任务还在跑）：拦截器弹了原因，这里只要把按钮
    // 的可见性同步回真实状态
  } finally {
    saving.value = false
    await refreshUndoAvailable()
  }
}
</script>

<template>
  <div class="edit">
    <el-empty v-if="pages.length === 0" description="还没有页面，请先到「故事」步骤分页" />

    <!--
      进度条挂在编辑器**上方**而不是替换它：任务跑的时候用户仍然要能翻页看
      每一页被改成了什么样。用 <template> 包一层是为了不让 JobProgressBar 的
      v-if 和下面 div 的 v-else 接成链（那样有任务时整个编辑器就没了）——
      template 不产生 DOM 节点，.edit 的 flex 布局不受影响。
    -->
    <template v-else>
      <JobProgressBar
        v-if="job"
        :job="job"
        title="智能排版进度"
        @cancel="onCancelLayout"
        @close="closeStream"
      />

      <div class="layout">
        <!-- 左：页码 -->
        <aside class="rail">
          <div
            v-for="(p, i) in pages"
            :key="p.id"
            class="rail-item"
            :class="{ active: current === i + 1 }"
            @click="current = i + 1"
          >
            <span class="rail-no">{{ i + 1 }}</span>
            <span class="rail-text">{{ p.text || '（无文字）' }}</span>
            <span v-if="!isPresetPage(p)" class="rail-tag">自由</span>
          </div>
        </aside>

      <!--
        中：预览（上面叠一层拖拽层，两者共用同一个缩放变换）。
        拖拽层在预设页上也要挂着 —— 那时它没有块，退化成一块整页可抓的垫子，
        抓住一拖就把这一页展开成自由拖拽（见 BlockOverlay 的文件头）。
      -->
      <PagePreview
        ref="preview"
        class="stage"
        :book-id="book!.id"
        :page-number="current"
        :reload-key="reloadKey"
      >
        <template #default="{ scale }">
          <BlockOverlay
            :blocks="freeBlocks"
            :selected-id="selectedBlockId"
            :scale="scale"
            :page-width-mm="book!.trimSize.widthMm"
            :page-height-mm="book!.trimSize.heightMm"
            :disabled="busy"
            :convert-page="presetPage ? convertByDrag : undefined"
            @update:selected-id="selectedBlockId = $event"
            @change="changeBlock"
          />
        </template>
      </PagePreview>

      <!-- 右：属性 -->
      <aside class="side">
        <div class="side-head">
          <span>第 {{ current }} / {{ pages.length }} 页</span>
          <el-tag v-if="saving" size="small" type="info">保存中</el-tag>
        </div>

        <el-alert
          v-if="hasOverflow"
          type="warning"
          :closable="false"
          show-icon
          class="overflow-notice"
          title="这一页的文字放不下"
          description="已经自动缩小过字号，仍然超出文本框，超出的部分会被裁掉。可以减少文字、手动调小字号，或转成自由拖拽把文字框拉大。"
        />

        <!--
          智能排版对**整本书**生效，预设页和自由页都算，所以这一组放在两个
          v-if 分支的外面 —— 放在预设页分支里的话，用户在自由页上就找不到它了。
        -->
        <h4 class="group">智能排版</h4>
        <div class="stack">
          <el-button type="primary" size="small" :disabled="busy" @click="runAutoLayout">
            一键智能排版
          </el-button>
          <el-button v-if="undoAvailable" size="small" :disabled="busy" @click="undoAutoLayout">
            撤销本次智能配置
          </el-button>
        </div>
        <p class="hint">
          让大模型看着每页插图的空白处挑落点、配字色；字号和行距按框的大小算出来。
          整本书一次配好，只保留最近一次撤销。
        </p>

        <el-divider />

        <template v-if="presetPage">
          <h4 class="group">版式</h4>
          <PresetPanel
            :model-value="presetPage.preset"
            :disabled="busy"
            @update:model-value="changePreset"
          />

          <h4 class="group">文字样式</h4>
          <TextStylePanel
            :options="presetPage.options"
            :preset="presetPage.preset"
            :disabled="busy"
            @change="changeOptions"
          />

          <el-button class="apply-all" size="small" :disabled="busy" @click="applyToAll">
            把版式与样式套用到所有预设页
          </el-button>

          <el-divider />
          <p class="hint">
            <strong>直接拖动页面上的文字或图片</strong>，就能把它单独挪到任意位置，
            松手后还能拖手柄改大小 —— 这一页会自动变成自由拖拽。
          </p>
          <el-button class="apply-all" size="small" :disabled="busy" @click="convertToFree">
            转为自由拖拽
          </el-button>
        </template>

        <!-- 自由页 -->
        <template v-else-if="freePage">
          <div class="row">
            <el-button size="small" :disabled="busy" @click="addBlock('text')">
              加文字块
            </el-button>
            <el-button size="small" :disabled="busy" @click="addBlock('image')">
              加图片块
            </el-button>
          </div>

          <div class="row">
            <el-button size="small" :disabled="busy || !canUndo" @click="undo">撤销</el-button>
            <el-button size="small" :disabled="busy || !canRedo" @click="redo">重做</el-button>
            <el-button size="small" :disabled="busy" @click="convertToPreset">转回预设</el-button>
          </div>

          <p class="hint">
            拖动块移动，拖八个手柄改大小。方向键微调（按住 Shift 走 10mm），Delete 删除，
            Ctrl+Z 撤销。
          </p>

          <el-divider />

          <BlockInspector
            v-if="selectedBlock"
            :block="selectedBlock"
            :bleed-mm="book!.bleedMm"
            :disabled="busy"
            @change="changeBlock"
            @layer="changeLayer"
            @remove="removeBlock"
          />
          <el-empty v-else :image-size="60" description="点页面上的块来编辑" />
        </template>
      </aside>
      </div>
    </template>
  </div>
</template>

<style scoped>
/* 撑满 .body 的整块区域。用 flex: 1 而不是 height: 100% —— 负 margin 撑满的
   原理见 BookWorkspace 的 .body 注释，那是这个写法成立的前提。 */
.edit {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

/* 负 margin 留在这一层，**别挪到 .edit 上** —— .edit 里还有个"没有页面"的
   el-empty 分支，挪上去会把空状态拉到贴边。 */
.layout {
  display: flex;
  flex: 1;
  min-height: 0;
  margin: -20px -24px;
}

.rail {
  width: 150px;
  flex-shrink: 0;
  border-right: 1px solid #e6e8eb;
  background: #fff;
  overflow-y: auto;
  padding: 8px;
}

.rail-item {
  display: flex;
  align-items: baseline;
  gap: 6px;
  padding: 7px 8px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
  color: #606266;
}

.rail-item:hover {
  background: #f5f7fa;
}

.rail-item.active {
  background: #ecf5ff;
  color: #409eff;
}

.rail-no {
  font-weight: 600;
  flex-shrink: 0;
}

.rail-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #909399;
}

.rail-tag {
  font-size: 10px;
  color: #e6a23c;
  flex-shrink: 0;
}

.stage {
  min-width: 0;
}

.side {
  width: 280px;
  flex-shrink: 0;
  border-left: 1px solid #e6e8eb;
  background: #fff;
  overflow-y: auto;
  padding: 12px 14px;
}

.side-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 13px;
  font-weight: 600;
  color: #2c3e50;
  margin-bottom: 12px;
}

.overflow-notice {
  margin-bottom: 12px;
}

.overflow-notice :deep(.el-alert__description) {
  font-size: 12px;
  line-height: 1.6;
  margin: 4px 0 0;
}

.group {
  font-size: 12px;
  font-weight: 600;
  color: #909399;
  margin: 16px 0 8px;
}

.group:first-of-type {
  margin-top: 0;
}

.apply-all {
  width: 100%;
  margin-top: 16px;
}

/* 竖排的一组整宽按钮。Element Plus 给相邻按钮加了 margin-left: 12px，
   竖排时要清掉，否则两个按钮左右错开。 */
.stack {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.stack .el-button {
  width: 100%;
  margin-left: 0;
}

.row {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
}

.row .el-button {
  flex: 1;
}

.hint {
  font-size: 11px;
  color: #a8abb2;
  line-height: 1.6;
  margin: 8px 0 0;
}

.side :deep(.el-divider) {
  margin: 14px 0;
}
</style>
