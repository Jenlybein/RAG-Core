import axios, { type AxiosRequestConfig } from 'axios'

import { getActiveKey } from './activeKey'
import { toApiError } from './axiosError'
import { ApiError, unwrapEnvelope } from './envelope'
import { asPayload } from './httpPayload'

/**
 * 数据面客户端：身份**只有**用户当前选中的 API Key，注入 `Authorization: Bearer`。
 *
 * **只给真实检索用。** 这个文件现在只应被 `services/ragService.ts` import——
 * 全仓库唯一的调用点是 `POST /api/v1/rag/retrieve`。出现第二个调用点就是身份混了：
 * 登录 / 我的 / 树 / 建库 / 上传 / 重建 / 删文档 / Key 与配置全部走控制面会话门
 * （`lib/consoleHttp.ts`），那扇门不认 Bearer。为什么把这件事写成一句注释而不是
 * 一个共享开关：共享开关会让「这一次到底用哪个身份」出现两个可能的答案，而一旦
 * 控制面请求顺手带上明文 Key，就是一次不可撤回的泄露。
 *
 * 与 `consoleHttp` 是两个实例、两段拦截器，不共用一个「按条件加头」的入口。
 * Key 来自内存（`lib/activeKey.ts`），不是构建期环境变量：`.env` 的 `API_KEY` 兜底
 * 已按本票删除，没有「没选也能跑」的后门。
 */
const instance = axios.create({
  // 留空即相对路径，由 Vite 的 /api 代理同源转发；填绝对地址会绕开代理。
  baseURL: import.meta.env.VITE_API_BASE_URL || undefined,
  timeout: 30000,
})

instance.interceptors.request.use((config) => {
  const key = getActiveKey()
  if (key === null) {
    // 不给「空 Bearer」：那会拿到 20010，看起来像 Key 失效，而实际只是还没选。
    throw new ApiError(
      -1,
      'data',
      '',
      '还没有选 Key：去 /keys 选一把，再回到检索页',
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
