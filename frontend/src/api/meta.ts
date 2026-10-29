import { api } from '@/api/client'
import type {
  DefaultsMeta,
  ExportSpec,
  FontMeta,
  Health,
  PresetMeta,
  StyleMeta,
  TrimSizeMeta,
} from '@/types/meta'

export const getHealth = () => api.get<Health>('/health').then((r) => r.data)

export const getStyles = () => api.get<StyleMeta[]>('/meta/styles').then((r) => r.data)

export const getTrimSizes = () =>
  api.get<TrimSizeMeta[]>('/meta/trim-sizes').then((r) => r.data)

export const getPresets = () => api.get<PresetMeta[]>('/meta/presets').then((r) => r.data)

export const getDefaults = () => api.get<DefaultsMeta>('/meta/defaults').then((r) => r.data)

export const getFonts = () => api.get<FontMeta[]>('/meta/fonts').then((r) => r.data)

export const getExportSpec = () =>
  api.get<ExportSpec>('/meta/export-spec').then((r) => r.data)
