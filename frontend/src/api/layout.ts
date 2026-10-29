import { api } from '@/api/client'
import type { JobCreated } from '@/api/illustrate'

/** 智能排版的候选框。位置是 mm，相对成品区。 */
export interface LayoutCandidate {
  xMm: number
  yMm: number
  widthMm: number
  heightMm: number
  /** 越小越空。只在候选之间比较有意义，绝对值没有单位。 */
  score: number
  /** `box` = 二维滑窗找出来的；`band` = 现有 overlay_band 那条全宽横带。 */
  kind: string
  luminance: number
  /** 这个框排下这段文字该用多大字号 —— 是算出来的，不是模型给的。 */
  fontSizePt: number
  lineHeight: number
  lines: number
  /** 到了最小字号还是放不下。界面上要提示用户。 */
  overflow: boolean
}

export interface LayoutPreview {
  applicable: boolean
  /** `applicable` 为 false 时的原因：「版式不需要」「还没有插图」…… */
  reason: string
  boxMm?: number[]
  candidates: LayoutCandidate[]
}

export interface LayoutUndoStatus {
  available: boolean
  createdAt: string | null
  pages: number
}

/**
 * 整本书一键智能排版。**直接写进书籍数据**，所以只给一次撤销机会。
 *
 * 没有可配置的页面时后端返回 400（detail 里带跳过原因汇总）。
 */
export const autoLayout = (bookId: string) =>
  api.post<JobCreated>(`/books/${bookId}/layout/auto`).then((r) => r.data)

/** 看这一页的候选框。**不写任何东西、不调模型**，可以反复看。 */
export const getLayoutPreview = (bookId: string, number: number) =>
  api.get<LayoutPreview>(`/books/${bookId}/layout/preview/${number}`).then((r) => r.data)

/**
 * 有没有可撤销的记录。撤销按钮靠它决定显示与否 —— 按钮永远可见然后点了
 * 404 是很糟的体验，而这个状态刷新页面后必须还准。
 */
export const getLayoutUndo = (bookId: string) =>
  api.get<LayoutUndoStatus>(`/books/${bookId}/layout/undo`).then((r) => r.data)

export const undoLayout = (bookId: string) =>
  api
    .post<{ restored: number; skipped: number; createdAt: string | null }>(
      `/books/${bookId}/layout/undo`,
    )
    .then((r) => r.data)
