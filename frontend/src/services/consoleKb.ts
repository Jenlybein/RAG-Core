import { consoleHttp } from '../lib/consoleHttp'
import type { DocumentListItem } from '../types/document'
import type {
  ConsoleTreeData,
  ConsoleTreeDocument,
  KnowledgeBaseNode,
} from '../types/knowledgeBase'

/**
 * 控制面「一次拿全」的知识库树：本票 (#89) 换掉阶段 1 那套「1 次列库 + N 次列文档」。
 *
 * 为什么单独放一个 `consoleKb.ts` 而不是塞进 `knowledgeBaseService.ts`：
 * 后者其余函数打的都是数据面（Bearer Key），本文件是唯一一处走控制面（会话 cookie）。
 * 把「哪扇门」提到文件粒度后，`grep consoleHttp` 就能看全控制面读接口，
 * 不会有人误以为列表页还在用 Key 取库。`fetchTree` 留在 `knowledgeBaseService`：
 * 它的签名被页面与轮询依赖，换个文件等于改调用点。
 *
 * 为什么这就能做到页面一行不改：端点直出的形状（`kb_id/name/split_strategy/
 * allowed_formats/chunk_size/documents[]`）与页面认的 `KnowledgeBaseNode[]` 一致，
 * 页面只消费这个类型，不知道数据是 1 次拿的还是 N 次拼的。
 */
export const CONSOLE_KB_TREE_PATH = '/console/knowledge-bases/tree'

/** 读树：租户来自会话，后端不做白名单收窄（控制面要看整个租户）。 */
export function fetchConsoleTree(): Promise<ConsoleTreeData> {
  return consoleHttp.get<ConsoleTreeData>(CONSOLE_KB_TREE_PATH)
}

/**
 * 线上树 → 既有 `KnowledgeBaseNode[]`。
 *
 * 抽成纯函数而不是留在 `fetchTree` 里：映射是本次切换唯一真正变了的行为，
 * 纯函数让空库、多个库、字段缺省三种形状都能在 vitest 里直接钉住，不用起后端。
 *
 * 两个数组在这里兜底（`?? []`）：页面直接读 `documents.length`、遍历
 * `allowed_formats`，缺一个就是白屏；后端契约里它们恒在，但这里是渲染前
 * 最后一个还能「给安全默认」的位置。
 */
export function toKnowledgeBaseNodes(data: ConsoleTreeData): KnowledgeBaseNode[] {
  return data.knowledge_bases.map((node) => ({
    kb_id: node.kb_id,
    name: node.name,
    split_strategy: node.split_strategy,
    allowed_formats: node.allowed_formats ?? [],
    chunk_size: node.chunk_size,
    documents: (node.documents ?? []).map(toDocumentListItem),
  }))
}

/** 文档项逐字段搬过来并补齐 `error_message`：`StatusBadge` 拿 null 才不显示失败原因。 */
function toDocumentListItem(document: ConsoleTreeDocument): DocumentListItem {
  return {
    document_id: document.document_id,
    title: document.title,
    status: document.status,
    chunk_count: document.chunk_count,
    error_message: document.error_message ?? null,
    source_type: document.source_type,
    created_at: document.created_at,
  }
}
