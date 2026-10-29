<script setup lang="ts">
import { useMetaStore } from '@/stores/meta'

/**
 * 预设版式选择。
 *
 * 每个选项画一张缩略示意图 —— 光看"图文上下（图在上）"这样的文字，
 * 用户得点进去才知道长什么样，六选一要点六次。缩略图直接说清楚。
 */
defineProps<{ modelValue: string; disabled?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [string] }>()

const meta = useMetaStore()

/** 用 CSS 画出各版式的骨架，色块位置与实际 grid 一致。 */
const SKELETON: Record<string, string[]> = {
  'image-top': ['image', 'text'],
  'image-bottom': ['text', 'image'],
  'split-lr': ['image', 'text'],
  'full-bleed-overlay': ['overlay'],
  'image-only': ['image'],
  'text-only': ['text'],
}
</script>

<template>
  <div class="presets" :class="{ disabled }">
    <button
      v-for="preset in meta.presets"
      :key="preset.id"
      type="button"
      class="preset"
      :class="{ active: preset.id === modelValue }"
      :disabled="disabled"
      @click="emit('update:modelValue', preset.id)"
    >
      <span class="thumb" :class="`thumb-${preset.id}`">
        <template v-for="(part, i) in SKELETON[preset.id] ?? []" :key="i">
          <span v-if="part === 'image'" class="bar image" />
          <span v-else-if="part === 'text'" class="bar text" />
          <span v-else class="bar overlay" />
        </template>
      </span>
      <span class="label">{{ preset.label }}</span>
    </button>
  </div>
</template>

<style scoped>
.presets {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
}

.presets.disabled {
  opacity: 0.5;
  pointer-events: none;
}

.preset {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 5px;
  padding: 7px 4px;
  background: #fff;
  border: 1px solid #dcdfe6;
  border-radius: 5px;
  cursor: pointer;
  transition: border-color 0.15s;
}

.preset:hover {
  border-color: #a0cfff;
}

.preset.active {
  border-color: #409eff;
  box-shadow: 0 0 0 1px #409eff inset;
}

.thumb {
  width: 40px;
  height: 52px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 3px;
  background: #f5f7fa;
  border-radius: 2px;
  position: relative;
}

.thumb-split-lr {
  flex-direction: row;
}

.bar {
  border-radius: 1px;
  flex: 1;
}

.bar.image {
  background: #b3d8ff;
}

.bar.text {
  background: #dcdfe6;
}

.thumb-full-bleed-overlay {
  background: #b3d8ff;
}

.bar.overlay {
  position: absolute;
  left: 3px;
  right: 3px;
  bottom: 3px;
  height: 13px;
  background: rgb(255 255 255 / 78%);
}

.label {
  font-size: 11px;
  color: #606266;
  line-height: 1.3;
  text-align: center;
}

.preset.active .label {
  color: #409eff;
}
</style>
