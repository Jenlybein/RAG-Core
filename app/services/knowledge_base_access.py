"""知识库可见性守卫：数据面入口共用的「不存在 OR 跨租户」判定。

检索、文档列表与上传都必须先确认 ``kb_id`` 对调用方租户可见。三处各写一遍
``if kb is None or kb.tenant_id != tenant_id`` 既容易漂移，也容易有一处忘记抛
业务码。统一走 :func:`ensure_visible`：缺失与跨租户一律
``KNOWLEDGE_BASE_NOT_FOUND``（20008），**绝不**返回「无权限」，避免泄露资源
存在性。

**租户比较在仓储层里，不在这里。** 这里曾经是「按 id 取出来、再比租户」的形状——
它没错，但它是**可选**的：下一个调用点可以只做前半句。现在取行只有
``knowledge_base_repository.get_by_id_for_tenant``（租户是强制参数），比租户这件事
因此不可能被忘掉。

这个守卫管的是「这个库归不归你」；「这把 Key 能不能查这个库」是**另一道关卡**，在
:mod:`app.services.data_scope`（六道关卡的第二道），两个入口都按 鉴权 → 白名单 → ……
的顺序调用它。

**两个面**（第 11 步票 #127 / #132）：检索链路与上传登记走 :func:`ensure_visible_async`，
控制面与运维脚本走同步版。判定只有一句（见两个函数的实现），两个面不会给出两种答案。

**异步面收 ``DBSession`` 而不是 ``AsyncSession``**（第 11 步票 #132）：异步面有两个
调用方，一个已经纯异步、一个还没有——``rag_service.retrieve``（票 #127 搬完，手里是
``AsyncSession``）与 ``document_service.upload``（票 #132 搬完）。后者同时被**控制面
上传** ``console_kb_service.upload_document`` 调用，而那条路由（``POST
/console/knowledge-bases/{kb_id}/documents``）拿的还是同步 ``Session``，要到票 #130
（控制面写异步）才换。同一个函数要同时喂两种会话，签名只能取并集 ``DBSession``；会话
差异收在 ``app/db/dual_session.py``，票 #130 落地后随那个模块一起收窄成 ``AsyncSession``。
"""

from sqlalchemy.orm import Session

from app.core.exceptions import AppError, ErrorCode
from app.db.dual_session import DBSession
from app.models.knowledge_base import KnowledgeBase
from app.repositories import knowledge_base_repository


def ensure_visible(db: Session, *, tenant_id: str, kb_id: str) -> KnowledgeBase:
    """返回该租户可见的知识库，否则抛 20008；不泄露它是否存在。"""

    kb = knowledge_base_repository.get_by_id_for_tenant(db, kb_id, tenant_id=tenant_id)
    if kb is None:
        raise AppError(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)
    return kb


async def ensure_visible_async(db: DBSession, *, tenant_id: str, kb_id: str) -> KnowledgeBase:
    """返回该租户可见的知识库，否则抛 20008；:func:`ensure_visible` 的异步面。

    两条守卫**共用同一句判定**（取行在仓储层，缺失与跨租户都回 ``None``），所以
    「不可见 → 20008」这件事不会因为走了哪条栈而分叉。拒绝先于召回：检索链路的
    调用方（``rag_service.retrieve``）在任何一次嵌入之前调用它。

    会话是 ``DBSession``（同步或异步）而不是 ``AsyncSession``：调用方除了已经纯异步
    的检索链路，还有 ``document_service.upload``——它同时服务数据面上传（``AsyncSession``）
    与控制面上传（同步 ``Session``，票 #130 才换）。理由与收窄时机见模块文档。
    """

    kb = await knowledge_base_repository.get_by_id_for_tenant_async(
        db, kb_id, tenant_id=tenant_id
    )
    if kb is None:
        raise AppError(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)
    return kb
