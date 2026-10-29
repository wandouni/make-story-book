<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import type { CheckboxValueType } from 'element-plus'
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import { useBookStore } from '@/stores/book'
import { useMetaStore } from '@/stores/meta'
import type { Page } from '@/types/book'

const bookStore = useBookStore()
const meta = useMetaStore()

const storyText = ref('')
const pageCount = ref(8)
/**
 * 分页用的版式。初始值来自后端（`meta.defaultPreset`），**不写死在这里** ——
 * 用户点「分页」时发出去的是这个值，写死的话改 `config.DEFAULT_PRESET` 界面上
 * 不会有任何变化。
 *
 * 空串只出现在"meta 还没拉到"的时候，那时这个下拉是空的、`runPaginate` 也
 * 不会被点到（见下面的 watch 和 paginate 里的判断）。
 */
const preset = ref('')
const paginating = ref(false)
const savingStory = ref(false)

// meta 是 BookWorkspace 在 onMounted 里 await 过的，走到这一步通常已经有值；
// 这个 watch 兜住时序意外，同时保证"用户还没动过下拉"时它才回填 ——
// 用户自己选过就别去覆盖他。
watch(
  () => meta.defaultPreset,
  (value) => {
    if (value && !preset.value) preset.value = value
  },
  { immediate: true },
)

const book = computed(() => bookStore.book)
const pages = computed(() => book.value?.pages ?? [])

// ---------------------------------------------------------------- 选择集
//
// 用**页 id**而不是页码：删除、拆分、合并、重新分页都会让页码整体漂移，
// 而 id 是稳定的。下面 persistPage 反查页码用的也是同一套办法。

/** 批量删除的勾选集。 */
const selectedIds = ref<Set<string>>(new Set())

const allSelected = computed(
  () => pages.value.length > 0 && selectedIds.value.size === pages.value.length,
)
/** 只勾了一部分 —— el-checkbox 的第三种状态（那一道横杠）。 */
const someSelected = computed(() => selectedIds.value.size > 0 && !allSelected.value)

function toggleAll(checked: boolean) {
  selectedIds.value = checked ? new Set(pages.value.map((p) => p.id)) : new Set()
}

function toggleOne(id: string, checked: boolean) {
  const next = new Set(selectedIds.value)
  if (checked) next.add(id)
  else next.delete(id)
  selectedIds.value = next
}

// 页面集合一变就把已经不存在的页从选择集里摘掉。不摘的话「全选」永远为假，
// 而「删除选中 (N)」里还挂着一个幽灵页码 —— 点下去会 404。
watch(
  () => pages.value.map((p) => p.id).join(','),
  () => {
    const alive = new Set(pages.value.map((p) => p.id))
    const next = new Set([...selectedIds.value].filter((id) => alive.has(id)))
    if (next.size !== selectedIds.value.size) selectedIds.value = next
  },
)

// ---------------------------------------------------------------- 同步

watch(
  () => book.value?.id,
  () => {
    const current = book.value
    if (!current) return
    storyText.value = current.storyText
    pageCount.value = current.pageCount
    preset.value =
      current.pages[0]?.mode === 'preset' ? current.pages[0].preset : meta.defaultPreset
    // 换了一本书，上一本的选择状态不能留着
    selectedIds.value = new Set()
  },
  { immediate: true },
)

const charCount = computed(() => storyText.value.replace(/\s+/g, '').length)

/** 页面文字被逐页改过之后，与原文就对不上了。这不是错误，只是提醒。 */
const outOfSync = computed(() => {
  const current = book.value
  if (!current || current.pages.length === 0) return false
  const normalize = (s: string) => s.replace(/\s+/g, '')
  return normalize(current.pages.map((p) => p.text).join('')) !== normalize(current.storyText)
})

// ---------------------------------------------------------------- 故事原文

async function saveStory() {
  if (!book.value || storyText.value === book.value.storyText) return
  savingStory.value = true
  try {
    const saved = await bookStore.saveStory(storyText.value)
    // 后端入库时会剥掉 Markdown 标记（`## 标题` → `标题`）。不把清理后的文本
    // 写回输入框，这里显示的就和后面分页出来的页面不是同一份东西了。
    if (saved !== null && saved !== storyText.value) {
      storyText.value = saved
      ElMessage.info('已去掉 Markdown 标记，只保留文字本身')
    }
  } finally {
    savingStory.value = false
  }
}

// ---------------------------------------------------------------- 分页

async function runPaginate() {
  if (!storyText.value.trim()) {
    ElMessage.warning('请先粘贴故事内容')
    return
  }
  // meta 没拉到时分页默认值还是空的，发过去会是个空 preset。宁可挡住也不要
  // 让后端去猜（它会当成"未知版式"渲染出空页）。
  if (!preset.value) {
    ElMessage.warning('版式清单还没加载好，请稍后再试')
    return
  }

  if (pages.value.length > 0) {
    const illustrated = pages.value.filter((p) => p.imageId).length
    const extra = illustrated
      ? `已生成的 ${illustrated} 张插图会被丢弃，需要重新配图。`
      : '当前所有页面的调整都会被覆盖。'
    try {
      await ElMessageBox.confirm(`重新分页会按原文重新切分所有页面。${extra}`, '确认重新分页', {
        type: 'warning',
        confirmButtonText: '重新分页',
        cancelButtonText: '取消',
      })
    } catch {
      return
    }
  }

  paginating.value = true
  try {
    await flushAll()
    await saveStory()
    const warnings = await bookStore.runPaginate(pageCount.value, preset.value)
    if (warnings.length === 0) ElMessage.success(`已分成 ${bookStore.book?.pages.length ?? 0} 页`)
  } finally {
    paginating.value = false
  }
}

// ---------------------------------------------------------------- 逐页文字

const saveTimers = new Map<string, ReturnType<typeof setTimeout>>()

function scheduleSave(page: Page) {
  const pending = saveTimers.get(page.id)
  if (pending) clearTimeout(pending)
  saveTimers.set(
    page.id,
    setTimeout(() => {
      saveTimers.delete(page.id)
      void persistPage(page)
    }, 600),
  )
}

async function persistPage(page: Page) {
  const current = book.value
  if (!current) return
  const number = current.pages.findIndex((p) => p.id === page.id) + 1
  if (number < 1) return // 这一页已经被删掉了
  await bookStore.savePageText(number, page.text)
}

/** 切分/合并/删除会替换整个 pages 数组，先把挂起的编辑落盘，避免丢字。 */
async function flushAll() {
  const pending = [...saveTimers.keys()]
  saveTimers.forEach((timer) => clearTimeout(timer))
  saveTimers.clear()
  for (const id of pending) {
    const page = book.value?.pages.find((p) => p.id === id)
    if (page) await persistPage(page)
  }
}

// ---------------------------------------------------------------- 页面操作

async function removePage(number: number) {
  const page = pages.value[number - 1]
  if (!page) return
  try {
    await ElMessageBox.confirm(
      `删除第 ${number} 页？该页文字会一并消失，不会回到原文里。`,
      '确认删除',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  await flushAll()
  await bookStore.removePage(number)
}

async function removeSelected() {
  const targets = pages.value
    .map((page, index) => ({ id: page.id, number: index + 1 }))
    .filter((t) => selectedIds.value.has(t.id))
  if (!targets.length) return

  const numbers = targets.map((t) => t.number)
  const list = numbers.slice(0, 8).join('、') + (numbers.length > 8 ? ' 等' : '')
  try {
    await ElMessageBox.confirm(
      `删除第 ${list} 页（共 ${numbers.length} 页）？这些页的文字会一并消失，不会回到原文里。`,
      '确认批量删除',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }

  await flushAll()
  const count = numbers.length
  await bookStore.removePages(numbers)
  selectedIds.value = new Set()
  ElMessage.success(`已删除 ${count} 页`)
}

async function mergeNext(number: number) {
  await flushAll()
  await bookStore.mergeNext(number)
}

// ---------------------------------------------------------------- 拆分

const splitVisible = ref(false)
const splitNumber = ref(1)
const splitIndex = ref(1)

const splitSource = computed(() => pages.value[splitNumber.value - 1]?.text ?? '')

/** 默认切在离中点最近的句末标点后 —— 中文故事几乎总是想在这里断开。 */
function nearestSentenceBreak(text: string): number {
  const middle = Math.floor(text.length / 2)
  const marks = /[。！？!?…]/g
  let best = middle
  let bestDistance = Infinity
  let match: RegExpExecArray | null
  while ((match = marks.exec(text)) !== null) {
    const position = match.index + 1
    if (position >= text.length) continue
    const distance = Math.abs(position - middle)
    if (distance < bestDistance) {
      best = position
      bestDistance = distance
    }
  }
  return Math.min(Math.max(best, 1), text.length - 1)
}

function openSplit(number: number) {
  const text = pages.value[number - 1]?.text ?? ''
  if (text.length < 2) {
    ElMessage.warning('这一页太短，无法再拆分')
    return
  }
  splitNumber.value = number
  splitIndex.value = nearestSentenceBreak(text)
  splitVisible.value = true
}

async function confirmSplit() {
  await flushAll()
  await bookStore.splitPage(splitNumber.value, splitIndex.value)
  splitVisible.value = false
  ElMessage.success('已拆分')
}

// flushAll 自己会清掉定时器（它先 clearTimeout 再逐页落盘），所以这里只需要它
// 一个 —— 原来是两个 onBeforeUnmount，第二个只清定时器，是白做的。
onBeforeUnmount(() => {
  void flushAll()
})
</script>

<template>
  <div class="story">
    <!-- 左：故事原文。窄栏 —— 一行 1000px 的中文没法读，所以这一栏是限宽的，
         多出来的横向空间全给右栏。 -->
    <div class="text-col">
      <el-card shadow="never" class="block">
        <template #header>
          <div class="head">
            <span>故事原文</span>
            <span class="counter">{{ charCount }} 字</span>
          </div>
        </template>

        <!-- autosize 不设 maxRows：随内容长高，滚动交给 .text-col。
             原来用 :rows + resize="vertical" 会在框里再套一个滚动条，
             而且 autosize 和手动 resize 混用会互相打架。 -->
        <el-input
          v-model="storyText"
          type="textarea"
          :autosize="{ minRows: 12 }"
          resize="none"
          placeholder="把写好的故事粘贴到这里。分页只会按标点切分，不会改写任何一个字。"
          @blur="saveStory"
        />

        <div class="actions">
          <span class="tip">离开输入框会自动保存。分页结果去空白后与原文逐字一致。</span>
          <el-button size="small" :loading="savingStory" @click="saveStory">保存原文</el-button>
        </div>
      </el-card>
    </div>

    <!-- 右：分页配置 + 页面列表。吃掉剩下的全部宽度，只有列表内部滚。 -->
    <div class="work-col">
      <el-card shadow="never" class="config-card">
        <template #header>分页</template>

        <!-- class="controls" 被 e2e 依赖（e2e_editor.py 的 check_default_preset
             靠 `.controls .el-select` 找版式下拉），改名要一起改 -->
        <div class="controls">
          <span class="label">页数</span>
          <el-input-number v-model="pageCount" :min="1" :max="30" size="default" />

          <span class="label">版式</span>
          <el-select v-model="preset" style="width: 220px">
            <el-option v-for="p in meta.presets" :key="p.id" :label="p.label" :value="p.id" />
          </el-select>

          <el-button type="primary" :loading="paginating" @click="runPaginate">
            {{ pages.length ? '重新分页' : '开始分页' }}
          </el-button>
        </div>

        <el-alert
          v-for="warning in bookStore.warnings"
          :key="warning"
          :title="warning"
          type="warning"
          :closable="false"
          show-icon
          class="warning"
        />

        <el-alert
          v-if="outOfSync"
          type="info"
          :closable="false"
          show-icon
          class="warning"
          title="页面文字与故事原文不一致"
          description="你逐页改过文字，或者原文改完还没重新分页。导出以页面文字为准。"
        />
      </el-card>

      <el-card v-if="pages.length" shadow="never" class="list-card">
        <template #header>
          <div class="head">
            <div class="head-left">
              <el-checkbox
                :model-value="allSelected"
                :indeterminate="someSelected"
                @change="(v: CheckboxValueType) => toggleAll(Boolean(v))"
              >
                全选
              </el-checkbox>
              <span class="counter">{{ pages.length }} 页</span>
            </div>
            <el-button
              size="small"
              type="danger"
              plain
              :disabled="selectedIds.size === 0"
              @click="removeSelected"
            >
              删除选中 ({{ selectedIds.size }})
            </el-button>
          </div>
        </template>

        <div class="pages">
          <div v-for="(page, index) in pages" :key="page.id" class="page-row">
            <div class="page-head">
              <el-checkbox
                :model-value="selectedIds.has(page.id)"
                @change="(v: CheckboxValueType) => toggleOne(page.id, Boolean(v))"
              />
              <span class="page-no">第 {{ index + 1 }} 页</span>
              <span class="page-len">{{ page.text.replace(/\s+/g, '').length }} 字</span>
              <span v-if="page.imageId" class="badge">已有插图</span>
              <div class="spacer" />
              <div class="page-actions">
                <el-button size="small" text @click="openSplit(index + 1)">拆分</el-button>
                <el-button
                  size="small"
                  text
                  :disabled="index === pages.length - 1"
                  @click="mergeNext(index + 1)"
                >
                  并入下页
                </el-button>
                <el-button size="small" text type="danger" @click="removePage(index + 1)">
                  删除
                </el-button>
              </div>
            </div>

            <el-input
              v-model="page.text"
              type="textarea"
              :autosize="{ minRows: 2, maxRows: 8 }"
              resize="none"
              @input="scheduleSave(page)"
            />
          </div>
        </div>
      </el-card>
    </div>

    <el-dialog v-model="splitVisible" title="拆分页面" width="560px">
      <p class="split-label">第 {{ splitNumber }} 页，共 {{ splitSource.length }} 字</p>
      <el-slider v-model="splitIndex" :min="1" :max="Math.max(1, splitSource.length - 1)" />

      <div class="split-preview">
        <div class="half">
          <div class="half-head">前半 · {{ splitIndex }} 字</div>
          <div class="half-body">{{ splitSource.slice(0, splitIndex) }}</div>
        </div>
        <div class="half">
          <div class="half-head">后半 · {{ splitSource.length - splitIndex }} 字</div>
          <div class="half-body">{{ splitSource.slice(splitIndex) }}</div>
        </div>
      </div>

      <template #footer>
        <el-button @click="splitVisible = false">取消</el-button>
        <el-button type="primary" @click="confirmSplit">拆分</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
/*
  左窄右宽。负 margin 抵消 BookWorkspace 的 .body 内边距（20px/24px），
  让两栏撑满整块区域。

  **用 flex: 1 而不是 height: 100%**：百分比解析的是父级的*内容盒*（比 padding
  box 少 40px），而负 margin 只平移不拉高，底部会露出一条 40px 的灰边。
  列向 flex 里 flex-grow 会把负 margin 精确补偿掉，详见 BookWorkspace 的注释。
*/
.story {
  display: flex;
  flex: 1;
  min-height: 0;
  margin: -20px -24px;
}

/* 左：故事原文。clamp 保证宽屏下也不会跟着变宽（34% 在约 1824px 处触顶 620）
   —— 一行一千多像素的中文没法读，多出来的宽度留给右栏。 */
.text-col {
  width: clamp(380px, 34%, 620px);
  flex-shrink: 0; /* 不被右栏网格的 min-content 挤扁 */
  min-width: 0;
  overflow-y: auto;
  padding: 20px 24px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* 短故事时卡片也铺满整栏，不然下面留一大片灰。
   用 flex-grow 而不是 min-height: 100% —— 后者是"至少这么高"，加上 .text-col
   自己的 40px 上下内边距就必然超出内容盒，于是左栏永远挂着一条多余的滚动条。
   flex: 1 0 auto 在内容比容器矮时正好铺满、比容器高时才溢出，两种情况都对。 */
.text-col > .block {
  flex: 1 0 auto;
}

/* 右：分页配置 + 页面列表 */
.work-col {
  flex: 1;
  min-width: 0; /* 关键：否则 .pages 的 min-content 会把整行撑破 */
  display: flex;
  flex-direction: column;
  min-height: 0;
  gap: 16px;
  padding: 20px 24px;
  border-left: 1px solid #e6e8eb;
  background: #fff;
}

/* 配置卡按内容高度，永不自己滚。视口太矮时整列溢出，由 .body 的兜底滚动接住，
   不会出现嵌套的滚动条。 */
.config-card {
  flex: 0 0 auto;
}

/* 页面列表吃掉剩余高度。只有卡片体滚，卡片头的「全选 / 删除选中」常驻。 */
.list-card {
  flex: 1 1 auto;
  min-height: 220px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.list-card :deep(.el-card__header) {
  flex-shrink: 0;
}

.list-card :deep(.el-card__body) {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 12px 14px;
}

.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.head-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.counter {
  font-size: 12px;
  color: #909399;
}

.actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 10px;
}

.tip {
  font-size: 12px;
  color: #909399;
}

.controls {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.label {
  font-size: 13px;
  color: #606266;
}

.warning {
  margin-top: 12px;
}

/*
  两列。窄屏会自然退成单列，这是对的：2 列的临界视口宽约 1182px，1024px 上
  硬塞两列会让每行只剩 ~292px，而 .page-head 的固定开销（勾选框 + 页码 + 字数 +
  徽章 + 三个按钮 + padding）约 338px，必然溢出。
*/
.pages {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(360px, 100%), 1fr));
  gap: 12px;
}

/* 不再是可点的东西（原来点一下会切右栏预览）——所以没有 cursor: pointer，
   也没有 hover 变色，那都是"可点"的承诺。焦点态用 :focus-within 补回来：
   Tab 进某个文本域时这一行仍然高亮，键盘用户受益，且不需要任何 JS 状态。 */
.page-row {
  border: 1px solid #e6e8eb;
  border-radius: 6px;
  padding: 10px 12px;
  transition: border-color 0.2s;
}

.page-row:focus-within {
  border-color: #409eff;
}

.page-actions {
  display: flex;
}

/* flex-wrap：行宽被挤到 360 出头时让徽章那一段换行，而不是溢出 */
.page-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 8px;
  min-width: 0;
}

.page-no {
  font-size: 13px;
  font-weight: 600;
  color: #2c3e50;
}

.page-len {
  font-size: 12px;
  color: #909399;
}

.badge {
  font-size: 11px;
  color: #67c23a;
  background: #f0f9eb;
  border-radius: 3px;
  padding: 1px 6px;
}

.spacer {
  flex: 1;
}

.split-label {
  font-size: 13px;
  color: #606266;
  margin: 0 0 4px;
}

.split-preview {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-top: 8px;
}

.half {
  border: 1px solid #e6e8eb;
  border-radius: 6px;
  overflow: hidden;
}

.half-head {
  font-size: 12px;
  color: #909399;
  background: #fafafa;
  padding: 6px 10px;
  border-bottom: 1px solid #e6e8eb;
}

.half-body {
  font-size: 13px;
  line-height: 1.6;
  color: #2c3e50;
  padding: 10px;
  max-height: 220px;
  overflow-y: auto;
  white-space: pre-wrap;
}
</style>
