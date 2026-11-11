import axios, { type AxiosRequestConfig } from 'axios'

import { getActiveKey } from './activeKey'
import { toApiError } from './axiosError'
import { ApiError, unwrapEnvelope } from './envelope'
import { asPayload } from './httpPayload'

/**
 * 数据面客户端：身份**只有**用户当前选中的 API Key，注入 `Authorization: Bearer`。
 *
 * **只给数据面的业务动作用。** 当前有三个调用点，都是「拿真实 Key 打真实请求」：
 * `POST /api/v1/rag/retrieve`（ragService，检索）、
 * `POST /api/v1/rag/feedback`（feedbackService，#106 起，给这次检索打分）、
 * `GET /api/v1/knowledge-bases`（scopeService，**只读发现**：这把 Key 能查哪些库）。
 *
 * 第三个是后来加的，理由要说清楚，否则边界就会一路松下去：**「这把 Key 能访问什么」
 * 只有用这把 Key 去问服务端才作数**。控制面那棵树是会话身份的视图、不做范围收窄，
 * 早先是在前端拿 `allowed_kb_ids` 自己算「哪些能勾」——那是客户端算的答案，一旦与
 * 服务端的判定漂了，界面会显示一个服务端并不认可的集合，而错误要等检索撞 20008 才暴露。
 * 这条调用**只读、不写入、不扩权**：它返回的正是检索本来就会接受的集合，答不出比它更多
 * 的东西。
 *
 * 除此之外**不该有第四个**：登录 / 我的 / 树 / 建库 / 上传 / 重建 / 删文档 / Key 与配置
 * 全部走控制面会话门（`lib/consoleHttp.ts`），那扇门不认 Bearer。为什么把这件事写成一句
 * 注释而不是一个共享开关：共享开关会让「这一次到底用哪个身份」出现两个可能的答案，而一旦
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
