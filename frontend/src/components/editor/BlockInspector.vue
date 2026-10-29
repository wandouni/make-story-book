<script setup lang="ts">
import { computed } from 'vue'

import type { UpdateBlockRequest } from '@/api/pages'
import NumberSlider from '@/components/common/NumberSlider.vue'
import { useMetaStore } from '@/stores/meta'
import type { Block, ImageBlock, TextBlock, TextStyle } from '@/types/book'

/**
 * 选中块的属性面板。
 *
 * 文字样式一律提交**完整**的 TextStyle 对象（见 `patchStyle`）：后端拿到
 * style 就整体替换，只发一个字段的话其余字段会被 Pydantic 的默认值填回去，
 * 用户先前调好的字号行距就无声无息地没了。
 *
 * 滑块一律用 NumberSlider 而不是裸 el-slider：裸的 `@change` 发的是旧值，
 * 详见那个组件的注释。
 */
const props = defineProps<{
  block: Block
  /** 书的出血量，只用于提示文案 —— 溢出量由后端按渲染模式算。 */
  bleedMm: number
  disabled?: boolean
}>()

const emit = defineEmits<{
  change: [string, UpdateBlockRequest]
  /** 层级调整交给父组件算 —— 它才知道所有块的 zIndex 范围。 */
  layer: [string, 'front' | 'back']
  remove: [string]
}>()

const meta = useMetaStore()

const textBlock = computed(() => (props.block.type === 'text' ? (props.block as TextBlock) : null))
const imageBlock = computed(() =>
  props.block.type === 'image' ? (props.block as ImageBlock) : null,
)

const fontOptions = computed(() => meta.fonts.map((f) => ({ value: f.family, label: f.label })))

function patch(payload: UpdateBlockRequest) {
  emit('change', props.block.id, payload)
}

/** 合并后整体提交，不能只发改动的那个字段。 */
function patchStyle(stylePatch: Partial<TextStyle>) {
  const style = textBlock.value?.style
  if (!style) return
  patch({ style: { ...style, ...stylePatch } })
}

/** 手动调过字号就关掉自动缩字，否则用户调好的字号会被系统改回去。 */
function patchFontSize(fontSizePt: number) {
  patchStyle({ fontSizePt, autoFit: false })
}

const num = (v: number | undefined) => (v === undefined ? 0 : v)
</script>

<template>
  <div class="inspector">
    <h4 class="group">位置与大小</h4>
    <div class="grid2">
      <div class="field">
        <label>X（mm）</label>
        <el-input-number
          :model-value="block.xMm"
          :controls="false"
          :step="1"
          size="small"
          :disabled="disabled || block.locked"
          @change="(v: number | undefined) => patch({ xMm: num(v) })"
        />
      </div>
      <div class="field">
        <label>Y（mm）</label>
        <el-input-number
          :model-value="block.yMm"
          :controls="false"
          :step="1"
          size="small"
          :disabled="disabled || block.locked"
          @change="(v: number | undefined) => patch({ yMm: num(v) })"
        />
      </div>
      <div class="field">
        <label>宽（mm）</label>
        <el-input-number
          :model-value="block.widthMm"
          :controls="false"
          :min="5"
          :step="1"
          size="small"
          :disabled="disabled || block.locked"
          @change="(v: number | undefined) => patch({ widthMm: num(v) })"
        />
      </div>
      <div class="field">
        <label>高（mm）</label>
        <el-input-number
          :model-value="block.heightMm"
          :controls="false"
          :min="5"
          :step="1"
          size="small"
          :disabled="disabled || block.locked"
          @change="(v: number | undefined) => patch({ heightMm: num(v) })"
        />
      </div>
    </div>

    <div class="field">
      <label>旋转</label>
      <NumberSlider
        :model-value="block.rotationDeg"
        :min="-180"
        :max="180"
        :step="1"
        show-input
        :disabled="disabled || block.locked"
        @change="(v) => patch({ rotationDeg: v })"
      />
    </div>

    <div class="field">
      <label>不透明度</label>
      <NumberSlider
        :model-value="block.opacity"
        :min="0.05"
        :max="1"
        :step="0.05"
        :disabled="disabled"
        @change="(v) => patch({ opacity: v })"
      />
    </div>

    <div class="row">
      <el-button size="small" :disabled="disabled" @click="emit('layer', block.id, 'front')">
        置顶
      </el-button>
      <el-button size="small" :disabled="disabled" @click="emit('layer', block.id, 'back')">
        置底
      </el-button>
      <el-checkbox
        :model-value="block.locked"
        size="small"
        :disabled="disabled"
        @change="(v: boolean | string | number) => patch({ locked: Boolean(v) })"
      >
        锁定
      </el-checkbox>
    </div>

    <!-- ------------------------------------------------ 文字块 -->
    <template v-if="textBlock">
      <h4 class="group">文字</h4>
      <div class="field">
        <label>内容</label>
        <el-input
          type="textarea"
          :model-value="textBlock.content"
          :rows="4"
          resize="vertical"
          :disabled="disabled || block.locked"
          @change="(v: string) => patch({ content: v })"
        />
      </div>

      <div class="field">
        <label>字体</label>
        <el-select
          :model-value="textBlock.style.fontFamily"
          size="small"
          :disabled="disabled || block.locked"
          @change="(v: string) => patchStyle({ fontFamily: v })"
        >
          <el-option v-for="f in fontOptions" :key="f.value" :label="f.label" :value="f.value" />
        </el-select>
      </div>

      <div class="field">
        <label>字号（pt）</label>
        <NumberSlider
          :model-value="textBlock.style.fontSizePt"
          :min="8"
          :max="72"
          :step="0.5"
          show-input
          :disabled="disabled || block.locked"
          @change="patchFontSize"
        />
      </div>

      <div class="field">
        <label>行距</label>
        <NumberSlider
          :model-value="textBlock.style.lineHeight"
          :min="1"
          :max="2.6"
          :step="0.05"
          show-input
          :disabled="disabled || block.locked"
          @change="(v) => patchStyle({ lineHeight: v })"
        />
      </div>

      <div class="field">
        <label>字距（px）</label>
        <NumberSlider
          :model-value="textBlock.style.letterSpacing"
          :min="-2"
          :max="10"
          :step="0.5"
          :disabled="disabled || block.locked"
          @change="(v) => patchStyle({ letterSpacing: v })"
        />
      </div>

      <div class="field">
        <label>内边距（mm）</label>
        <NumberSlider
          :model-value="textBlock.style.paddingMm"
          :min="0"
          :max="30"
          :step="1"
          :disabled="disabled || block.locked"
          @change="(v) => patchStyle({ paddingMm: v })"
        />
      </div>

      <div class="field">
        <label>水平对齐</label>
        <el-radio-group
          :model-value="textBlock.style.align"
          size="small"
          :disabled="disabled || block.locked"
          @change="(v: string | number | boolean | undefined) => patchStyle({ align: v as TextStyle['align'] })"
        >
          <el-radio-button value="left">左</el-radio-button>
          <el-radio-button value="center">中</el-radio-button>
          <el-radio-button value="right">右</el-radio-button>
          <el-radio-button value="justify">两端</el-radio-button>
        </el-radio-group>
      </div>

      <div class="field">
        <label>垂直对齐</label>
        <el-radio-group
          :model-value="textBlock.style.verticalAlign"
          size="small"
          :disabled="disabled || block.locked"
          @change="(v: string | number | boolean | undefined) => patchStyle({ verticalAlign: v as TextStyle['verticalAlign'] })"
        >
          <el-radio-button value="top">上</el-radio-button>
          <el-radio-button value="middle">中</el-radio-button>
          <el-radio-button value="bottom">下</el-radio-button>
        </el-radio-group>
      </div>

      <div class="field">
        <label>文字方向</label>
        <el-radio-group
          :model-value="textBlock.style.writingMode"
          size="small"
          :disabled="disabled || block.locked"
          @change="(v: string | number | boolean | undefined) => patchStyle({ writingMode: v as TextStyle['writingMode'] })"
        >
          <el-radio-button value="horizontal-tb">横排</el-radio-button>
          <el-radio-button value="vertical-rl">竖排</el-radio-button>
        </el-radio-group>
      </div>

      <div class="row">
        <div class="field">
          <label>文字颜色</label>
          <el-color-picker
            :model-value="textBlock.style.color"
            size="small"
            :disabled="disabled || block.locked"
            @change="(v: string | null) => patchStyle({ color: v ?? '#2C3E50' })"
          />
        </div>
        <div class="field">
          <label>底色</label>
          <el-color-picker
            :model-value="textBlock.style.backgroundColor ?? ''"
            size="small"
            show-alpha
            :disabled="disabled || block.locked"
            @change="(v: string | null) => patchStyle({ backgroundColor: v || null })"
          />
        </div>
        <el-button
          v-if="textBlock.style.backgroundColor"
          size="small"
          text
          :disabled="disabled || block.locked"
          @click="patchStyle({ backgroundColor: null })"
        >
          清除底色
        </el-button>
      </div>

      <el-checkbox
        :model-value="textBlock.style.autoFit"
        size="small"
        :disabled="disabled || block.locked"
        @change="(v: boolean | string | number) => patchStyle({ autoFit: Boolean(v) })"
      >
        超出时自动缩小字号
      </el-checkbox>
    </template>

    <!-- ------------------------------------------------ 图片块 -->
    <template v-if="imageBlock">
      <h4 class="group">图片</h4>
      <div class="field">
        <label>填充方式</label>
        <el-radio-group
          :model-value="imageBlock.fit"
          size="small"
          :disabled="disabled || block.locked"
          @change="(v: string | number | boolean | undefined) => patch({ fit: v as ImageBlock['fit'] })"
        >
          <el-radio-button value="cover">裁切填满</el-radio-button>
          <el-radio-button value="contain">完整放入</el-radio-button>
        </el-radio-group>
      </div>

      <div class="field">
        <label>焦点左右</label>
        <NumberSlider
          :model-value="imageBlock.focusX"
          :min="0"
          :max="1"
          :step="0.01"
          :disabled="disabled || block.locked || imageBlock.fit === 'contain'"
          @change="(v) => patch({ focusX: v })"
        />
      </div>

      <div class="field">
        <label>焦点上下</label>
        <NumberSlider
          :model-value="imageBlock.focusY"
          :min="0"
          :max="1"
          :step="0.01"
          :disabled="disabled || block.locked || imageBlock.fit === 'contain'"
          @change="(v) => patch({ focusY: v })"
        />
      </div>
      <p class="hint">图片被裁切时，用它决定保留哪一块。</p>

      <div class="field">
        <label>圆角（mm）</label>
        <NumberSlider
          :model-value="imageBlock.borderRadiusMm"
          :min="0"
          :max="40"
          :step="1"
          :disabled="disabled || block.locked"
          @change="(v) => patch({ borderRadiusMm: v })"
        />
      </div>

      <div class="field">
        <el-checkbox
          :model-value="imageBlock.bleed"
          size="small"
          :disabled="disabled || block.locked"
          @change="(v: boolean | string | number) => patch({ bleed: Boolean(v) })"
        >
          满版（四周溢出出血区）
        </el-checkbox>
      </div>
      <p class="hint">
        满版块在印刷模式下向四周各溢出 {{ bleedMm }}mm，预览模式不溢出。坐标按成品区记，
        所以切换预览/印刷不会改变图片的实际裁切。
      </p>
    </template>

    <el-button class="remove" size="small" type="danger" plain :disabled="disabled" @click="emit('remove', block.id)">
      删除这个块
    </el-button>
  </div>
</template>

<style scoped>
.inspector {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.group {
  font-size: 12px;
  font-weight: 600;
  color: #909399;
  margin: 8px 0 0;
}

.group:first-of-type {
  margin-top: 0;
}

.grid2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 5px;
  min-width: 0;
}

.field > label {
  font-size: 12px;
  color: #606266;
}

.row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.hint {
  font-size: 11px;
  color: #a8abb2;
  line-height: 1.5;
  margin: 0;
}

.remove {
  margin-top: 8px;
  width: 100%;
}
</style>
