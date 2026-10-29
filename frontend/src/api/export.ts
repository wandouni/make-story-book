import { api } from '@/api/client'

/**
 * 导出**没有**走任务表：一次几秒钟就完事，而任务表是为"几十张图、每张要等
 * 十几秒"的生图设计的。所以这里是普通的同步请求，不用订阅 SSE。
 */
export type ExportKind = 'preview' | 'print'

/** 报告里每一页插图的实际情况。 */
export interface ExportImageInfo {
  pageNumber: number
  imageId: string
  sourcePx: { width: number; height: number }
  placedMm: { width: number; height: number }
  /** 这张图放进这个盒子后**实际**有多少 dpi，不是目标值。 */
  effectiveDpi: number
  targetDpi: number
  belowTarget: boolean
}

export interface ExportReport {
  kind: ExportKind
  pageCount: number
  /** 含出血的成品尺寸（印刷版比裁切尺寸每边大 bleedMm）。 */
  pageSizeMm: { width: number; height: number }
  bleedMm: number
  targetDpi: number
  fileSizeBytes: number
  images: ExportImageInfo[]
  warnings: string[]
}

export interface ExportResult {
  kind: ExportKind
  filename: string
  url: string
  report: ExportReport
}

export const exportBook = (bookId: string, kind: ExportKind) =>
  api.post<ExportResult>(`/books/${bookId}/export`, { kind }).then((r) => r.data)

/**
 * 文件地址。走 Vite 代理，所以是 `/api/...` 的裸路径而不是 axios 的 baseURL。
 *
 * - `inline` 让浏览器直接打开而不是下载 —— 打印前想先翻一遍时用得上
 * - `bust` 是给**同一个文件名**换内容用的：重新导出的文件名不变
 *   （preview.pdf / print.pdf），不加时间戳的话浏览器会把旧的那份拿出来
 */
export function exportFileUrl(
  bookId: string,
  filename: string,
  opts: { inline?: boolean; bust?: number } = {},
): string {
  const query = new URLSearchParams()
  if (opts.inline) query.set('inline', '1')
  if (opts.bust) query.set('t', String(opts.bust))
  const suffix = query.toString()
  return `/api/books/${bookId}/exports/${encodeURIComponent(filename)}${suffix ? `?${suffix}` : ''}`
}

export function formatSize(bytes: number): string {
  if (bytes <= 0) return '—'
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}
