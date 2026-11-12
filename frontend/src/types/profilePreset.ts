/**
 * 档位预设的请求 / 响应形状（`app/schemas/console_profiles.py`）。
 *
 * `profile` 是档位名（`speed` / `balanced` / `quality` / `custom`），不是主键 UUID——
 * 这张表的主键就是档位名本身（四行固定，不存在「加一档」的运行时操作）。
 */

export interface ProfilePresetData {
  profile: string
  /** `vector` / `hybrid`（词表来自后端 `SUPPORTED_RETRIEVAL_MODES`）。 */
  mode: string
  top_k: number
  /** 档位**想要**重排与否；生效值还要与部署开关取合取。 */
  rerank: boolean
  /** 同上。 */
  rewrite: boolean
}

export interface ProfilePresetListData {
  items: ProfilePresetData[]
}

/** 部分更新：没发的字段不动（`undefined` 与「不改」同义，前端不必补全整行）。 */
export interface ProfilePresetPatchRequest {
  mode?: string
  top_k?: number
  rerank?: boolean
  rewrite?: boolean
}

export interface ProfilePresetPatchData {
  before: Record<string, unknown>
  after: Record<string, unknown>
  changed: string[]
}
