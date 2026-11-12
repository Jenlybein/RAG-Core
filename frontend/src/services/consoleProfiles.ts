/**
 * 档位预设页的请求层（`/console/profile-presets`）。
 *
 * 全部走控制面客户端 `consoleHttp`：身份是会话 cookie，写请求自动带
 * `x-requested-with`。服务层只负责「怎么请求」；什么时候请求、结果怎么缓存由页面里的
 * `useQuery` / `useMutation` 决定。
 *
 * **只有管理员能读写这两条**：档位是平台级配置，改一行所有租户的检索质量都跟着变。
 * 租户面板要展示「这个档位能干什么」时，读的是 `GET /console/me` 里那份
 * `profile_presets`（见 `types/console.ts`），不走这里——那条接口对租户是 20011。
 */

import { consoleHttp } from '../lib/consoleHttp'
import type {
  ProfilePresetData,
  ProfilePresetListData,
  ProfilePresetPatchData,
  ProfilePresetPatchRequest,
} from '../types/profilePreset'

/** 列表的 query key：任何一档改完，页面按它失效重取。 */
export const PROFILE_PRESETS_QUERY_KEY = ['console', 'profile-presets'] as const

export function fetchProfilePresets(): Promise<ProfilePresetListData> {
  return consoleHttp.get<ProfilePresetListData>('/console/profile-presets')
}

/**
 * 改一个档位。只发真正动过的字段：整行发回去会让后端把没变的列也重写一遍并写进审计，
 * 事后看账本会以为管理员逐项改过。
 */
export function patchProfilePreset(
  profile: string,
  body: ProfilePresetPatchRequest,
): Promise<ProfilePresetPatchData> {
  return consoleHttp.patch<ProfilePresetPatchData>(`/console/profile-presets/${profile}`, body)
}

/** 前端也要能判「这一档是不是命名档」：`custom` 的行语义与其它三行不同。 */
export const NAMED_PROFILES = ['speed', 'balanced', 'quality'] as const

export function isNamedProfile(profile: string): boolean {
  return (NAMED_PROFILES as readonly string[]).includes(profile)
}

/**
 * 档位参数 → 一句人话。**展示与判定只此一处**：后台页、Key 编辑器的悬浮提示、
 * 前台卡片都用它，否则三处各写一遍，改文案时必然漏一处。
 *
 * 措辞刻意把「意图」与「生效」分开；生效值还要看部署有没有装那两项能力。
 */
export function describePreset(preset: ProfilePresetData): string {
  const parts: string[] = []
  if (preset.mode === 'hybrid') parts.push('向量 + 关键词混合召回')
  else if (preset.mode === 'vector') parts.push('只走向量召回')
  else if (preset.mode === 'bm25') parts.push('只走关键词召回')
  else parts.push(preset.mode)
  parts.push(`返回 ${preset.top_k} 条`)
  parts.push(preset.rerank ? '结果重排' : '不重排')
  parts.push(preset.rewrite ? '查询改写' : '不改写')
  return parts.join(' · ')
}
