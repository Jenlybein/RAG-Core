"""归属索引的数据访问：唯一能碰 ``retrieval_traces`` 表的一层。

**检索链路只写不读**。写侧只 ``flush`` 不 ``commit``（事务由 Service 收口，与其它仓储一致）。

读侧只有一处：控制台按 ``log_id`` 读回瀑布之前，先要证明「这一行属于当前租户」
（:func:`get_by_id_for_tenant`，#103）。回读**不进检索链路**——链路一旦开始读这张表，
旁路就变回主链路的依赖，而这张表的意义正是「链路不需要它也能跑完」。
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.retrieval_trace import RetrievalTrace


def create(db: Session, trace: RetrievalTrace) -> RetrievalTrace:
    db.add(trace)
    db.flush()
    return trace


def get_by_id_for_tenant(db: Session, log_id: str, *, tenant_id: str) -> RetrievalTrace | None:
    """按 ``id``（对外即 ``log_id``）**且** ``tenant_id`` 取一行；缺失与跨租户都返回 ``None``。

    租户是**强制参数、不是可选过滤器**：调用方拿到 ``None`` 一律按「不存在」（20016）处理，
    可区分的拒绝会泄露「这个 log_id 在别人那儿存在」。这条口径与
    ``document_repository.get_by_id_for_tenant`` 逐字同构，是 ADR-0006 的隔离精神在控制面
    这一侧的落地；无租户的按 id 读取本模块**不提供**（那张表的读者只有控制台）。
    """

    statement = select(RetrievalTrace).where(
        RetrievalTrace.id == log_id, RetrievalTrace.tenant_id == tenant_id
    )
    return db.execute(statement).scalar_one_or_none()
