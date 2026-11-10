import axios, { type AxiosRequestConfig } from 'axios'

import { toApiError } from './axiosError'
import { ApiError, unwrapEnvelope } from './envelope'
import { asPayload } from './httpPayload'

/**
 * 数据面客户端：身份**只有** API Key，注入 `Authorization: Bearer`。
 *
 * 与 consoleHttp 是两个实例、两段拦截器，不共用一个「按条件加头」的入口：
 * 一旦合并，就会出现「控制面请求顺手带上了明文 Key」这种事故，而它是不可撤回的泄露。
 */
const instance = axios.create({
  // 留空即相对路径，由 Vite 的 /api 代理同源转发；填绝对地址会绕开代理。
  baseURL: import.meta.env.VITE_API_BASE_URL || undefined,
  timeout: 30000,
})

instance.interceptors.request.use((config) => {
  // 本票 Key 来自构建期环境变量；T10 会换成页面选中的那把 Key，但注入点保持在这一处。
  const key = (import.meta.env.API_KEY ?? '').trim()
  if (key === '') {
    throw new ApiError(
      -1,
      'data',
      '',
      '数据面缺少 API_KEY：检查 frontend/.env 的 API_KEY，并与后端的 DEV_API_KEY 对齐',
    )
  }
  config.headers.set('Authorization', `Bearer ${key}`)
  return config
})

instance.interceptors.response.use(
  (response) => unwrapEnvelope(response.data, 'data'),
  (error) => Promise.reject(toApiError(error, 'data')),
)

/**
 * 暴露的是「已经剥过信封」的方法：运行时返回 data，所以返回类型直接是 T，
 * 不让调用方再写一层 `response.data.data` 的猜测。
 */
export const dataHttp = {
  get<T>(url: string, config?: AxiosRequestConfig): Promise<T> {
    return asPayload<T>(instance.get<unknown, T>(url, config))
  },
  post<T>(url: string, body?: unknown, config?: AxiosRequestConfig): Promise<T> {
    return asPayload<T>(instance.post<unknown, T>(url, body, config))
  },
  put<T>(url: string, body?: unknown, config?: AxiosRequestConfig): Promise<T> {
    return asPayload<T>(instance.put<unknown, T>(url, body, config))
  },
  patch<T>(url: string, body?: unknown, config?: AxiosRequestConfig): Promise<T> {
    return asPayload<T>(instance.patch<unknown, T>(url, body, config))
  },
  delete<T>(url: string, config?: AxiosRequestConfig): Promise<T> {
    return asPayload<T>(instance.delete<unknown, T>(url, config))
  },
}
