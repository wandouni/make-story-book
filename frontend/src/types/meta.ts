export interface StyleMeta {
  key: string
  label: string
  prompt: string
}

export interface TrimSizeMeta {
  id: string
  label: string
  widthMm: number
  heightMm: number
}

export interface PresetMeta {
  id: string
  label: string
  slots: string[]
  image_ratio: number
}

export interface FontMeta {
  family: string
  label: string
  url: string
  weights: number[]
}

/** 生图服务是否可用。available 为 false 时 reason 说明为什么。 */
export interface ImageServiceStatus {
  available: boolean
  reason: string
}

/** 导出的目标分辨率。来自后端 geometry，不要在别处硬编码这两个数。 */
export interface ExportSpec {
  printDpi: number
  screenDpi: number
}

/**
 * 新建页 / 重新分页的默认值，来自 config.DEFAULT_PRESET、
 * config.DEFAULT_OVERLAY_PANEL_OPACITY 和 config.DEFAULT_FONT_FAMILY。
 * **不要在组件里写死这些值** —— 用户点「分页」时发出去的是界面上的值，
 * 硬编码会让后端常量形同虚设。
 *
 * fontFamily 只在"页面 options 里没有字体"时才用得上（正常每页都有），
 * 但组件里那份兜底默认值同样不该自己写一个字符串。
 */
export interface DefaultsMeta {
  preset: string
  overlayPanelOpacity: number
  fontFamily: string
}

export interface Health {
  ok: boolean
  version: string
  playwright: boolean
  playwrightReason: string
  imageService: string
  keysConfigured: { text: boolean; doubao: boolean }
  /**
   * 比 keysConfigured 更准：key 填了不代表这个服务能用。
   * 例如 API_KEY 是火山方舟的，发给通义的 DashScope 只会 401。
   */
  imageServices: Record<string, ImageServiceStatus>
}
