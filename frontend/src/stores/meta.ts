import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import {
  getDefaults,
  getExportSpec,
  getFonts,
  getHealth,
  getPresets,
  getStyles,
  getTrimSizes,
} from '@/api/meta'
import type {
  DefaultsMeta,
  ExportSpec,
  FontMeta,
  Health,
  PresetMeta,
  StyleMeta,
  TrimSizeMeta,
} from '@/types/meta'

/** 画风 / 开本 / 预设 / 字体这些元数据是静态的，应用启动拉一次就够。 */
export const useMetaStore = defineStore('meta', () => {
  const styles = ref<StyleMeta[]>([])
  const trimSizes = ref<TrimSizeMeta[]>([])
  const presets = ref<PresetMeta[]>([])
  const fonts = ref<FontMeta[]>([])
  const health = ref<Health | null>(null)
  const exportSpec = ref<ExportSpec | null>(null)
  const defaults = ref<DefaultsMeta | null>(null)
  const loaded = ref(false)

  /**
   * 默认版式。meta 还没拉到时是空串 —— `BookWorkspace` 在 `onMounted` 里
   * `await meta.load()` 过，各步骤组件都是那之后才挂载的，所以实际拿得到值。
   * 空串只是兜底，调用方要么等 load()，要么把它当"未定"处理（别拿它发请求）。
   */
  const defaultPreset = computed(() => defaults.value?.preset ?? '')

  async function load(force = false) {
    if (loaded.value && !force) return
    const [s, t, p, f, h, e, d] = await Promise.all([
      getStyles(),
      getTrimSizes(),
      getPresets(),
      getFonts(),
      getHealth(),
      getExportSpec(),
      getDefaults(),
    ])
    styles.value = s
    trimSizes.value = t
    presets.value = p
    fonts.value = f
    health.value = h
    exportSpec.value = e
    defaults.value = d
    loaded.value = true
  }

  return {
    styles,
    trimSizes,
    presets,
    fonts,
    health,
    exportSpec,
    defaults,
    defaultPreset,
    loaded,
    load,
  }
})
