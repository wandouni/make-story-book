<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, ref } from 'vue'

import {
  exportBook,
  exportFileUrl,
  formatSize,
  type ExportKind,
  type ExportResult,
} from '@/api/export'
import { useBookStore } from '@/stores/book'
import { useMetaStore } from '@/stores/meta'

const bookStore = useBookStore()
const meta = useMetaStore()

const book = computed(() => bookStore.book)
const pages = computed(() => book.value?.pages ?? [])

const playwrightReady = computed(() => meta.health?.playwright ?? false)

/** 导出结果按 kind 存。带 at 是为了给同一个文件名换内容时破缓存。 */
const done = ref<Partial<Record<ExportKind, { result: ExportResult; at: number }>>>({})
const busyKind = ref<ExportKind | null>(null)

const KINDS: {
  id: ExportKind
  title: string
  tag: string
  desc: string
}[] = [
  {
    id: 'preview',
    title: '预览版',
    tag: '屏幕分享',
    desc: '裁切尺寸，不带出血。发给别人看、放进邮件或微信里都合适，体积也小。',
  },
  {
    id: 'print',
    title: '印刷版',
    tag: '交给印厂',
    desc: '四周多出出血区，插图按 300dpi 重采样，并画出裁切标记。这份才是拿去印的。',
  },
]

/**
 * 每种版本的规格。尺寸在**导出之前**就要说清楚 —— 用户得先知道
 * "印刷版比预览版每边大 3mm" 才好判断该拿哪份去印。
 *
 * dpi 从后端取（/api/meta/export-spec），不在前端硬编码：
 * 改了 geometry.PRINT_DPI，这里写的字得跟着变。
 */
const specs = computed(() => {
  const trim = book.value?.trimSize
  if (!trim) return null
  const bleed = book.value?.bleedMm ?? 0
  const print = meta.exportSpec?.printDpi ?? 300
  const screen = meta.exportSpec?.screenDpi ?? 150

  return {
    preview: {
      width: trim.widthMm,
      height: trim.heightMm,
      bleed: 0,
      dpi: screen,
    },
    print: {
      width: trim.widthMm + 2 * bleed,
      height: trim.heightMm + 2 * bleed,
      bleed,
      dpi: print,
    },
  } satisfies Record<ExportKind, { width: number; height: number; bleed: number; dpi: number }>
})

function spec(kind: ExportKind) {
  return specs.value?.[kind]
}

/** 这一版里最低的插图有效 dpi，用来给一个总览。没有插图时返回 null。 */
function lowestDpi(kind: ExportKind): number | null {
  const images = done.value[kind]?.result.report.images ?? []
  if (!images.length) return null
  return Math.min(...images.map((i) => i.effectiveDpi))
}

function fileUrl(kind: ExportKind, inline: boolean): string {
  const entry = done.value[kind]
  if (!entry || !book.value) return '#'
  return exportFileUrl(book.value.id, entry.result.filename, { inline, bust: entry.at })
}

async function run(kind: ExportKind) {
  if (!book.value || busyKind.value) return
  busyKind.value = kind
  try {
    // 同步接口，几秒钟。大书会慢一些，按钮上的 loading 就是全部的进度提示。
    const result = await exportBook(book.value.id, kind)
    done.value[kind] = { result, at: Date.now() }
    ElMessage.success(
      result.report.warnings.length
        ? '导出完成，但有需要注意的地方'
        : '导出完成',
    )
  } catch {
    // 错误提示由 axios 拦截器统一处理
  } finally {
    busyKind.value = null
  }
}

function open(kind: ExportKind) {
  window.open(fileUrl(kind, true), '_blank')
}
</script>

<template>
  <div class="export">
    <el-alert
      v-if="!playwrightReady"
      type="warning"
      :closable="false"
      show-icon
      class="notice"
      title="PDF 导出不可用"
      :description="meta.health?.playwrightReason || '浏览器内核未就绪，请查看后端日志。'"
    />

    <el-empty
      v-if="pages.length === 0"
      description="还没有页面，请先到「故事」步骤分页"
    />

    <div v-else class="cards">
      <section v-for="kind in KINDS" :key="kind.id" class="card">
        <header class="card-head">
          <h3>{{ kind.title }}</h3>
          <el-tag size="small" :type="kind.id === 'print' ? 'success' : 'info'">
            {{ kind.tag }}
          </el-tag>
        </header>

        <p class="desc">{{ kind.desc }}</p>

        <ul v-if="spec(kind.id)" class="specs">
          <li>
            <span>页面尺寸</span>
            <b>{{ spec(kind.id)!.width.toFixed(1) }} × {{ spec(kind.id)!.height.toFixed(1) }} mm</b>
          </li>
          <li>
            <span>出血</span>
            <b>{{ spec(kind.id)!.bleed > 0 ? `四周各 ${spec(kind.id)!.bleed} mm` : '无' }}</b>
          </li>
          <li>
            <span>插图分辨率</span>
            <b>{{ spec(kind.id)!.dpi }} dpi</b>
          </li>
          <li>
            <span>页数</span>
            <b>{{ pages.length }}</b>
          </li>
        </ul>

        <el-button
          type="primary"
          :loading="busyKind === kind.id"
          :disabled="!playwrightReady || (busyKind !== null && busyKind !== kind.id)"
          @click="run(kind.id)"
        >
          {{ done[kind.id] ? '重新导出' : '开始导出' }}
        </el-button>

        <!-- 导出结果。报告如实说明每页插图的实际 dpi，不假装是 300dpi。 -->
        <div v-if="done[kind.id]" class="result">
          <p class="file">
            <span class="name">{{ done[kind.id]!.result.filename }}</span>
            <span class="size">{{ formatSize(done[kind.id]!.result.report.fileSizeBytes) }}</span>
          </p>

          <div class="actions">
            <el-button size="small" @click="open(kind.id)">在线预览</el-button>
            <a :href="fileUrl(kind.id, false)" class="download">
              <el-button size="small" type="primary">下载</el-button>
            </a>
          </div>

          <el-alert
            v-for="warning in done[kind.id]!.result.report.warnings"
            :key="warning"
            type="warning"
            :closable="false"
            show-icon
            class="warning"
            :title="warning"
          />

          <div v-if="done[kind.id]!.result.report.images.length" class="dpi">
            <p class="dpi-head">
              插图分辨率
              <el-tag
                v-if="lowestDpi(kind.id)! < (spec(kind.id)?.dpi ?? 300)"
                size="small"
                type="warning"
              >
                最低 {{ lowestDpi(kind.id) }} dpi
              </el-tag>
              <el-tag v-else size="small" type="success">全部达标</el-tag>
            </p>
            <el-table :data="done[kind.id]!.result.report.images" size="small">
              <el-table-column prop="pageNumber" label="页" width="52" />
              <el-table-column label="原图" width="110">
                <template #default="{ row }">{{ row.sourcePx.width }}×{{ row.sourcePx.height }}</template>
              </el-table-column>
              <el-table-column label="放置尺寸" width="120">
                <template #default="{ row }">
                  {{ row.placedMm.width }}×{{ row.placedMm.height }}mm
                </template>
              </el-table-column>
              <el-table-column label="有效 dpi">
                <template #default="{ row }">
                  <el-tag size="small" :type="row.belowTarget ? 'warning' : 'success'">
                    {{ row.effectiveDpi }}
                  </el-tag>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.export {
  max-width: 1100px;
}

.notice {
  margin-bottom: 16px;
}

.cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
  gap: 16px;
  align-items: start;
}

.card {
  background: #fff;
  border: 1px solid #e6e8eb;
  border-radius: 6px;
  padding: 16px;
}

.card-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.card-head h3 {
  font-size: 15px;
  font-weight: 600;
  color: #2c3e50;
  margin: 0;
}

.desc {
  font-size: 12px;
  color: #909399;
  line-height: 1.7;
  margin: 8px 0 14px;
}

.specs {
  list-style: none;
  margin: 0 0 16px;
  padding: 12px;
  background: #fafafa;
  border-radius: 4px;
}

.specs li {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  line-height: 2;
}

.specs span {
  color: #909399;
}

.specs b {
  color: #2c3e50;
  font-weight: 500;
}

.result {
  margin-top: 16px;
  padding-top: 14px;
  border-top: 1px dashed #e6e8eb;
}

.file {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin: 0 0 10px;
}

.file .name {
  font-size: 13px;
  font-weight: 600;
  color: #2c3e50;
}

.file .size {
  font-size: 12px;
  color: #909399;
}

.actions {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}

.download {
  text-decoration: none;
}

.warning {
  margin-bottom: 8px;
}

.dpi-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: #606266;
  margin: 12px 0 6px;
}
</style>
