import axios, { type AxiosRequestConfig } from 'axios'

import { toApiError } from './axiosError'
import { unwrapEnvelope } from './envelope'
import { asPayload } from './httpPayload'
import { needsWriteHeader, WRITE_HEADER_NAME, WRITE_HEADER_VALUE } from './httpPolicy'

/**
 * 控制面客户端：身份**只有**浏览器 cookie（`console_session`，Path=/console）。
 *
 * 这里刻意不 import `import.meta.env.API_KEY`：控制面请求只要出现 ``Authorization``，
 * 就说明两套身份已经混了，那时「拿 Key 管平台」的事故就写进了代码。
 *
 * 同源代理下浏览器会自动带上 cookie，因此不需要 ``withCredentials``；真跨源了，
 * 那也不是加这个参数能解决的（要动 SameSite 与 CORS，本步明确不做）。
 */
const instance = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || undefined,
  timeout: 30000,
})

instance.interceptors.request.use((config) => {
  // 控制面的写操作强制防跨站头；缺了后端返回 20011，而 20011 不是掉登录。
  if (needsWriteHeader(config.method)) {
    config.headers.set(WRITE_HEADER_NAME, WRITE_HEADER_VALUE)
  }
  return config
})

instance.interceptors.response.use(
  (response) => unwrapEnvelope(response.data, 'console'),
  (error) => Promise.reject(toApiError(error, 'console')),
)

export const consoleHttp = {
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
