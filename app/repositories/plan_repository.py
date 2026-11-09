"""套餐数据访问：唯一能碰 plans 表的一层。

只 flush，不 commit——事务由 Service 收口（与既有仓储同一套规矩）。
"""

from sqlalchemy.orm import Session

from app.models.plan import Plan


def create(db: Session, plan: Plan) -> Plan:
    db.add(plan)
    db.flush()
    return plan


def get_by_id(db: Session, plan_id: int) -> Plan | None:
    return db.get(Plan, plan_id)


def get_by_code(db: Session, code: str) -> Plan | None:
    """按档位码取。**只有三档是种子**，库里少一档时调用方必须当错误处理。"""

    return db.query(Plan).filter(Plan.code == code).first()


def list_all(db: Session) -> list[Plan]:
    return db.query(Plan).order_by(Plan.id).all()


def count(db: Session) -> int:
    """数行数而不是取回全部：播种只关心「表是不是空的」。"""

    return db.query(Plan).count()
