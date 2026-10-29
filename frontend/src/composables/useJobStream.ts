import { onBeforeUnmount, ref } from 'vue'

import { getJob, jobEventsUrl } from '@/api/jobs'
import { isTerminal, type Job } from '@/types/job'

/**
 * 订阅一个任务的 SSE 进度流。
 *
 * 后端每帧推的都是**完整快照**，所以这里直接整体替换 job.value，
 * 不做增量合并 —— 漏帧也不会算错进度。
 *
 * 断线处理：EventSource 会自动重连，但如果任务已经不在服务端了
 * （服务重启会清空任务表），重连只会一直拿到 404 并无限重试。
 * 所以连续几次错误后就关掉，改用轮询确认一次终态。
 */
const MAX_ERRORS_BEFORE_FALLBACK = 3

export function useJobStream() {
  const job = ref<Job | null>(null)
  const connected = ref(false)
  const streamError = ref('')

  let source: EventSource | null = null
  let errors = 0
  let closedByUs = false

  function close() {
    closedByUs = true
    source?.close()
    source = null
    connected.value = false
  }

  async function fallbackPoll(jobId: string, onDone?: (job: Job) => void) {
    try {
      const snapshot = await getJob(jobId)
      job.value = snapshot
      if (isTerminal(snapshot)) onDone?.(snapshot)
      else streamError.value = '进度流已断开，刷新页面可继续查看。'
    } catch {
      // 任务真的没了（服务重启）。给出明确说法，别让进度条永远转下去。
      streamError.value = '任务已丢失（服务可能重启过），请重新发起。'
      job.value = job.value
        ? { ...job.value, status: 'failed', message: '任务已丢失，请重新发起' }
        : null
    }
  }

  function watch(jobId: string, onDone?: (job: Job) => void) {
    close()
    closedByUs = false
    errors = 0
    streamError.value = ''
    job.value = null

    source = new EventSource(jobEventsUrl(jobId))

    source.addEventListener('progress', (event) => {
      errors = 0
      connected.value = true
      const payload = JSON.parse((event as MessageEvent).data) as Job
      job.value = payload
      if (isTerminal(payload)) {
        close()
        onDone?.(payload)
      }
    })

    source.onerror = () => {
      connected.value = false
      if (closedByUs) return
      errors += 1
      if (errors >= MAX_ERRORS_BEFORE_FALLBACK) {
        close()
        void fallbackPoll(jobId, onDone)
      }
    }
  }

  onBeforeUnmount(close)

  return { job, connected, streamError, watch, close }
}
