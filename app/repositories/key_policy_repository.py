"""密钥配置数据访问：唯一能碰 key_policies 表的一层。

只 flush，不 commit——事务由 Service 收口。
"""

from sqlalchemy.orm import Session

from app.models.key_policy import KeyPolicy


def create(db: Session, policy: KeyPolicy) -> KeyPolicy:
    db.add(policy)
    db.flush()
    return policy


def get_by_id(db: Session, policy_id: str) -> KeyPolicy | None:
    return db.get(KeyPolicy, policy_id)
