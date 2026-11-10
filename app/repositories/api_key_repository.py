"""API Key 数据访问：唯一能碰 api_keys 表的一层。

只 flush，不 commit——事务由 Service 收口。
"""

from sqlalchemy.orm import Session

from app.models.api_key import ApiKey


def create(db: Session, api_key: ApiKey) -> ApiKey:
    db.add(api_key)
    db.flush()
    return api_key


def get_by_value(db: Session, key_value: str) -> ApiKey | None:
    """鉴权路径的唯一查询：按原文一击命中（本期明文，见 ADR-0004）。"""

    return db.query(ApiKey).filter(ApiKey.key_value == key_value).first()


def get_by_id(db: Session, key_id: str) -> ApiKey | None:
    """控制面按 key_id 找一行；不存在与「不属于本租户」由服务层合成同一个答案。"""

    return db.get(ApiKey, key_id)


def list_by_tenant(db: Session, tenant_id: str) -> list[ApiKey]:
    """本租户的全部 Key，按签发时间排序。

    排序只为让面板与两次读的结果稳定；它不参与任何判定。**故意不提供跨租户列表**：
    「看别人的 Key」不是本票给管理员的能力（管理员连这个接口都进不去，见服务层）。
    """

    return (
        db.query(ApiKey)
        .filter(ApiKey.tenant_id == tenant_id)
        .order_by(ApiKey.created_at, ApiKey.id)
        .all()
    )


def delete(db: Session, api_key: ApiKey) -> None:
    """只删 Key 这一行；配置行不在这里出现——删 Key 不删配置（结构上独立）。"""

    db.delete(api_key)
    db.flush()
