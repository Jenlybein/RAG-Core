"""同步引擎：**只剩控制面装配与运维脚本**的读取口（第 11 步票 #134）。

## 这里原来还有什么

第 10 步及以前，这个模块是请求路径的入口：``get_db``（每个请求一个 ``Session``）与
``get_transaction``（写路径一个请求一笔事务）。第 11 步把检索、观测、控制面读写、入库
索引逐片搬到 ``AsyncSession`` 之后，那两个依赖**一个调用点都没有了**——路由的依赖树里
只剩 ``get_async_db`` / ``get_async_transaction``（守卫用例见
``tests/test_console_async_reads.py`` / ``tests/test_console_async_writes.py``）。所以它们
连同文档一起删掉：留着就是留着一种「也可以这么写」的示范（ADR-0024）。

## 剩下的同步读者，只有三类

1. **Provider 装配期的角色指针**（``app.services.model_config_service.resolve_role_config``）：
   ``get_llm_provider(role)`` 是同步工厂，Provider 层按约定不认识数据库（ADR-0011 的层向），
   而「这个角色现在指向哪条模型接入」只能当场读库。要把它挪到异步栈，得让整个 Provider
   装配变成协程——那是另一次层向改造，不在第 11 步（见 ADR-0024「还剩一处」与翻案条件）。
2. **运行时配置的热刷新**（``app.services.runtime_settings``）：刷新中间件用
   ``run_in_threadpool`` 调它，所以这次读库发生在线程池里、**不占事件循环**。
3. **运维脚本与启动期装载**：``scripts/*.py`` 的一次性动作，以及 ``app/main.py`` 启动时的
   档位装载。它们用的种子/运维仓储只有同步实现（``tenant_repository.create``、
   ``plan_repository.create``、``existing_tenant_ids``……），给它们造异步孪生等于为脚本新增
   生产代码。

共同点：**都不在请求路径上**。别在这里加第四个读者——新代码一律走
``app.db.async_session``。

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
