"""归属索引的数据访问：唯一能碰 ``retrieval_traces`` 表的一层。

只写不改，也**只写不读**：这张表是旁路账本，检索链路从不回读它（回读属于控制台 / 评测，
那些是另外的入口）。只 ``flush`` 不 ``commit``——事务由 Service 收口，与其它仓储一致。
"""

from sqlalchemy.orm import Session

from app.models.retrieval_trace import RetrievalTrace


def create(db: Session, trace: RetrievalTrace) -> RetrievalTrace:
    db.add(trace)
    db.flush()
    return trace
