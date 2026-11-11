import { dataHttp } from '../lib/dataHttp'

/**
 * 「这把 Key 能访问哪些库」——走**数据面**（`Authorization: Bearer <key>`）。
 *
 * 为什么用数据面而不是控制面的库树 + 前端自己筛白名单：
 *
 * - 控制面那棵树是**会话身份**的视图，它列全部库、不做范围收窄；现在的「哪些能勾」
 *   是前端拿 `/console/keys` 里的 `allowed_kb_ids` 自己算的。那是**客户端算的答案**，
 *   一旦两边口径漂了（配置改了、库被删、`unrestricted_scope` 之类），界面会显示一个
 *   服务端并不认可的集合，而错误要等检索撞 `20008` 才暴露。
 * - 数据面这条 `GET /api/v1/knowledge-bases` 是**服务端自己**按同一份 policy 过滤后
 *   的答案（`scoped_kb_ids`），与检索用的是同一个判定，只是反应不同：检索越界拒绝，
 *   列库只把越界的去掉。
 *
 * 所以「选 Key → 问服务端能查哪些库 → 让用户从里面勾」这条链上，服务端是唯一的真相。
 *
 * 只读：这是**发现类**调用，不写入任何东西。数据面客户端仍然只允许出现在被列举的
 * service 文件里（见 `lib/__tests__/dataPlaneBoundary.spec.ts`）。
 */

export interface MyKnowledgeBase {
  kb_id: string
  name: string
  tenant_id: string
  /** `fixed` / `paragraph` 之类的切分策略，原样来自后端。 */
  split_strategy: string
  allowed_formats: string[]
  chunk_size: number
}

/** `GET /api/v1/knowledge-bases` 的 `data`。 */
export interface MyKnowledgeBaseList {
  knowledge_bases: MyKnowledgeBase[]
}

export const MY_KB_QUERY_KEY = ['data-plane', 'knowledge-bases'] as const

/**
 * 问服务端：这把 Key 能查哪些库。
 *
 * 没有「传 key」参数：身份由 `lib/dataHttp` 的请求拦截器从内存里的当前 Key 注入。
 * 没选 Key 时它会抛一个可操作的错误（「还没有选 Key」），而不是发一个空 Bearer 去换
 * 一个看起来像「Key 失效」的 20010。
 */
export function fetchMyKnowledgeBases(): Promise<MyKnowledgeBaseList> {
  return dataHttp.get<MyKnowledgeBaseList>('/api/v1/knowledge-bases')
}
