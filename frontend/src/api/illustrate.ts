import { api } from '@/api/client'

export interface JobCreated {
  jobId: string
  total: number
}

export interface ImageVersion {
  imageId: string
  pageNumber?: number
  prompt?: string
  basePrompt?: string
  service?: string
  size?: string
  placeholder?: boolean
  createdAt?: string
}

export interface ImageHistory {
  current: string | null
  versions: ImageVersion[]
}

/**
 * 生成提示词。默认**跳过已有提示词的页**，所以整本生成不会覆盖用户手改过的。
 * 单页「重新生成」要传 `overwrite: true`，否则那一页有提示词时点了没反应。
 */
export const generatePrompts = (
  bookId: string,
  pageNumbers?: number[],
  overwrite = false,
) =>
  api
    .post<JobCreated>(`/books/${bookId}/prompts/generate`, { pageNumbers, overwrite })
    .then((r) => r.data)

export const illustrateBook = (bookId: string, pageNumbers?: number[]) =>
  api.post<JobCreated>(`/books/${bookId}/illustrate`, { pageNumbers }).then((r) => r.data)

export const illustratePage = (bookId: string, number: number) =>
  api.post<JobCreated>(`/books/${bookId}/pages/${number}/illustrate`).then((r) => r.data)

export const refinePage = (bookId: string, number: number, instruction: string) =>
  api
    .post<JobCreated>(`/books/${bookId}/pages/${number}/refine`, { instruction })
    .then((r) => r.data)

export const getImageHistory = (bookId: string, number: number) =>
  api
    .get<ImageHistory>(`/books/${bookId}/pages/${number}/image-history`)
    .then((r) => r.data)

export const selectImage = (bookId: string, number: number, imageId: string) =>
  api
    .post<{ page: unknown }>(`/books/${bookId}/pages/${number}/image/select`, { imageId })
    .then((r) => r.data)

/** 插图地址。imageId 不可变，内容不会变，可以放心让浏览器缓存。 */
export const imageUrl = (bookId: string, imageId: string | null) =>
  imageId ? `/api/assets/books/${bookId}/images/${imageId}.png` : ''

/**
 * 列表页的封面缩略图。**必须走 `.jpg?box=` 这个重采样端点，不能用 imageUrl 的
 * `.png` 原图** —— 原图单张 600~800KB，一页 12 张卡片就是 8MB，还要解码十几张
 * 1024² 的位图。重采样端点（见 backend/app/api/assets.py）按 box 做磁盘缓存，
 * 带一年期的 Cache-Control，而 imageId 不可变，所以这个 URL 可以放心长缓存。
 *
 * box 取显示尺寸的 2 倍左右，视网膜屏也够清晰。
 */
export const imageThumbUrl = (bookId: string, imageId: string | null, box = '240x320') =>
  imageId
    ? `/api/assets/books/${bookId}/images/${imageId}.jpg?box=${box}&fit=cover`
    : ''

/** 生成 AI 封面背景图。 */
export const generateCover = (bookId: string) =>
  api.post<JobCreated>(`/books/${bookId}/cover/generate`).then((r) => r.data)
