"""离线测试的环境隔离：关掉 MCP 会话管理器、关键词引擎固定成空实现。

必须在任何测试文件 import ``app.main`` 之前生效，所以放在 conftest 里
（``app.core.config.settings`` 在 import 时就把环境变量读成对象了）。
生产 / 手动起服务时 ENABLE_MCP 默认 True，/mcp/ 正常挂载。

**为什么 KEYWORD_SEARCH_PROVIDER 是强制覆盖而不是 setdefault**：离线套件的前提是
「无 ES、无网络、无 .env」。可这条前提以前是靠「本机恰好没有 .env」成立的——开发者
在 shell 里导出过 .env 模板里的 ``KEYWORD_SEARCH_PROVIDER=elasticsearch``（或者干脆
docker compose 起过容器），套件就会真的去连 ES：结果取决于这台机器今天有什么，
而不是取决于代码。``setdefault`` 拦不住这种情况（变量已经存在，它什么都不做），
所以这里无条件写死 ``none``：**环境里有 ES 配置也一样全绿**。

**为什么还要换掉进程级会话工厂**：请求会话由各测试用 ``dependency_overrides`` 换掉，但
「拿不到 ``Depends`` 的路径」（配置刷新的中间件、档位装载、检索旁路的归属索引写入 #101）
走的是 ``app.db.session_factory``。不在这里换掉的话，每跑一次检索都会真的去连一次库：
本机没有 PostgreSQL 时是每次一条 warning、套件变慢；本机恰好起着一个 dev 库时更糟——
测试会往里真写行。两种都不该取决于「这台机器今天开着什么服务」。

**为什么连运行时配置缓存也要换掉**：配置刷新的中间件每 30 秒读一次覆盖层，**先读缓存**再
读库。缓存的生产实现连本机 Redis，而本机 Redis 里可能还留着某次手工实验写下的
``RERANK_ENABLED=true``：一旦进程跑够 30 秒，那次刷新就会把它合并进全局 ``settings``，
于是**后面**那些「默认不重排」的检索用例开始看到重排被打开（区间内随机红几条，且只在跑
得够久的套件里出现，单跑必绿——最难查的那类）。这同样不是套件自己的事实。缺省给一份空
内存缓存之后，覆盖层读到的永远是「一行都没有」，与「这台机器上恰好有什么」彻底解耦。

**为什么观测去向也固定成 ``none``**：``TRACE_SINK`` 的缺省值是 ``jsonl``（生产缺省：
默认开启观测但不依赖外部服务），于是**每跑一次检索都会往 ``logs/traces/`` 写一行**。
在套件里这既不是被测行为，也不是套件自己的事实：跑一次全量测试就多出上千行文件、
把开发机的磁盘与目录弄脏，而这份文件的内容还取决于「今天跑了哪些用例」。更隐蔽的是
反方向——开发者本机 ``.env`` 里写着 ``TRACE_SINK=langfuse``，那每跑一次检索就会往一个
可能正开着的观测栈推六段 span，套件于是变慢，甚至把测试数据混进真实 trace 里。
观测那几张票自己的用例会把去向**显式**指到临时目录或假客户端（见
``tests/test_observability_sink.py`` / ``tests/test_retrieval_observability.py``），
其余用例只该看见「丢弃」这个去向。强制覆盖而不是 ``setdefault``，理由与上面那条一字不差。

**为什么每条例行前还要 ``reset_trace_sink()``**：去向是进程级缓存（SDK 客户端不该每个
请求新建一个）。某个用例用 ``set_trace_sink`` 注入过替身之后，缓存里就留着它——下一个
用例即使什么都没做，也会把 span 交给那个替身。夹具在每个用例开始时清掉注入与缓存，
于是「这个用例看到的去向」只由它自己（或上面的环境变量）决定。
"""

import os
from typing import Any

import pytest

os.environ.setdefault("ENABLE_MCP", "false")
# 强制覆盖：见上。这一行让「无 ES」成为套件自己的事实，而不是机器的巧合。
os.environ["KEYWORD_SEARCH_PROVIDER"] = "none"
# 强制覆盖：见上最后两段。这一行让「观测去向是丢弃」成为套件自己的事实。
os.environ["TRACE_SINK"] = "none"


class _MemorySettingsCache:
    """运行时配置覆盖层的空内存缓存（契约见 ``app.core.runtime_settings_cache``）。"""

    def __init__(self) -> None:
        self._overrides: dict[str, Any] | None = None

    def get(self) -> dict[str, Any] | None:
        return self._overrides

    def set(self, overrides: dict[str, Any]) -> None:
        self._overrides = dict(overrides)


@pytest.fixture(autouse=True)
def _offline_process_state():
    """进程级注入点全部换成离线替身：会话工厂、运行时配置缓存与观测去向（见模块文档后三段）。

    用例自己注入时（``session_factory.set_session_factory(...)`` /
    ``runtime_settings.set_settings_cache(...)`` / ``observability.set_trace_sink(...)``）
    会覆盖本夹具；夹具在**每个**用例开始时重新注入 / 清场，所以也不会把上一个用例注入的
    东西漏给下一个。
    """

    from app.core.runtime_settings_cache import reset_settings_cache, set_settings_cache
    from app.db import session_factory as factory_mod
    from app.observability import reset_trace_sink
    from tests.fake_db import RecordingSession

    factory_mod.set_session_factory(RecordingSession)
    set_settings_cache(_MemorySettingsCache())
    reset_trace_sink()
    try:
        yield
    finally:
        factory_mod.set_session_factory(None)
        reset_settings_cache()
        reset_trace_sink()
