import axios, { AxiosError } from 'axios'
import { ElMessage } from 'element-plus'

export const api = axios.create({
  baseURL: '/api',
  // 分页和 prompt 生成会调 LLM，可能跑好几分钟
  timeout: 600_000,
})

function describe(error: AxiosError): string {
  const detail = (error.response?.data as { detail?: unknown } | undefined)?.detail
  if (typeof detail === 'string') return detail
  if (detail) return JSON.stringify(detail)
  if (error.code === 'ECONNABORTED') return '请求超时'
  if (!error.response) return '连不上后端，请确认服务已启动'
  return `请求失败（HTTP ${error.response.status}）`
}

api.interceptors.response.use(
  (response) => response,
  (error: AxiosError) => {
    ElMessage.error(describe(error))
    return Promise.reject(error)
  },
)
