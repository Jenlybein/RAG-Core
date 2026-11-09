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
