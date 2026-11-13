"""同步引擎：**只剩控制面装配与运维脚本**的读取口（第 11 步票 #134）。

## 这里原来还有什么

第 10 步及以前，这个模块是请求路径的入口：``get_db``（每个请求一个 ``Session``）与
``get_transaction``（写路径一个请求一笔事务）。第 11 步把检索、观测、控制面读写、入库
索引逐片搬到 ``AsyncSession`` 之后，那两个依赖**一个调用点都没有了**——路由的依赖树里
只剩 ``get_async_db`` / ``get_async_transaction``（守卫用例见
``tests/test_console_async_reads.py`` / ``tests/test_console_async_writes.py``）。所以它们
连同文档一起删掉：留着就是留着一种「也可以这么写」的示范（ADR-0024）。

## 剩下的同步读者，共三处

1. **运行时配置的热刷新**（中间件线程池）：``app.services.runtime_settings`` 由刷新中间件
   用 ``run_in_threadpool`` 调，所以这次读库发生在线程池的那条线程上、**不占事件循环**。
2. **运维脚本**（独立进程）：``scripts/*.py`` 的一次性动作。它们用的种子/运维仓储只有同步
   实现（``tenant_repository.create``、``plan_repository.create``、``existing_tenant_ids``……），
   给它们造异步孪生等于为脚本新增生产代码。
3. **启动期档位装载**（lifespan 里、开始接请求之前）：``app/main.py`` 装载一次档位参数。

三处都在**线程池 / 独立进程 / 启动期**，确实不占事件循环。

**原来还有第四处，已经搬走**（票 #137）：Provider 装配的角色指针
（``model_config_service.resolve_role_config`` / ``role_endpoint``）随 ``get_llm_provider``
一起变成了协程，角色指针走 ``AsyncSession``——事件循环上不再有同步会话读。ADR-0024
决策四第一行因此从「留待 #137」改成「已搬」。

别在这里加第四个读者——新代码一律走 ``app.db.async_session``。

## 为什么引擎仍然在模块导入时创建

``create_engine`` 不连库（SQLAlchemy 惰性连接），所以「import 就建引擎」不会在离线环境里
失败；真正连库只发生在第一次 ``execute``。这一点与异步引擎（``app.db.async_session``）
逐字相同。
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings

engine = create_engine(settings.DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

__all__ = ["SessionLocal", "engine"]
