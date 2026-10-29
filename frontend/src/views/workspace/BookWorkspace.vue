<script setup lang="ts">
import { computed, onMounted, ref, type Component } from 'vue'
import { useRoute } from 'vue-router'

import FlipBook from '@/components/preview/FlipBook.vue'
import { useBookStore } from '@/stores/book'
import { useMetaStore } from '@/stores/meta'
import { isPresetPage } from '@/types/book'
import EditStep from '@/views/workspace/EditStep.vue'
import ExportStep from '@/views/workspace/ExportStep.vue'
import IllustrateStep from '@/views/workspace/IllustrateStep.vue'
import SetupStep from '@/views/workspace/SetupStep.vue'
import StoryStep from '@/views/workspace/StoryStep.vue'

interface Step {
  key: string
  label: string
  component?: Component
  hint?: string
}

// 步骤按实际创作顺序排。component 为空的还没实现，
// 点进去会看到明确的说明而不是空白页。
const STEPS: Step[] = [
  { key: 'setup', label: '设置', component: SetupStep },
  { key: 'story', label: '故事', component: StoryStep },
  { key: 'illustrate', label: '配图', component: IllustrateStep },
  { key: 'edit', label: '排版', component: EditStep },
  { key: 'export', label: '导出', component: ExportStep },
]

const route = useRoute()
const bookStore = useBookStore()
const meta = useMetaStore()

const activeKey = ref('setup')
const flipbookOpen = ref(false)
const activeIndex = computed(() => STEPS.findIndex((s) => s.key === activeKey.value))
const activeStep = computed(() => STEPS[activeIndex.value])

const book = computed(() => bookStore.book)

/**
 * 3D 封面上书名/作者用的字体。
 *
 * 取第一页的字体：字体是 per-page 存的，同一本书里不会混（重新分页会整批重置），
 * 所以第一页就是整本的字体。页面还没有（或第一页是自由页没有 options）时退回
 * `meta.defaults.fontFamily` —— 那是后端 config.DEFAULT_FONT_FAMILY，前端不该
 * 自己写一个字符串。
 *
 * woff2 地址由字体族在 `meta.fonts` 里查。清单里给的是 400 字重那条，封面要的是
 * 700，所以按文件名约定把 `-400.woff2` 换成 `-700.woff2`；查不到或名字对不上就
 * 返回 undefined，让 FlipBook3D 用它的兜底值。查不到不该报错 —— 封面是装饰。
 */
const coverFontFamily = computed(
  () =>
    book.value?.pages.find(isPresetPage)?.options.fontFamily || meta.defaults?.fontFamily,
)
const coverFontUrl = computed(() => {
  const family = coverFontFamily.value
  if (!family) return undefined
  const found = meta.fonts.find((f) => f.family === family)
  return found?.url.replace(/-400\.woff2$/, '-700.woff2')
})

/** AI 封面生成完：直接写回当前书对象（服务端已持久化，reload 也会拿到同一张）。 */
function onCoverGenerated(imageId: string) {
  if (book.value) book.value.coverImageId = imageId
}

function goTo(index: number) {
  activeKey.value = STEPS[index]?.key ?? 'setup'
}

onMounted(async () => {
  await meta.load()
  await bookStore.load(String(route.params.id))
})
</script>

<template>
  <div class="workspace">
    <div class="bar">
      <RouterLink to="/" class="back">← 全部绘本</RouterLink>
      <h2 v-if="book" class="title">{{ book.title }}</h2>
      <span v-if="book" class="meta">
        {{ book.trimSize.label }} · {{ book.pages.length }} 页
      </span>

      <div class="spacer" />

      <el-button
        v-if="book && book.pages.length"
        size="small"
        @click="flipbookOpen = true"
      >
        翻页预览
      </el-button>
    </div>

    <el-steps :active="activeIndex" simple class="steps">
      <el-step
        v-for="(step, index) in STEPS"
        :key="step.key"
        :title="step.label"
        class="step"
        @click="goTo(index)"
      />
    </el-steps>

    <!--
      骨架屏只在**第一次**加载时出现（`&& !book`）。少了后半句，任何一次
      `bookStore.reload()` 都会把整块 .body 换成骨架屏 —— 于是当前那一步的
      组件被**卸载重建**：它持有的状态（任务流订阅、撤销栈、选中的块）全部
      丢掉。最明显的症状是任务跑完后进度条连同"哪些页被跳过"一起消失，
      用户只看到一个一闪而过的 toast，没法回头查。
      重新加载时保留旧内容直到新数据到位，界面也不会跳。
    -->
    <div v-if="bookStore.loading && !book" class="body">
      <el-skeleton :rows="6" animated />
    </div>

    <el-empty v-else-if="!book" description="绘本不存在或已被删除" />

    <div v-else class="body">
      <component :is="activeStep.component" v-if="activeStep.component" />

      <el-empty v-else :description="activeStep.hint">
        <template #image>
          <div class="pending">待实现</div>
        </template>
      </el-empty>
    </div>

    <FlipBook
      v-if="book"
      v-model="flipbookOpen"
      :book-id="book.id"
      :page-count="book.pages.length"
      :trim-width-mm="book.trimSize.widthMm"
      :trim-height-mm="book.trimSize.heightMm"
      :bleed-mm="book.bleedMm"
      :book-title="book.title"
      :book-author="book.author"
      :font-family="coverFontFamily"
      :font-url="coverFontUrl"
      :cover-image-id="book.coverImageId ?? undefined"
      @cover-generated="onCoverGenerated"
    />
  </div>
</template>

<style scoped>
/* height 而不是 min-height：.app-shell 现在是固定高度，这里铺满它，
   于是下面的 .body 拿到确定高度，各步骤的"自己滚"才有依据。 */
.workspace {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background: #f5f6f8;
}

.bar {
  display: flex;
  align-items: baseline;
  gap: 12px;
  padding: 12px 24px;
  background: #fff;
  border-bottom: 1px solid #e6e8eb;
  flex-shrink: 0;
}

.back {
  font-size: 13px;
  color: #606266;
  text-decoration: none;
}

.back:hover {
  color: #409eff;
}

.title {
  font-size: 16px;
  font-weight: 600;
  margin: 0;
  color: #2c3e50;
}

.meta {
  font-size: 12px;
  color: #909399;
}

.spacer {
  flex: 1;
}

.steps {
  background: #fff;
  border-bottom: 1px solid #e6e8eb;
  flex-shrink: 0;
}

.steps :deep(.el-step) {
  cursor: pointer;
}

/*
  这里是个 **flex 列容器**，不只是个带内边距的盒子 —— 这个决定同时解决两件事：

  1. 想撑满整块区域的步骤（故事页、排版页）用 `flex: 1` + `margin: -20px -24px`。
     列向 flex 里，负 margin 会被 flex-grow 精确抵消：outer hypothetical size
     = 0 + (-40)，自由空间 = (H-40) - (-40) = H，全给这个唯一的 grow 项，于是
     它恰好等于这里的 padding box 高度。**不要改回 `height: 100%`** —— 百分比
     解析的是内容盒（H-40），负 margin 只平移不拉高，底部会露出 40px 灰边。
  2. 普通文档流的步骤（设置、配图、导出）一行都不用改：它们作为 flex item
     `flex: 0 1 auto` + `min-height: auto`，主轴尺寸等于内容高且不会被压缩，
     内容超高就溢出到下面的 overflow-y —— 滚动条还在窗口右缘，和以前一样。

  overflow-x 显式写 hidden：只写 overflow-y: auto 的话 overflow-x 会被算成
  auto，横向多出一条幽灵滚动条。overflow-y 是给第 2 类步骤兜底的。
*/
.body {
  flex: 1;
  min-height: 0;
  padding: 20px 24px;
  display: flex;
  flex-direction: column;
  overflow: hidden auto;
}

.pending {
  font-size: 14px;
  color: #c0c4cc;
  border: 1px dashed #dcdfe6;
  border-radius: 6px;
  padding: 24px 40px;
}
</style>
