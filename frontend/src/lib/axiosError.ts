import axios from 'axios'

import { ApiError, type Face, unwrapEnvelope } from './envelope'

/**
 * 把 axios 的各种失败折成同一个 ApiError：
 * - 业务失败（HTTP 200 + code!==0）走 envelope 的分流文案；
 * - 连不上后端要区别于「后端说不行」——前者是环境问题，后者是业务结论；
 * - 非信封响应（HTML/422 detail）说明请求没到业务层，不能伪装成空成功。
 */
export function toApiError(error: unknown, face: Face): ApiError {
  if (error instanceof ApiError) {
    return error
  }

  if (axios.isAxiosError(error)) {
    const response = error.response
    if (response === undefined) {
      return new ApiError(
        -1,
        face,
        '',
        '连不上后端：确认 uvicorn 已启动在 127.0.0.1:8000，并且是从 5173 经 Vite 代理访问',
      )
    }

    try {
      // 信封里带业务码时，交给唯一的解包逻辑给出 human message。
      unwrapEnvelope(response.data, face)
    } catch (inner) {
      if (inner instanceof ApiError) {
        return inner
      }
    }

    const hint =
      response.status === 422
        ? '后端拒绝了请求体（HTTP 422）：检查字段名与格式'
        : `后端返回 HTTP ${response.status}，没有业务码可用`
    return new ApiError(-1, face, '', hint)
  }

  if (error instanceof Error) {
    return new ApiError(-1, face, '', `请求未能发送：${error.message}`)
  }
  return new ApiError(-1, face, '', '请求未能发送：未知异常')
}
