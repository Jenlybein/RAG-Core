"""数据库会话。

engine 在模块导入时创建，但不立刻连库（SQLAlchemy 惰性连接）；
get_db 作为 FastAPI 依赖，每个请求一个 Session，用完必关。
"""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

engine = create_engine(settings.DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：yield 一个 Session，请求结束后关闭。"""

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
