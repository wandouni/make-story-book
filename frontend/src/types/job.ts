// 与 backend/app/services/job_manager.py 的 Job.to_dict() 一一对应。

export type JobStatus = 'pending' | 'running' | 'done' | 'failed' | 'cancelled'
/**
 * `skipped` = 这一页**本来就不该被处理**（版式不需要、还没有插图……），
 * 和"处理了但失败了"是两回事。目前只有智能排版会发它。
 */
export type JobItemStatus = 'pending' | 'running' | 'done' | 'failed' | 'skipped'

export interface JobItem {
  pageNumber: number
  status: JobItemStatus
  imageId: string | null
  /** 失败原因**或跳过原因**。进度条的 tooltip 直接显示它。 */
  error: string
}

export interface Job {
  id: string
  kind: string
  bookId: string
  status: JobStatus
  message: string
  total: number
  completed: number
  failed: number
  /** 跳过的页数。现有的三个 kind（提示词/生图/精修）恒为 0。 */
  skipped: number
  items: JobItem[]
  createdAt: string
}

export const TERMINAL_STATUSES: JobStatus[] = ['done', 'failed', 'cancelled']

export function isTerminal(job: Job | null): boolean {
  return job !== null && TERMINAL_STATUSES.includes(job.status)
}
