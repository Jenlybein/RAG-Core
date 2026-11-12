"""模型配置的数据访问：唯一能碰 ``model_configs`` 表的一层（票 #94）。

只 flush，不 commit——事务由 Service 收口（与既有仓储同一套规矩）。这张表**天生很小**
（管理员手动增删的几条接入），所以列表一次取回、不设上限。

这里只回答「有哪些行」与「按 id 找一行」，不判任何规则：provider 是否属于注册表、
角色是否指向它、删不删得掉，全是业务判定，写在服务层。

**两种会话并存是迁移期的形状**（第 11 步票 #130）：控制面（面板的六条接口）在异步栈上
（:func:`list_all_async` / :func:`get_by_id_async` / :func:`create`），而 Provider 装配
（``model_config_service.resolve_role_config``）与旁证跑批仍在同步会话上
（:func:`list_all` / :func:`get_by_id`）——它们拿不到 ``Depends``，也不在请求的异步路径上。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.model_config import ModelConfig


def list_all(db: Session) -> list[ModelConfig]:
    """同步面：Provider 装配与旁证跑批用（面板走 :func:`list_all_async`）。"""

    return db.query(ModelConfig).order_by(ModelConfig.created_at).all()


async def list_all_async(db: AsyncSession) -> list[ModelConfig]:
    """:func:`list_all` 的异步孪生（控制面列表，票 #130）。

    排序表达式逐字照抄同步版：面板上「先建的在上」不因为换了会话而变。
    """

    statement = select(ModelConfig).order_by(ModelConfig.created_at)
    return list((await db.execute(statement)).scalars().all())


def get_by_id(db: Session, config_id: str) -> ModelConfig | None:
    """同步面：角色指针解析用（``resolve_role_config``）。"""

    return db.query(ModelConfig).filter(ModelConfig.id == config_id).first()


async def get_by_id_async(db: AsyncSession, config_id: str) -> ModelConfig | None:
    """:func:`get_by_id` 的异步孪生（控制面的改 / 删 / 看明文，票 #130）。"""

    statement = select(ModelConfig).where(ModelConfig.id == config_id)
    return (await db.execute(statement)).scalars().first()


async def create(db: AsyncSession, row: ModelConfig) -> ModelConfig:
    """新增一条配置（只 flush，不提交——它必须与那条 ``model.create`` 审计同一笔事务）。"""

    db.add(row)
    await db.flush()
    return row
