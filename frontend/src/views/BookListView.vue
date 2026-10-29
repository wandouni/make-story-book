<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { createBook, deleteBook, listBooks } from '@/api/books'
import { imageThumbUrl } from '@/api/illustrate'
import { useMetaStore } from '@/stores/meta'
import type { BookSummary } from '@/types/book'

const router = useRouter()
const meta = useMetaStore()
const books = ref<BookSummary[]>([])
const loading = ref(true)
const creating = ref(false)
const dialogVisible = ref(false)

const form = reactive({
  title: '',
  author: '',
  protagonist: '',
  style: '',
  trimSizeId: '',
  pageCount: 8,
  imageService: 'doubao' as 'doubao' | 'tongyi',
})

const warnings = computed(() => {
  const h = meta.health
  if (!h) return []
  const list: string[] = []
  if (!h.playwright) list.push(`PDF 导出不可用：${h.playwrightReason}`)
  if (!h.keysConfigured.text && !h.keysConfigured.doubao) {
    list.push('尚未配置 API key，生图功能不可用（见 backend/.env.example）')
  }
  return list
})

// ---------------------------------------------------------------- 搜索与分页
//
// 两件事都在前端做：`GET /api/books` 一次返回全部（按 updatedAt 倒序），
// 书的数量级是"一个人写的绘本"，全量在内存里过滤是微秒级的。真到了几千本
// 再说，那时该动的是后端的查询参数，不是在这里加防抖。

const keyword = ref('')
const pageSize = ref(12)
const currentPage = ref(1)

const filtered = computed(() => {
  const q = keyword.value.trim().toLowerCase()
  if (!q) return books.value
  // 只搜标题和作者。不搜 style（那是机器 key），也搜不了主角 ——
  // BookSummary 里根本没有 protagonist 字段。
  return books.value.filter(
    (b) => b.title.toLowerCase().includes(q) || b.author.toLowerCase().includes(q),
  )
})

const paged = computed(() =>
  filtered.value.slice(
    (currentPage.value - 1) * pageSize.value,
    currentPage.value * pageSize.value,
  ),
)

// 换了关键词或每页张数就回第 1 页 —— 停在第 3 页看一个只剩两页的结果集没有意义
watch([keyword, pageSize], () => {
  currentPage.value = 1
})

// 总数缩水时夹紧页码（比如删掉了最后一页上的最后一本）。不夹的话会出现
// "当前页是空的、什么都没有"的鬼状态 —— 用户会以为书全没了。
watch(
  () => filtered.value.length,
  (n) => {
    const max = Math.max(1, Math.ceil(n / pageSize.value))
    if (currentPage.value > max) currentPage.value = max
  },
)

const gridEl = ref<HTMLElement | null>(null)

/**
 * 翻页后卡片区必须回到顶部。
 *
 * 卡片区是个持久存在的滚动容器，翻页只换里面的卡片，浏览器**不会**自动把
 * scrollTop 归零。从第 1 页（滚到了底部）翻到第 2 页（只剩一张卡）就会看到
 * 一片空白，用户以为翻页坏了。
 *
 * refresh()（删除后重拉列表）不归零 —— 用户还在原来位置浏览；若因为夹紧
 * 换了页，上面那个 watcher 会自然触发归零。
 */
watch([currentPage, pageSize, keyword], async () => {
  await nextTick()
  gridEl.value?.scrollTo({ top: 0 })
})

// ---------------------------------------------------------------- 封面

/** 图片文件被手工删过时，<img> 会 404 —— 记下来退回占位块，别留一个碎图。 */
const failedCovers = ref<Set<string>>(new Set())

/**
 * 缩略图按开本比例裁。BookSummary 里没有宽高，只有 trimSizeId，
 * 所以去 meta.trimSizes 里查 —— 那份表是后端 config.TRIM_SIZES 的唯一来源，
 * 在前端另写一份比例表迟早会漂。
 */
function coverRatio(book: BookSummary): string {
  const trim = meta.trimSizes.find((t) => t.id === book.trimSizeId)
  if (!trim || !trim.heightMm) return '3 / 4'
  return `${trim.widthMm} / ${trim.heightMm}`
}

function coverUrl(book: BookSummary): string {
  if (!book.coverImageId || failedCovers.value.has(book.id)) return ''
  // 按显示尺寸的 2 倍取（96px 宽 → 240），视网膜屏也够
  return imageThumbUrl(book.id, book.coverImageId, '240x320')
}

// ---------------------------------------------------------------- 数据

async function refresh() {
  loading.value = true
  try {
    books.value = await listBooks()
    failedCovers.value = new Set()
  } finally {
    loading.value = false
  }
}

function openCreate() {
  form.title = ''
  form.author = ''
  form.protagonist = ''
  form.style = meta.styles[0]?.key ?? ''
  form.trimSizeId = meta.trimSizes.find((t) => t.id === 'SQ200')?.id ?? meta.trimSizes[0]?.id ?? ''
  form.pageCount = 8
  form.imageService = (meta.health?.imageService as 'doubao' | 'tongyi') ?? 'doubao'
  dialogVisible.value = true
}

async function submitCreate() {
  if (!form.title.trim()) {
    ElMessage.warning('请填写标题')
    return
  }
  creating.value = true
  try {
    await createBook({ ...form, title: form.title.trim() })
    dialogVisible.value = false
    ElMessage.success('已创建')
    await refresh()
  } finally {
    creating.value = false
  }
}

async function remove(book: BookSummary) {
  try {
    await ElMessageBox.confirm(
      `删除《${book.title}》？书籍数据、已生成的插图和导出的 PDF 都会一并删除，无法恢复。`,
      '确认删除',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  await deleteBook(book.id)
  ElMessage.success('已删除')
  await refresh()
}

function open(book: BookSummary) {
  router.push({ name: 'workspace', params: { id: book.id } })
}

function formatTime(iso: string) {
  return new Date(iso).toLocaleString('zh-CN', { hour12: false })
}

onMounted(async () => {
  await meta.load()
  await refresh()
})
</script>

<template>
  <div class="page">
    <el-alert
      v-for="warning in warnings"
      :key="warning"
      :title="warning"
      type="warning"
      :closable="false"
      show-icon
      class="warning"
    />

    <div class="toolbar">
      <h2>我的绘本</h2>

      <!-- 一本都没有时不显示搜索框 —— 那是个必然搜不到东西的框 -->
      <el-input
        v-if="books.length"
        v-model="keyword"
        class="search"
        placeholder="搜索标题或作者"
        clearable
      >
        <template #prefix>
          <el-icon><Search /></el-icon>
        </template>
      </el-input>

      <el-button type="primary" @click="openCreate">新建绘本</el-button>
    </div>

    <el-skeleton v-if="loading" :rows="3" animated />

    <el-empty v-else-if="books.length === 0" description="还没有绘本，点右上角新建一本" />

    <div ref="gridEl" class="grid-area">
      <!-- 空状态分两种：搜不到 ≠ 一本书都没有。混在一起用户会以为书被删了 -->
      <el-empty v-if="filtered.length === 0" :image-size="80">
        <template #description>
          <span>没有匹配「{{ keyword }}」的绘本</span>
        </template>
        <el-button size="small" @click="keyword = ''">清除搜索</el-button>
      </el-empty>

      <div v-else class="grid">
        <el-card
          v-for="book in paged"
          :key="book.id"
          shadow="hover"
          class="card"
          @click="open(book)"
        >
          <div class="thumb" :style="{ aspectRatio: coverRatio(book) }">
            <img
              v-if="coverUrl(book)"
              :src="coverUrl(book)"
              :alt="book.title"
              @error="failedCovers.add(book.id)"
            />
            <div v-else class="thumb-empty">
              <el-icon><Picture /></el-icon>
              <span>暂无插图</span>
            </div>
          </div>

          <div class="card-text">
            <h3 class="card-title">{{ book.title }}</h3>
            <p class="card-meta">
              <span v-if="book.author">{{ book.author }} · </span>{{ book.style }} ·
              {{ book.pageCount }} 页
            </p>
            <p class="card-time">{{ formatTime(book.updatedAt) }}</p>
            <div class="spacer" />
            <div class="card-actions">
              <el-button size="small" text type="danger" @click.stop="remove(book)">
                删除
              </el-button>
            </div>
          </div>
        </el-card>
      </div>
    </div>

    <!-- 固定在底部，永远够得着；只有 .grid-area 在滚 -->
    <div v-if="books.length" class="pager">
      <el-pagination
        v-model:current-page="currentPage"
        v-model:page-size="pageSize"
        :page-sizes="[12, 24, 48]"
        :total="filtered.length"
        layout="total, sizes, prev, pager, next"
        background
      />
    </div>

    <el-dialog v-model="dialogVisible" title="新建绘本" width="520px">
      <el-form label-width="90px" label-position="left">
        <el-form-item label="标题" required>
          <el-input v-model="form.title" placeholder="例如：小兔子的冒险" maxlength="60" />
        </el-form-item>
        <el-form-item label="作者">
          <el-input v-model="form.author" placeholder="选填" maxlength="30" />
        </el-form-item>
        <el-form-item label="主角描述">
          <el-input
            v-model="form.protagonist"
            type="textarea"
            :rows="2"
            placeholder="例如：一只白色小兔子，戴着红色围巾。用于保持每页插图的角色一致。"
          />
        </el-form-item>
        <el-form-item label="画风">
          <el-select v-model="form.style" style="width: 100%">
            <el-option v-for="s in meta.styles" :key="s.key" :label="s.label" :value="s.key" />
          </el-select>
        </el-form-item>
        <el-form-item label="开本">
          <el-select v-model="form.trimSizeId" style="width: 100%">
            <el-option
              v-for="t in meta.trimSizes"
              :key="t.id"
              :label="t.label"
              :value="t.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="页数">
          <el-input-number v-model="form.pageCount" :min="1" :max="30" />
        </el-form-item>
        <el-form-item label="生图服务">
          <el-radio-group v-model="form.imageService">
            <el-radio value="doubao">豆包</el-radio>
            <el-radio value="tongyi">通义万相</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="submitCreate">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
/*
  铺满整个 .app-main。**没有 max-width** —— 里面那条内部滚动条会落在容器的
  右边缘，保留 960px 居中就会变成一条浮在窗口正中间的滚动条，看起来像 bug。
  卡片网格本来就是 auto-fill 自适应的，宽一点只会多排几列。

  overflow-y 是安全阀：视口极矮（< ~400px）时整页可滚，保证底部的分页器
  不会被裁掉。正常尺寸下永远不滚，滚动只发生在 .grid-area 里。
*/
.page {
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 20px 24px;
  overflow: hidden auto;
}

.warning {
  margin-bottom: 12px;
  flex-shrink: 0;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-shrink: 0;
}

.toolbar h2 {
  font-size: 18px;
  margin: 0;
  margin-right: auto;
}

.search {
  width: 260px;
}

.grid-area {
  flex: 1 1 auto;
  min-height: 140px;
  overflow-y: auto;
  padding-right: 4px; /* 给滚动条留条缝，别贴着卡片 */
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(340px, 100%), 1fr));
  gap: 16px;
}

.pager {
  flex-shrink: 0;
  display: flex;
  justify-content: flex-end;
}

.card {
  cursor: pointer;
}

.card :deep(.el-card__body) {
  display: flex;
  gap: 12px;
  padding: 12px;
}

/*
  封面缩略图。aspect-ratio 由开本算出来（见 coverRatio），配合服务端的
  fit=cover 重采样，裁剪量就只有亚像素级的那点误差。
*/
.thumb {
  width: 96px;
  flex-shrink: 0;
  border-radius: 4px;
  overflow: hidden;
  background: #f2f3f5;
}

.thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.thumb-empty {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  color: #c0c4cc;
  font-size: 11px;
}

.thumb-empty .el-icon {
  font-size: 20px;
}

.card-text {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.card-title {
  font-size: 15px;
  margin: 0;
  color: #2c3e50;
}

.card-meta {
  font-size: 12px;
  color: #606266;
  margin: 0;
}

.card-time {
  font-size: 12px;
  color: #a8abb2;
  margin: 0;
}

.spacer {
  flex: 1;
}

.card-actions {
  display: flex;
  justify-content: flex-end;
}
</style>
