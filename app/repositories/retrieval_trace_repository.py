"""归属索引的数据访问：唯一能碰 ``retrieval_traces`` 表的一层。

只写不改；**检索链路从不回读它**。它唯一被读的地方是反馈的归属校验（#105）：
「这条 ``log_id`` 存在吗、``trace_id`` 对得上吗、是不是当前租户的」。那是另一个入口，
不是检索链路；控制台与评测的回读走各自的查询，也不经过这里。

只 ``flush`` 不 ``commit``——事务由 Service 收口，与其它仓储一致。
"""

from sqlalchemy.orm import Session

from app.models.retrieval_trace import RetrievalTrace


def create(db: Session, trace: RetrievalTrace) -> RetrievalTrace:
    db.add(trace)
    db.flush()
    return trace


def get_by_log_id(db: Session, log_id: str) -> RetrievalTrace | None:
    """按 ``log_id``（即表的主键 ``id``）取那一行；没有就是 ``None``。

    读走调用方传进来的会话（反馈走的是请求会话）：校验是只读动作，不需要另开事务。
    三种失败在调用方那里合并成同一个答复，所以这里只回「那一行长什么样」，不判定对错。
    """

    return db.get(RetrievalTrace, log_id)
