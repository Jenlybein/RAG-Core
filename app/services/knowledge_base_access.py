"""知识库可见性守卫：数据面入口共用的「不存在 OR 跨租户」判定。

检索、文档列表、上传与重建都必须先确认 ``kb_id`` 对调用方租户可见。几处各写一遍
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

**只有一份**（第 11 步票 #130）：同步版随它的最后一个调用点（同步的数据面路由）一起
删掉了。现在所有调用方都在事件循环上，手里都是 ``AsyncSession``。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ErrorCode
from app.models.knowledge_base import KnowledgeBase
from app.repositories import knowledge_base_repository


async def ensure_visible(db: AsyncSession, *, tenant_id: str, kb_id: str) -> KnowledgeBase:
    """返回该租户可见的知识库，否则抛 20008；不泄露它是否存在。"""

    kb = await knowledge_base_repository.get_by_id_for_tenant(
        db, kb_id, tenant_id=tenant_id
    )
    if kb is None:
        raise AppError(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)
    return kb
