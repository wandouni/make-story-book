<script setup lang="ts">
import { ref, watch } from 'vue'

/**
 * 滑块（el-slider 的正确用法封装）。
 *
 * 为什么要包一层：el-slider 松手时 emit 的 `change` 带的是 **props.modelValue**，
 * 不是刚拖出来的那个值 —— 见 element-plus/es/components/slider/src/composables/
 * use-slide.mjs 里的 `emitChange`。它内部还有一个
 * `watch(() => initData.dragging, v => { if (!v) setValues() })`，松手瞬间用
 * props.modelValue 把内部值**重置回去**（use-watch.mjs）。
 *
 * 只读绑定（`:model-value` + `@change`）时父组件从不回写，props.modelValue 就
 * 一直是旧值。表现是：拖动过程中数字明明在变，一松手弹回原值，提交上去的也是
 * 原值 —— 看起来像"设置按钮不生效"。带 `show-input` 时数字框走的是同一个
 * emitChange，所以手动输入也一样失效。
 *
 * 这里自己接住 `update:model-value`（它带的是新值，拖动中每一步都会发），
 * 只在松手/提交时才用本地值 emit `change` —— 既拿得到新值，又保持
 * "拖一次只提交一次"，不会每动一像素就发一个请求。
 */
const props = withDefaults(
  defineProps<{
    modelValue: number
    min: number
    max: number
    step?: number
    disabled?: boolean
    /** 是否带右侧的数字输入框。 */
    showInput?: boolean
  }>(),
  { step: 1, disabled: false, showInput: false },
)

const emit = defineEmits<{
  'update:modelValue': [number]
  change: [number]
}>()

/** 拖动/输入期间的真值。props 只在外部改动时才是权威。 */
const value = ref(props.modelValue)

// 外部改值要跟着走：换页、套用版式、保存失败后回滚
watch(
  () => props.modelValue,
  (v) => {
    value.value = v
  },
)

function onUpdate(v: number | number[]) {
  if (typeof v !== 'number') return
  value.value = v
  emit('update:modelValue', v)
}

function onChange() {
  // 这里**不能**用事件参数：见文件头注释，el-slider 给的是旧值
  emit('change', value.value)
}
</script>

<template>
  <el-slider
    class="number-slider"
    :model-value="value"
    :min="min"
    :max="max"
    :step="step"
    size="small"
    :disabled="disabled"
    :show-input="showInput"
    :show-input-controls="false"
    @update:model-value="onUpdate"
    @change="onChange"
  />
</template>

<style scoped>
/* show-input 的数字框默认宽 130px，在 280px 宽的面板里把滑道挤到只剩 87px ——
   8~48pt 摊在 80 个 step 上，一格一像素，没法细调。压窄数字框把滑道让出来。 */
.number-slider :deep(.el-slider__input) {
  width: 64px;
}
</style>
