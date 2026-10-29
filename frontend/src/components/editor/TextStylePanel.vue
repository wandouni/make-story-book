<script setup lang="ts">
import { computed } from 'vue'

import NumberSlider from '@/components/common/NumberSlider.vue'
import { useMetaStore } from '@/stores/meta'
import type { PresetPageOptions } from '@/types/book'

/**
 * 预设页的文字样式。
 *
 * 只在**操作结束**时才 emit —— 滑块拖一次会触发几十个 input 事件，
 * 每个都发请求的话既刷屏又会被后到的旧响应覆盖。所以滑块绑 @change
 * （松开才触发），下拉和取色器同理。
 *
 * 滑块一律用 NumberSlider 而不是裸 el-slider：裸的 `@change` 发的是旧值，
 * 详见那个组件的注释。
 */
const props = defineProps<{
  options: PresetPageOptions
  preset: string
  disabled?: boolean
}>()

const emit = defineEmits<{ change: [Partial<PresetPageOptions>] }>()

const meta = useMetaStore()

/** 只有满版叠字才需要选叠字位置。 */
const showOverlayPosition = computed(() => props.preset === 'full-bleed-overlay')

/** 整页文字没有图片槽，调图片焦点没有意义。 */
const showImageFocus = computed(
  () => props.preset !== 'text-only' && props.preset !== 'image-only',
)

const fontOptions = computed(() =>
  meta.fonts.map((f) => ({ value: f.family, label: f.label })),
)

/** 智能配置在这一页留了固定落点。此时 `overlayPosition` 是失效的。 */
const hasOverlayRect = computed(() => props.options.overlayRect !== null)

/**
 * 改叠字位置时**必须同一次把 `overlayRect` 清掉**。
 *
 * 后端 `overlay_zone_mm` 里矩形是短路在 `overlayPosition` 之前的，不清的话
 * 用户点「上」屏幕上毫无变化 —— 一个必然被报成 bug 的陷阱。两者必须在同一个
 * patch 里发出去，分成两次 emit 会出现「先清矩形、后设位置」的中间态请求。
 */
function onOverlayPositionChange(v: string | number | boolean | undefined) {
  emit('change', {
    overlayPosition: v as PresetPageOptions['overlayPosition'],
    overlayRect: null,
  })
}

/** 「恢复自动」：清掉固定落点，回到 `overlayPosition` 的规则。 */
function resetOverlayRect() {
  emit('change', { overlayRect: null, overlayPosition: 'auto' })
}
</script>

<template>
  <div class="panel" :class="{ disabled }">
    <div class="field">
      <label>字体</label>
      <el-select
        :model-value="options.fontFamily"
        size="small"
        :disabled="disabled"
        @change="(v: string) => emit('change', { fontFamily: v })"
      >
        <el-option
          v-for="f in fontOptions"
          :key="f.value"
          :label="f.label"
          :value="f.value"
        />
      </el-select>
    </div>

    <div class="field">
      <label>字号</label>
      <NumberSlider
        :model-value="options.fontSizePt"
        :min="8"
        :max="48"
        :step="0.5"
        show-input
        :disabled="disabled"
        @change="(v) => emit('change', { fontSizePt: v })"
      />
    </div>

    <div class="field">
      <label>行距</label>
      <NumberSlider
        :model-value="options.lineHeight"
        :min="1"
        :max="2.6"
        :step="0.05"
        show-input
        :disabled="disabled"
        @change="(v) => emit('change', { lineHeight: v })"
      />
    </div>

    <div class="field">
      <label>页边距</label>
      <NumberSlider
        :model-value="options.paddingMm"
        :min="0"
        :max="30"
        :step="1"
        show-input
        :disabled="disabled"
        @change="(v) => emit('change', { paddingMm: v })"
      />
    </div>

    <div class="field">
      <label>对齐</label>
      <el-radio-group
        :model-value="options.align"
        size="small"
        :disabled="disabled"
        @change="(v: string | number | boolean | undefined) => emit('change', { align: v as PresetPageOptions['align'] })"
      >
        <el-radio-button value="left">左</el-radio-button>
        <el-radio-button value="center">中</el-radio-button>
        <el-radio-button value="right">右</el-radio-button>
      </el-radio-group>
    </div>

    <div class="field">
      <label>文字颜色</label>
      <el-color-picker
        :model-value="options.color"
        size="small"
        :disabled="disabled"
        @change="(v: string | null) => emit('change', { color: v ?? '#2C3E50' })"
      />
    </div>

    <template v-if="showOverlayPosition">
      <div class="field">
        <label>叠字位置</label>
        <el-radio-group
          :model-value="options.overlayPosition"
          size="small"
          :disabled="disabled"
          @change="onOverlayPositionChange"
        >
          <el-radio-button value="auto">自动</el-radio-button>
          <el-radio-button value="top">上</el-radio-button>
          <el-radio-button value="center">中</el-radio-button>
          <el-radio-button value="bottom">下</el-radio-button>
        </el-radio-group>
        <p class="hint">
          「自动」会看插图，把文字放到画面最空的地方。还没生成插图时先贴底。
        </p>
        <p v-if="hasOverlayRect" class="hint rect-hint">
          当前：智能配置的固定位置（上面的单选不起作用）
          <el-button link type="primary" size="small" :disabled="disabled" @click="resetOverlayRect">
            恢复自动
          </el-button>
        </p>
        <p v-else class="hint">当前：按叠字位置规则自动算</p>
      </div>

      <div class="field">
        <label>底衬不透明度</label>
        <NumberSlider
          :model-value="options.overlayPanelOpacity"
          :min="0"
          :max="1"
          :step="0.02"
          show-input
          :disabled="disabled"
          @change="(v) => emit('change', { overlayPanelOpacity: v })"
        />
        <p class="hint">文字后面那块白色圆角板。调到 0 就只剩字压在图上了。</p>
      </div>
    </template>

    <template v-if="showImageFocus">
      <div class="field">
        <label>图片左右</label>
        <NumberSlider
          :model-value="options.imageFocusX"
          :min="0"
          :max="1"
          :step="0.01"
          :disabled="disabled"
          @change="(v) => emit('change', { imageFocusX: v })"
        />
      </div>

      <div class="field">
        <label>图片上下</label>
        <NumberSlider
          :model-value="options.imageFocusY"
          :min="0"
          :max="1"
          :step="0.01"
          :disabled="disabled"
          @change="(v) => emit('change', { imageFocusY: v })"
        />
      </div>
      <p class="hint">图片被裁切时，用它决定保留哪一块。</p>
    </template>
  </div>
</template>

<style scoped>
.panel {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.panel.disabled {
  opacity: 0.55;
  pointer-events: none;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.field > label {
  font-size: 12px;
  color: #606266;
}

.field :deep(.el-slider) {
  padding-right: 4px;
}

.hint {
  font-size: 11px;
  color: #a8abb2;
  line-height: 1.5;
  margin: 0;
}

/* 固定落点是「这一页被智能配置过」的状态，用主色把它和普通提示区分开。 */
.rect-hint {
  color: #409eff;
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
}
</style>
