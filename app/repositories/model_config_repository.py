"""模型配置的数据访问：唯一能碰 ``model_configs`` 表的一层（票 #94）。

只 flush，不 commit——事务由 Service 收口（与既有仓储同一套规矩）。这张表**天生很小**
（管理员手动增删的几条接入），所以列表一次取回、不设上限。

这里只回答「有哪些行」与「按 id 找一行」，不判任何规则：provider 是否属于注册表、
角色是否指向它、删不删得掉，全是业务判定，写在服务层。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.model_config import ModelConfig


def list_all(db: Session) -> list[ModelConfig]:
    """全部配置行，按创建时间排序（面板上「先建的在上」比随机顺序好读）。"""

    return db.query(ModelConfig).order_by(ModelConfig.created_at).all()


def get_by_id(db: Session, config_id: str) -> ModelConfig | None:
    return db.query(ModelConfig).filter(ModelConfig.id == config_id).first()


def create(db: Session, row: ModelConfig) -> ModelConfig:
    db.add(row)
    db.flush()
    return row
