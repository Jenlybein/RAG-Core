"""数据库会话与**请求级事务边界**。

engine 在模块导入时创建，但不立刻连库（SQLAlchemy 惰性连接）；
get_db 作为 FastAPI 依赖，每个请求一个 Session，用完必关；
get_transaction 是**写路径**的那一半：一个请求一笔事务，成功提交、异常回滚。

两个依赖刻意分开而不是塞进 ``get_db``：读路径不需要提交（也不该顺手提交），而写接口
在签名上声明 ``Depends(get_transaction)`` 这件事本身就是「这条路径会写库」的可见标记。
提交点因此只有一处（``app.db.transaction``），服务层不再各自 ``commit()``。
"""

from collections.abc import Generator

from fastapi import Depends
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.db.transaction import Transaction, transaction

engine = create_engine(settings.DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：yield 一个 Session，请求结束后关闭。"""

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_transaction(db: Session = Depends(get_db)) -> Generator[Transaction, None, None]:
    """FastAPI 依赖：把这次请求圈进**一笔**事务（写接口专用）。

    出块时提交（并执行 ``after_commit`` 登记的动作），端点抛异常时整笔回滚后再把异常
    原样抛回去——所以错误码与响应形状完全由异常处理器决定，与事务无关。

    FastAPI 会缓存同一请求内的依赖结果，于是同一请求里多处声明它仍然只有一笔。
    """

    with transaction(db) as opened:
        yield opened
