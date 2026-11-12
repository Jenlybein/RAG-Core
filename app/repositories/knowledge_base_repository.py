"""知识库数据访问：唯一能碰 knowledge_bases 表的一层。

只 flush，不 commit——事务由 Service 收口。

**这里没有「先按 id 取出来、再让调用方比租户」的函数。** 那种形状的危险在于它是可选的：
调用方只要忘了比一次，跨租户的库就被读出来了，而且读出来之后一切看起来都正常。所以取
「一行库」的入口只有 :func:`get_by_id_for_tenant`，租户是**无默认值的强制参数**——名字里
带租户，签名里必须有租户，忘记过滤在代码层就写不出来（ADR-0006）。唯一不带租户的是运维
脚本发现归属用的 :func:`get_owner_tenant_id`，它只回一个字符串，不回库行。

**异步面**（第 11 步票 #127）：:func:`get_by_id_for_tenant_async` 是同一个入口的异步版，
与同步版共用一条语句（:func:`_by_id_for_tenant_statement`）。检索链路走异步面，控制面与
上传仍走同步面——迁移按垂直切片推进，两面并存期间**语句只有一份**。
"""

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.knowledge_base import SPLIT_STRUCTURE, KnowledgeBase, settings_of


def create(db: Session, kb: KnowledgeBase) -> KnowledgeBase:
    db.add(kb)
    db.flush()
    return kb


def _by_id_for_tenant_statement(kb_id: str, *, tenant_id: str) -> Select[tuple[KnowledgeBase]]:
    """「按 id **且**租户取一行」的唯一语句：同步面与异步面共用同一条。

    语句只写一次的理由与「唯一入口」相同：迁移期两个面并存，各拼一遍 SQL 就是两份
    可能漂移的租户过滤。检索链路走 :func:`get_by_id_for_tenant_async`，控制面与上传
    仍走同步版；两边发出的 SQL 因此逐字相同。
    """

    return select(KnowledgeBase).where(
        KnowledgeBase.id == kb_id, KnowledgeBase.tenant_id == tenant_id
    )


def get_by_id_for_tenant(db: Session, kb_id: str, *, tenant_id: str) -> KnowledgeBase | None:
    """按 ``id`` **且** ``tenant_id`` 取库；缺失与跨租户一律 ``None``。

    调用方拿到 ``None`` 后统一按「知识库不存在」（``20008``）处理，不区分「没这个库」与
    「这个库不归你」——可区分的拒绝等于告诉攻击者资源存在（ADR-0006 / ADR-0018）。
    数据面的可见性守卫 ``app.services.knowledge_base_access.ensure_visible`` 与它同一条
    口径，两个入口都只经过那一个守卫。
    """

    return db.execute(_by_id_for_tenant_statement(kb_id, tenant_id=tenant_id)).scalar_one_or_none()


async def get_by_id_for_tenant_async(
    db: AsyncSession, kb_id: str, *, tenant_id: str
) -> KnowledgeBase | None:
    """:func:`get_by_id_for_tenant` 的异步面（检索链路，第 11 步票 #127）。

    语义、租户口径与返回形状与同步版逐字一致，只是 ``execute`` 要 ``await``。
    它是**同一条语句**的另一个面，不是另一份实现（见 :func:`_by_id_for_tenant_statement`）。
    """

    result = await db.execute(_by_id_for_tenant_statement(kb_id, tenant_id=tenant_id))
    return result.scalar_one_or_none()


def get_owner_tenant_id(db: Session, kb_id: str) -> str | None:
    """**仅供运维脚本**：按 ``kb_id`` 取它的归属租户，供 ``--tenant-id`` 缺省时发现租户。

    这是本模块唯一不带租户的查询，因此刻意只回答「归谁」，不回答「这个库长什么样」：它
    返回 ``str | None``，拿不到行，也就没法被数据面借去读别人的库。运维脚本
    （``scripts/reindex_knowledge_base.py``）直连数据库、不走 API Key，先在「不知道租户」
    的处境里，随后每一处查询都显式带上这个租户。

    数据面路径**不得调用**：它们手里已经有租户了，该用 :func:`get_by_id_for_tenant`。
    """

    kb = db.get(KnowledgeBase, kb_id)
    return None if kb is None else str(kb.tenant_id)


def get_split_config(
    db: Session, kb_id: str, *, tenant_id: str
) -> tuple[str, int]:
    """取该库的切块配置 (strategy, chunk_size)。

    库不存在或跨租户时回落到默认值，而不是抛错：索引任务是异步的，库在这期间
    被删掉时应当按默认配置继续尝试，让后续流程报出「库不存在」这个真正的原因；
    在取配置这一步先炸只会把原因盖掉。

    策略列非空，这里不再有「缺省就是 structure」的回退——那种回退会让
    「没有策略」这个本该不存在的情况被悄悄掩盖。
    """

    kb = db.get(KnowledgeBase, kb_id)
    if kb is None or kb.tenant_id != tenant_id:
        values = settings_of(None)
        return SPLIT_STRUCTURE, values["chunk_size"]
    values = settings_of(kb)
    return kb.split_strategy, values["chunk_size"]


def list_by_tenant(db: Session, tenant_id: str) -> list[KnowledgeBase]:
    """列这个租户的库；数据面还会再按 Key 的数据范围收窄一层（见 ``data_scope``）。"""

    return (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.tenant_id == tenant_id)
        .order_by(KnowledgeBase.created_at)
        .all()
    )


def count_by_tenant(db: Session, tenant_id: str) -> int:
    """这个租户有几个库（静态配额「库数」的唯一计数处，第 7 步票 #79）。

    只数、不取行：调用方（``app.services.static_quota``）要的是数字，把整批行读出来
    再 ``len()`` 在库多的时候是白白的 IO。``tenant_id`` 是强制参数，理由与本模块
    其余函数相同——一条不带租户的计数会让「按套餐判库数」变成「按整个平台判」。
    """

    stmt = (
        select(func.count())
        .select_from(KnowledgeBase)
        .where(KnowledgeBase.tenant_id == tenant_id)
    )
    return int(db.execute(stmt).scalar_one())
