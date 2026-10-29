<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import type { CheckboxValueType } from 'element-plus'
import { computed, ref, watch } from 'vue'

import {
  generatePrompts,
  getImageHistory,
  illustrateBook,
  illustratePage,
  imageUrl,
  refinePage,
  selectImage,
  type ImageHistory,
} from '@/api/illustrate'
import { cancelJob } from '@/api/jobs'
import JobProgressBar from '@/components/common/JobProgressBar.vue'
import { useJobStream } from '@/composables/useJobStream'
import { useBookStore } from '@/stores/book'
import { useMetaStore } from '@/stores/meta'
import type { Job } from '@/types/job'

const bookStore = useBookStore()
const meta = useMetaStore()

const { job, streamError, watch: watchJob, close: closeStream } = useJobStream()

const busy = ref(false)
const book = computed(() => bookStore.book)
const pages = computed(() => book.value?.pages ?? [])

const keys = computed(() => meta.health?.keysConfigured ?? { text: false, doubao: false })

/**
 * 生图服务是否真的能用。不能只看 key 填没填 —— key 可能是别家的，
 * 后端会给出具体原因（例如 API_KEY 指向火山方舟却选了通义）。
 */
const serviceStatus = computed(() => {
  const service = book.value?.imageService
  if (!service) return { available: false, reason: '' }
  return meta.health?.imageServices?.[service] ?? { available: true, reason: '' }
})
const serviceReady = computed(() => serviceStatus.value.available)

/** 有正文但还没写提示词的页 —— 只有这些页需要"生成提示词"。 */
const pagesMissingPrompt = computed(() =>
  pages.value
    .map((page, index) => ({ page, number: index + 1 }))
    .filter(({ page }) => !page.imagePrompt.trim() && page.text.trim()),
)

const pagesWithPrompt = computed(() => pages.value.filter((p) => p.imagePrompt.trim()).length)

/** 带页码的完整列表。批量操作要的是**当前**页码，所以每次都重新算。 */
const numbered = computed(() => pages.value.map((page, index) => ({ page, number: index + 1 })))

// ---------------------------------------------------------------- 勾选

/** 批量操作的勾选集，用页 id 而不是页码 —— 页码会随页面增删漂移。 */
const selectedIds = ref<Set<string>>(new Set())

const allSelected = computed(
  () => pages.value.length > 0 && selectedIds.value.size === pages.value.length,
)
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

// 页面集合一变就把已经不存在的页摘掉，否则批量按钮上会挂着一个幽灵页码
watch(
  () => pages.value.map((p) => p.id).join(','),
  () => {
    const alive = new Set(pages.value.map((p) => p.id))
    const next = new Set([...selectedIds.value].filter((id) => alive.has(id)))
    if (next.size !== selectedIds.value.size) selectedIds.value = next
  },
)

/** 选中页里还没写提示词的 —— 批量生成提示词只该管这些页。 */
const selectedMissingPrompt = computed(() => {
  const selected = new Set(selectedIds.value)
  return pagesMissingPrompt.value.filter(({ page }) => selected.has(page.id))
})

/** 选中页里有提示词的 —— 没提示词就没法生图。 */
const selectedWithPrompt = computed(() => {
  const selected = new Set(selectedIds.value)
  return numbered.value.filter(({ page }) => selected.has(page.id) && page.imagePrompt.trim())
})

/** 缩略图按开本比例留位，避免图片加载完成后整页跳动。 */
const aspectRatio = computed(() => {
  const trim = book.value?.trimSize
  return trim ? `${trim.widthMm} / ${trim.heightMm}` : '1 / 1'
})

// ---------------------------------------------------------------- 任务

watch(job, (snapshot) => {
  if (!snapshot || !book.value) return
  // 边生成边更新缩略图，不用等整个任务跑完
  for (const item of snapshot.items) {
    if (!item.imageId) continue
    const page = book.value.pages[item.pageNumber - 1]
    if (page) page.imageId = item.imageId
  }
})

async function runJob(launcher: () => Promise<{ jobId: string; total: number }>) {
  if (busy.value) return
  busy.value = true
  try {
    const { jobId } = await launcher()
    watchJob(jobId, async (finished: Job) => {
      // 任务结束后用服务端状态覆盖本地推测：本地只知道 imageId，
      // imageStatus 和可能并发的其它改动要以服务端为准。
      await bookStore.reload()
      if (finished.status === 'done' && finished.failed === 0) {
        ElMessage.success(`已完成 ${finished.completed} 页配图`)
      } else if (finished.status === 'failed') {
        ElMessage.error(finished.message || '配图失败')
      }
    })
  } catch {
    // 错误提示由 axios 拦截器统一处理
  } finally {
    busy.value = false
  }
}

function textReady(): boolean {
  if (keys.value.text) return true
  ElMessage.warning('未配置 API_KEY，无法调用文本模型生成提示词')
  return false
}

function imageReady(): boolean {
  if (serviceReady.value) return true
  ElMessage.warning(serviceStatus.value.reason || '当前生图服务不可用')
  return false
}

/**
 * 生成提示词。`pageNumbers` 省略 = 整本；`overwrite` 只在单页「重新生成」时为
 * true —— 后端默认跳过已有提示词的页，好让整本生成不覆盖用户手改过的内容。
 */
async function startPrompts(pageNumbers?: number[], overwrite = false) {
  if (!textReady()) return
  await runJob(() => generatePrompts(book.value!.id, pageNumbers, overwrite))
}

async function startAll() {
  if (!imageReady()) return
  if (pagesWithPrompt.value === 0) {
    ElMessage.warning('还没有任何提示词，请先生成提示词')
    return
  }
  await runJob(() => illustrateBook(book.value!.id))
}

/** 给单页生成提示词。已有提示词时先确认 —— 会覆盖掉手改过的内容。 */
async function generatePromptFor(number: number) {
  const page = pages.value[number - 1]
  if (!page) return
  // 正文为空时后端没有可依据的信息，会当成"没有需要生成的页"而报错，
  // 那句报错在这里看会很莫名，所以先拦住
  if (!page.text.trim()) {
    ElMessage.warning('这一页还没有正文，无法生成提示词')
    return
  }

  const hasPrompt = Boolean(page.imagePrompt.trim())
  if (hasPrompt) {
    try {
      await ElMessageBox.confirm(
        `第 ${number} 页已有提示词，重新生成会覆盖它（包括手动改过的内容）。`,
        '确认重新生成',
        { type: 'warning', confirmButtonText: '重新生成', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
  }
  await startPrompts([number], hasPrompt)
}

async function startPromptsForSelected() {
  const numbers = selectedMissingPrompt.value.map((t) => t.number)
  if (!numbers.length) return
  await startPrompts(numbers)
}

async function illustrateSelected() {
  if (!imageReady()) return
  const numbers = selectedWithPrompt.value.map((t) => t.number)
  if (!numbers.length) return
  await runJob(() => illustrateBook(book.value!.id, numbers))
}

async function generateImage(number: number) {
  if (!imageReady()) return
  await runJob(() => illustratePage(book.value!.id, number))
}

async function onCancelJob() {
  if (!job.value) return
  await cancelJob(job.value.id)
  ElMessage.info('已请求取消，正在完成手上这一页')
}

// ---------------------------------------------------------------- 提示词编辑

async function savePrompt(number: number, value: string) {
  await bookStore.savePagePrompt(number, value)
}

// ---------------------------------------------------------------- 修改要求

const refineVisible = ref(false)
const refineNumber = ref(1)
const refineText = ref('')

function openRefine(number: number) {
  refineNumber.value = number
  refineText.value = ''
  refineVisible.value = true
}

async function confirmRefine() {
  const instruction = refineText.value.trim()
  if (!instruction) {
    ElMessage.warning('请填写修改要求')
    return
  }
  refineVisible.value = false
  await runJob(() => refinePage(book.value!.id, refineNumber.value, instruction))
}

// ---------------------------------------------------------------- 历史版本

const historyVisible = ref(false)
const historyNumber = ref(1)
const history = ref<ImageHistory>({ current: null, versions: [] })
const historyLoading = ref(false)

async function openHistory(number: number) {
  historyNumber.value = number
  historyVisible.value = true
  historyLoading.value = true
  try {
    history.value = await getImageHistory(book.value!.id, number)
  } finally {
    historyLoading.value = false
  }
}

async function useVersion(imageId: string) {
  try {
    await ElMessageBox.confirm('把这一页切回这个版本？当前版本会保留在历史里，随时可以切回来。', '确认回滚', {
      confirmButtonText: '切回这一版',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  await selectImage(book.value!.id, historyNumber.value, imageId)
  await bookStore.reload()
  history.value = await getImageHistory(book.value!.id, historyNumber.value)
  ElMessage.success('已切换')
}
</script>

<template>
  <div class="illustrate">
    <el-alert
      v-if="!keys.text && !keys.doubao"
      type="warning"
      :closable="false"
      show-icon
      class="notice"
      title="尚未配置 API key，配图功能不可用"
      description="在 backend/.env 里填写 API_KEY（文本模型）和 ARK_API_KEY（豆包生图），然后重启后端。"
    />

    <el-alert
      v-else-if="!serviceReady"
      type="warning"
      :closable="false"
      show-icon
      class="notice"
      title="当前生图服务不可用"
      :description="serviceStatus.reason"
    />

    <el-alert
      v-if="streamError"
      type="error"
      :closable="false"
      show-icon
      class="notice"
      :title="streamError"
    />

    <div class="toolbar">
      <el-button
        :loading="busy"
        :disabled="pagesMissingPrompt.length === 0"
        @click="startPrompts()"
      >
        生成提示词
        <span v-if="pagesMissingPrompt.length" class="count">
          （{{ pagesMissingPrompt.length }} 页待生成）
        </span>
      </el-button>

      <el-button type="primary" :loading="busy" @click="startAll">
        全部配图
      </el-button>

      <el-divider direction="vertical" />

      <el-checkbox
        :model-value="allSelected"
        :indeterminate="someSelected"
        :disabled="pages.length === 0"
        @change="(v: CheckboxValueType) => toggleAll(Boolean(v))"
      >
        全选
      </el-checkbox>

      <el-button
        :loading="busy"
        :disabled="selectedMissingPrompt.length === 0"
        @click="startPromptsForSelected"
      >
        选中页生成提示词 ({{ selectedMissingPrompt.length }})
      </el-button>

      <el-button
        :loading="busy"
        :disabled="selectedWithPrompt.length === 0"
        @click="illustrateSelected"
      >
        选中页配图 ({{ selectedWithPrompt.length }})
      </el-button>

      <span class="hint">
        共 {{ pages.length }} 页，{{ pagesWithPrompt }} 页已有提示词
      </span>
    </div>

    <JobProgressBar
      v-if="job"
      :job="job"
      title="配图进度"
      @cancel="onCancelJob"
      @close="closeStream"
    />

    <el-empty v-if="pages.length === 0" description="还没有页面，请先到「故事」步骤分页" />

    <div v-else class="grid">
      <div
        v-for="(page, index) in pages"
        :key="page.id"
        class="cell"
        :class="{ picked: selectedIds.has(page.id) }"
      >
        <div class="thumb" :style="{ aspectRatio }">
          <img v-if="page.imageId" :src="imageUrl(book!.id, page.imageId)" alt="" />
          <div v-else class="thumb-empty">
            {{ page.imagePrompt ? '待生成' : '无提示词' }}
          </div>
          <span v-if="page.imageStatus === 'failed'" class="failed-badge">生成失败</span>
        </div>

        <div class="cell-head">
          <el-checkbox
            :model-value="selectedIds.has(page.id)"
            @change="(v: CheckboxValueType) => toggleOne(page.id, Boolean(v))"
          />
          <span class="page-no">第 {{ index + 1 }} 页</span>
          <div class="spacer" />
          <el-button
            size="small"
            text
            :disabled="!page.imagePrompt"
            @click="generateImage(index + 1)"
          >
            生成图片
          </el-button>
          <el-button
            size="small"
            text
            :disabled="!page.imagePrompt"
            @click="openRefine(index + 1)"
          >
            改要求
          </el-button>
          <el-button size="small" text :disabled="!page.imageId" @click="openHistory(index + 1)">
            历史
          </el-button>
        </div>

        <div class="prompt">
          <el-input
            v-model="page.imagePrompt"
            type="textarea"
            :autosize="{ minRows: 2, maxRows: 5 }"
            resize="none"
            placeholder="中文生图提示词。留空则「生成提示词」时会为这一页补上。"
            @change="savePrompt(index + 1, page.imagePrompt)"
          />
          <!--
            放在提示词框下面而不是顶栏：顶栏已经有三个按钮，再加一个在 260px
            的卡片里会挤成一团，而这一颗本来就只跟这个输入框有关。
          -->
          <el-button
            size="small"
            text
            type="primary"
            class="prompt-btn"
            :disabled="!page.text.trim()"
            @click="generatePromptFor(index + 1)"
          >
            {{ page.imagePrompt.trim() ? '重新生成提示词' : '生成提示词' }}
          </el-button>
        </div>
      </div>
    </div>

    <el-dialog v-model="refineVisible" title="按新要求重画" width="520px">
      <p class="dialog-label">第 {{ refineNumber }} 页</p>
      <el-input
        v-model="refineText"
        type="textarea"
        :rows="3"
        placeholder="例如：把背景改成夜晚，加一点星光"
      />
      <p class="dialog-tip">
        会先用文本模型改写这一页的提示词，再重新生成图片。原图会留在历史版本里。
      </p>
      <template #footer>
        <el-button @click="refineVisible = false">取消</el-button>
        <el-button type="primary" @click="confirmRefine">开始重画</el-button>
      </template>
    </el-dialog>

    <el-drawer v-model="historyVisible" :title="`第 ${historyNumber} 页的历史版本`" size="420px">
      <el-skeleton v-if="historyLoading" :rows="3" animated />
      <el-empty v-else-if="history.versions.length === 0" description="这一页还没有生成过图片" />
      <div v-else class="history">
        <div v-for="version in history.versions" :key="version.imageId" class="version">
          <img :src="imageUrl(book!.id, version.imageId)" alt="" />
          <div class="version-body">
            <p class="version-id">
              {{ version.imageId }}
              <el-tag v-if="version.imageId === history.current" size="small" type="success">
                当前
              </el-tag>
              <el-tag v-else-if="version.placeholder" size="small" type="danger">占位图</el-tag>
            </p>
            <p class="version-time">{{ version.createdAt?.slice(0, 19).replace('T', ' ') }}</p>
            <p class="version-prompt">{{ version.prompt }}</p>
            <el-button
              v-if="version.imageId !== history.current"
              size="small"
              @click="useVersion(version.imageId)"
            >
              切回这一版
            </el-button>
          </div>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.illustrate {
  max-width: 1200px;
}

.notice {
  margin-bottom: 12px;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}

.count {
  color: #e6a23c;
}

.hint {
  font-size: 12px;
  color: #909399;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 16px;
}

.cell {
  background: #fff;
  border: 1px solid #e6e8eb;
  border-radius: 6px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.thumb {
  position: relative;
  background: #f2f3f5;
  overflow: hidden;
}

.thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.thumb-empty {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  color: #c0c4cc;
}

.failed-badge {
  position: absolute;
  left: 8px;
  top: 8px;
  font-size: 11px;
  color: #fff;
  background: #f56c6c;
  border-radius: 3px;
  padding: 2px 6px;
}

.cell-head {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 8px 10px 0;
}

.page-no {
  font-size: 13px;
  font-weight: 600;
  color: #2c3e50;
}

.spacer {
  flex: 1;
}

.cell.picked {
  border-color: #409eff;
  box-shadow: 0 0 0 1px #409eff inset;
}

.prompt {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  padding: 8px 10px 10px;
}

.prompt :deep(.el-textarea__inner) {
  font-size: 12px;
}

.prompt-btn {
  font-size: 12px;
  height: auto;
  padding: 0 4px;
}

.dialog-label {
  font-size: 13px;
  color: #606266;
  margin: 0 0 8px;
}

.dialog-tip {
  font-size: 12px;
  color: #909399;
  line-height: 1.6;
  margin: 8px 0 0;
}

.history {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.version {
  display: flex;
  gap: 12px;
  border: 1px solid #e6e8eb;
  border-radius: 6px;
  padding: 10px;
}

.version img {
  width: 96px;
  height: 96px;
  object-fit: cover;
  border-radius: 4px;
  flex-shrink: 0;
}

.version-body {
  min-width: 0;
  flex: 1;
}

.version-id {
  font-size: 12px;
  font-weight: 600;
  color: #2c3e50;
  margin: 0 0 2px;
  display: flex;
  align-items: center;
  gap: 6px;
}

.version-time {
  font-size: 11px;
  color: #a8abb2;
  margin: 0 0 4px;
}

.version-prompt {
  font-size: 11px;
  color: #606266;
  line-height: 1.5;
  margin: 0 0 6px;
  max-height: 52px;
  overflow: hidden;
}
</style>
