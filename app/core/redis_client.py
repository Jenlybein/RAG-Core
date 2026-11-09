"""Redis 客户端的**唯一**构造点。

两条硬要求：

1. **不在 import 时连库**。``Redis.from_url`` 本身是惰性的（第一次发命令才建连接），
   但「模块一被 import 就构造客户端」仍然会让一个不关心 Redis 的测试（或一条只读的
   运维脚本）莫名其妙地持有一个指向 ``localhost:6379`` 的连接对象。这里把构造推迟到
   第一次真正需要它的时候。
2. **可以被测试换掉，而不是靠 monkeypatch 全局**。``set_redis_client`` / ``reset_redis_client``
   是这个模块的公开接口：注入一个假客户端、用完复位，比在每个测试里
   ``monkeypatch.setattr("app.core.redis_client._client", ...)`` 更不容易漏、也不依赖
   私有名字。

其余模块一律只调 :func:`get_redis_client`，不自己 ``Redis.from_url(...)``——否则「连的是
哪个库」这件事会散落在若干个文件里，而它们迟早会指向不同的库。
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings

#: 进程内单例。这里存的是**客户端对象**，不是连接：redis-py 自己管连接池。
_client: Any | None = None


def get_redis_client() -> Any:
    """取（必要时构造）进程内唯一的 Redis 客户端；此时**不**发任何命令。"""

    global _client
    if _client is None:
        # 延迟到真正要用时才 import 并构造：见模块文档第 1 条。
        from redis import Redis

        _client = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _client


def set_redis_client(client: Any | None) -> None:
    """注入一个客户端（测试用假实现，生产可换成连到另一个库的实例）。"""

    global _client
    _client = client


def reset_redis_client() -> None:
    """复位成「还没构造」的状态；下一次 ``get_redis_client`` 会按当前配置重建。"""

    set_redis_client(None)
