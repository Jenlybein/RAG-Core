import { consoleHttp } from '../lib/consoleHttp'
import { WRITE_HEADER_NAME, WRITE_HEADER_VALUE } from '../lib/httpPolicy'
import type {
  ModelConfigCreateRequest,
  ModelConfigData,
  ModelConfigDeleteData,
  ModelConfigListData,
  ModelConfigPatchData,
  ModelConfigPatchRequest,
  ModelConfigSecretData,
} from '../types/modelConfig'

/**
 * 模型配置页的请求层（`/console/model-configs`）。
 *
 * 全部走控制面客户端 `consoleHttp`：身份是会话 cookie，写请求自动补 `x-requested-with`。
 * 服务层只负责「怎么请求」；什么时候请求、结果怎么缓存由页面的 `useQuery` /
 * `useMutation` 决定。
 *
 * **列表与表单里的 `api_key` 永远是 masked**（后端在响应里就已经打码）；明文只从
 * `GET /{id}/secret` 出一次，且每次调用都会在后端留一条 `model.reveal` 审计。
 */

/** 配置列表的 query key：增删改与改角色指向之后，页面按它失效。 */
export const MODEL_CONFIGS_QUERY_KEY = ['console', 'model-configs'] as const

/** 全部配置行（每行带服务端算好的 `roles`：现在被哪些角色指向）。 */
export function fetchModelConfigs(): Promise<ModelConfigListData> {
  return consoleHttp.get<ModelConfigListData>('/console/model-configs')
}

/** 新增一条接入。`base_url` / `api_key` 允许空串（默认地址 / 还没填 Key）。 */
export function createModelConfig(body: ModelConfigCreateRequest): Promise<ModelConfigData> {
  return consoleHttp.post<ModelConfigData>('/console/model-configs', body)
}

/**
 * 改一条配置。只发用户真正动过的字段：整行发回去会让后端把没变的列也重写一遍并写进
 * 审计，事后看账本会以为管理员逐项改过。
 */
export function updateModelConfig(
  configId: string,
  patch: ModelConfigPatchRequest,
): Promise<ModelConfigPatchData> {
  return consoleHttp.patch<ModelConfigPatchData>(
    `/console/model-configs/${encodeURIComponent(configId)}`,
    patch,
  )
}

/**
 * 删一条配置。
 *
 * 仍被任一角色指向时后端返回 `20015`（原文会说明「先把 fast/strong 的指向改掉」），
 * 这里不吞掉它：页面要把原文展示出来并指路，而不是给一句笼统的「参数非法」。
 */
export function deleteModelConfig(configId: string): Promise<ModelConfigDeleteData> {
  return consoleHttp.delete<ModelConfigDeleteData>(
    `/console/model-configs/${encodeURIComponent(configId)}`,
  )
}

/**
 * 显示一次明文 Key。**每一次调用后端都会写一条审计**，所以它被后端归为「写操作」，
 * 要求防跨站头——而 `consoleHttp` 只对 POST/PUT/PATCH/DELETE 自动补头，GET 不会。
 *
 * 因此这里显式带头：不加的话，这条 GET 会被后端的 `require_console_admin_write` 判成
 * `20011`，症状是「显示原文」永远失败。这个特例只在这一处，不把 GET 也变成需要写头的
 * 方法（那会让每个读请求都变成非简单请求，见 `lib/httpPolicy.ts` 的注释）。
 */
export function revealModelConfigSecret(configId: string): Promise<ModelConfigSecretData> {
  return consoleHttp.get<ModelConfigSecretData>(
    `/console/model-configs/${encodeURIComponent(configId)}/secret`,
    { headers: { [WRITE_HEADER_NAME]: WRITE_HEADER_VALUE } },
  )
}
