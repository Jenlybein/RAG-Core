import { consoleHttp } from '../lib/consoleHttp'
import type {
  SettingsListData,
  SettingsPatchData,
  SettingsPatchRequest,
} from '../types/runtimeSettings'

/**
 * 检索参数页的请求层（`/console/settings`，配置中台 #93）。
 *
 * 全部走控制面客户端 `consoleHttp`：身份是会话 cookie，写请求自动补 `x-requested-with`。
 *
 * 这里只负责「怎么请求」；「先试算、看影响面、再真写」的产品规则在
 * `views/admin/RetrievalPage.vue`（与 #95 的套餐页同一形状）。
 */

/** 配置清单的 query key：保存成功后页面按它失效，下一次读到库里的新值。 */
export const SETTINGS_QUERY_KEY = ['console', 'settings'] as const

/** 三档清单（后端已按 locked → restart → hot 分组，并带每行的来源与非法标记）。 */
export function fetchSettings(): Promise<SettingsListData> {
  return consoleHttp.get<SettingsListData>('/console/settings')
}

/**
 * 改运行时配置。`dryRun=true` 只试算、不落库、不删缓存，返回 `impact`；
 * `false` 才真正写库并按档位提示「热更 / 待重启」。
 *
 * `dry_run` 显式传：后端默认值也是 `true`（忘记传时最坏只是没保存），但把「这是试算」
 * 写在代码里，读页面的人不用再去核对一次默认值。
 */
export function updateSettings(
  values: SettingsPatchRequest['values'],
  dryRun: boolean,
): Promise<SettingsPatchData> {
  return consoleHttp.patch<SettingsPatchData>(
    '/console/settings',
    { values },
    { params: { dry_run: dryRun } },
  )
}
