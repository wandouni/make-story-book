import { api } from '@/api/client'
import type { Block, Page, PresetPageOptions, TextStyle } from '@/types/book'

export interface StoryResponse {
  charCount: number
  paragraphCount: number
  /** 入库后的文本：后端会剥掉 Markdown 标记，可能与提交的不一样。 */
  text: string
}

export interface PaginateResponse {
  pages: Page[]
  warnings: string[]
}

export interface UpdatePageRequest {
  text?: string
  imagePrompt?: string
  preset?: string
  options?: PresetPageOptions
  /** 整页覆盖块。撤销/重做用，只对自由页有效。 */
  blocks?: Block[]
}

export const putStory = (bookId: string, text: string) =>
  api.put<StoryResponse>(`/books/${bookId}/story`, { text }).then((r) => r.data)

export const paginate = (bookId: string, pageCount: number, preset: string) =>
  api
    .post<PaginateResponse>(`/books/${bookId}/paginate`, { pageCount, preset })
    .then((r) => r.data)

export const updatePage = (bookId: string, number: number, payload: UpdatePageRequest) =>
  api.patch<{ page: Page }>(`/books/${bookId}/pages/${number}`, payload).then((r) => r.data.page)

export const deletePage = (bookId: string, number: number) =>
  api.delete<{ pages: Page[] }>(`/books/${bookId}/pages/${number}`).then((r) => r.data.pages)

/**
 * 一次删多页。整单要么全成功要么全失败 —— 后端不会部分删除，
 * 所以不用担心返回的 pages 和请求的页码对不上。
 */
export const batchDeletePages = (bookId: string, pageNumbers: number[]) =>
  api
    .post<{ pages: Page[] }>(`/books/${bookId}/pages/batch-delete`, { pageNumbers })
    .then((r) => r.data.pages)

export const splitPage = (bookId: string, number: number, atCharIndex: number) =>
  api
    .post<{ pages: Page[] }>(`/books/${bookId}/pages/${number}/split`, { atCharIndex })
    .then((r) => r.data.pages)

export const mergeNext = (bookId: string, number: number) =>
  api
    .post<{ pages: Page[] }>(`/books/${bookId}/pages/${number}/merge-next`)
    .then((r) => r.data.pages)

export const reorderPages = (bookId: string, order: string[]) =>
  api.put<{ pages: Page[] }>(`/books/${bookId}/pages/order`, { order }).then((r) => r.data.pages)

// ---------------------------------------------------------------- 模式转换

/** 预设页 → 自由页。后端展开成视觉等价的块，前端不做任何几何推导。 */
export const toFreePage = (bookId: string, number: number) =>
  api.post<{ page: Page }>(`/books/${bookId}/pages/${number}/to-free`).then((r) => r.data.page)

/** 自由页 → 预设页。**有损**，调用前必须让用户二次确认。 */
export const toPresetPage = (bookId: string, number: number, preset: string) =>
  api
    .post<{ page: Page }>(`/books/${bookId}/pages/${number}/to-preset`, { preset })
    .then((r) => r.data.page)

// ---------------------------------------------------------------- 块

export interface NewBlockRequest {
  type: 'text' | 'image'
  xMm?: number
  yMm?: number
  widthMm?: number
  heightMm?: number
  content?: string
  imageId?: string | null
}

/**
 * 块的局部更新。字段全可选：拖拽只发 xMm/yMm，改样式只发 style。
 *
 * 类型专属字段用错会被后端拒绝（400）而不是静默忽略 —— 所以别把
 * content 发给图片块、别把 fit 发给文字块。
 */
export interface UpdateBlockRequest {
  xMm?: number
  yMm?: number
  widthMm?: number
  heightMm?: number
  rotationDeg?: number
  zIndex?: number
  locked?: boolean
  opacity?: number
  content?: string
  style?: TextStyle
  imageId?: string | null
  fit?: 'cover' | 'contain'
  focusX?: number
  focusY?: number
  borderRadiusMm?: number
  bleed?: boolean
}

export const addBlock = (bookId: string, number: number, payload: NewBlockRequest) =>
  api
    .post<{ page: Page }>(`/books/${bookId}/pages/${number}/blocks`, payload)
    .then((r) => r.data.page)

export const updateBlock = (
  bookId: string,
  number: number,
  blockId: string,
  payload: UpdateBlockRequest,
) =>
  api
    .patch<{ page: Page }>(`/books/${bookId}/pages/${number}/blocks/${blockId}`, payload)
    .then((r) => r.data.page)

export const deleteBlock = (bookId: string, number: number, blockId: string) =>
  api
    .delete<{ page: Page }>(`/books/${bookId}/pages/${number}/blocks/${blockId}`)
    .then((r) => r.data.page)
