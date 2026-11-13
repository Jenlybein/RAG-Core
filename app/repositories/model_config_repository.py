"""模型配置的数据访问：唯一能碰 ``model_configs`` 表的一层（票 #94）。

只 flush，不 commit——事务由 Service 收口（与既有仓储同一套规矩）。这张表**天生很小**
（管理员手动增删的几条接入），所以列表一次取回、不设上限。

这里只回答「有哪些行」与「按 id 找一行」，不判任何规则：provider 是否属于注册表、
角色是否指向它、删不删得掉，全是业务判定，写在服务层。

**只有异步面**（第 11 步票 #137）：控制面（面板的六条接口）与 Provider 装配
（``model_config_service.resolve_role_config``，每次装配都读一次角色指针）都在异步栈上。
旧的同步孪生（当时也叫 ``list_all`` / ``get_by_id``）随装配搬到异步栈一起删掉——那条路径
上已经没有同步调用点，留着就是留一份「也可以这么写」的示范。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.model_config import ModelConfig


async def list_all(db: AsyncSession) -> list[ModelConfig]:
    """全部配置行（控制面列表与旁证跑批；装配期只按 id 取那一行）。"""

    statement = select(ModelConfig).order_by(ModelConfig.created_at)
    return list((await db.execute(statement)).scalars().all())


async def get_by_id(db: AsyncSession, config_id: str) -> ModelConfig | None:
    """按 id 找一行（角色指针解析、控制面改 / 删 / 看明文共用这一条）。"""

    statement = select(ModelConfig).where(ModelConfig.id == config_id)
    return (await db.execute(statement)).scalars().first()


async def create(db: AsyncSession, row: ModelConfig) -> ModelConfig:
    """新增一条配置（只 flush，不提交——它必须与那条 ``model.create`` 审计同一笔事务）。"""

    db.add(row)
    await db.flush()
    return row
