import { api } from '@/api/client'
import type { Job } from '@/types/job'

export const getJob = (jobId: string) => api.get<Job>(`/jobs/${jobId}`).then((r) => r.data)

export const cancelJob = (jobId: string) =>
  api.post<{ ok: boolean; reason?: string }>(`/jobs/${jobId}/cancel`).then((r) => r.data)

/** SSE 地址。EventSource 不能带自定义头，所以这里只能拼 URL。 */
export const jobEventsUrl = (jobId: string) => `/api/jobs/${jobId}/events`
