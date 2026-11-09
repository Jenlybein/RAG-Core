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


def list_by_tenant(db: Session, tenant_id: str) -> list[KeyPolicy]:
    """这个租户的全部密钥配置。控制面「改套餐影响面」要数「谁会失去档位」。

    按 id 排序只为让两次读的结果稳定；顺序不参与任何判定。
    """

    return db.query(KeyPolicy).filter(KeyPolicy.tenant_id == tenant_id).order_by(KeyPolicy.id).all()
