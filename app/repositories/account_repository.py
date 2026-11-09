"""账号数据访问：唯一能碰 accounts 表的一层。

只 flush，不 commit——事务由 Service 收口（与既有仓储同一套规矩）。

``get_by_username`` 是登录的唯一入口：登录先按用户名定位账号，再验口令；查不到就走
「等时假校验」那条路（见 ``app.core.passwords.dummy_verify``）。
"""

from sqlalchemy.orm import Session

from app.models.account import Account


def create(db: Session, account: Account) -> Account:
    db.add(account)
    db.flush()
    return account


def get_by_id(db: Session, account_id: str) -> Account | None:
    return db.get(Account, account_id)


def get_by_username(db: Session, username: str) -> Account | None:
    return db.query(Account).filter(Account.username == username).first()


def list_all(db: Session) -> list[Account]:
    """管理员视角的账号清单：按 id 排序只为让两次读的结果稳定。"""

    return db.query(Account).order_by(Account.id).all()
