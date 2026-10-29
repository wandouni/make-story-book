<script setup lang="ts">
import { computed } from 'vue'

import { isTerminal, type Job, type JobItemStatus } from '@/types/job'

const props = defineProps<{ job: Job; title?: string }>()
const emit = defineEmits<{ cancel: []; close: [] }>()

// 跳过也算"有结论了"。不算的话，一本 20 页跳过 5 页的书永远停在 75%，
// 看起来像卡住了 —— 而它其实已经处理完了。
const settled = computed(() => props.job.completed + props.job.failed + props.job.skipped)

const percentage = computed(() =>
  props.job.total ? Math.round((settled.value / props.job.total) * 100) : 0,
)

const barStatus = computed<'success' | 'exception' | 'warning' | undefined>(() => {
  switch (props.job.status) {
    case 'failed':
      return 'exception'
    case 'cancelled':
      return 'warning'
    case 'done':
      return props.job.failed > 0 ? 'warning' : 'success'
    default:
      return undefined
  }
})

const summary = computed(() => {
  const { completed, failed, skipped, total } = props.job
  const parts = [`${settled.value} / ${total}`]
  if (failed) parts.push(`${failed} 页失败`)
  if (completed) parts.push(`${completed} 页成功`)
  // 「跳过」要说出来：不说的话用户看到 20/20 却只有 15 页有变化，
  // 只会以为功能坏了。每页具体原因在下面的灰点 tooltip 里。
  if (skipped) parts.push(`${skipped} 页跳过`)
  return parts.join(' · ')
})

// Record<JobItemStatus, string> 不是装饰：给 JobItemStatus 加一个状态而忘了
// 在这里补样式，TypeScript 会在编译期报错。这正是我们要的护栏。
const DOT_CLASS: Record<JobItemStatus, string> = {
  pending: 'dot',
  running: 'dot running',
  done: 'dot done',
  failed: 'dot failed',
  skipped: 'dot skipped',
}

const finished = computed(() => isTerminal(props.job))
</script>

<template>
  <el-card shadow="never" class="progress-card">
    <div class="head">
      <span class="title">{{ title ?? '生成进度' }}</span>
      <span class="summary">{{ summary }}</span>
      <div class="spacer" />
      <el-button v-if="!finished" size="small" text type="danger" @click="emit('cancel')">
        取消
      </el-button>
      <el-button v-else size="small" text @click="emit('close')">收起</el-button>
    </div>

    <el-progress :percentage="percentage" :status="barStatus" :stroke-width="10" />

    <p v-if="job.message" class="message">{{ job.message }}</p>

    <div class="dots">
      <el-tooltip
        v-for="item in job.items"
        :key="item.pageNumber"
        :content="`第 ${item.pageNumber} 页：${item.error || item.status}`"
        placement="top"
      >
        <span :class="DOT_CLASS[item.status]" />
      </el-tooltip>
    </div>
  </el-card>
</template>

<style scoped>
.progress-card {
  margin-bottom: 16px;
}

.head {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px;
}

.title {
  font-size: 14px;
  font-weight: 600;
  color: #2c3e50;
}

.summary {
  font-size: 12px;
  color: #909399;
}

.spacer {
  flex: 1;
}

.message {
  font-size: 12px;
  color: #606266;
  margin: 8px 0 0;
}

.dots {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 10px;
}

.dot {
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: #e4e7ed;
  display: inline-block;
}

.dot.running {
  background: #409eff;
  animation: pulse 1s ease-in-out infinite;
}

.dot.done {
  background: #67c23a;
}

.dot.failed {
  background: #f56c6c;
}

/* 跳过：空心灰点，和"还没轮到"（浅灰实心）与"失败"（红）都区分得开。
   空心还顺带表达了"这一页没被碰过"。 */
.dot.skipped {
  background: transparent;
  box-shadow: inset 0 0 0 2px #c0c4cc;
}

@keyframes pulse {
  50% {
    opacity: 0.35;
  }
}
</style>
