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

**为什么重排端点也要在这里清空**：第 10 步票 3 起，生产工厂会按 ``RERANK_BASE_URL``
装配专用端点，而开发机的 ``.env`` 里写着真实地址与真 key（评测要跑的就是它）。不钉死的话，
任何一条「打开重排」的用例都会真的发出一次**计费**请求：套件变慢、结果取决于那家服务
今天通不通，账单上还会出现一行没人认领的费用。需要端点的用例自己 monkeypatch 这几行
（见 ``tests/test_rerank_provider.py``），所以这不是「禁掉重排」，而是「把端点这件事变成
用例显式声明的输入」。

**为什么每条例行前后要清三类 Provider 缓存与客户端注册表**：第 10 步票 4 起，三个 Provider
工厂都按配置指纹做进程级缓存，连接也按事件循环注册。不清的话，某个用例用 monkeypatch
改过配置后装出来的 Provider（甚至一个已经绑在**它自己那个**已关闭 loop 上的客户端）会留给
下一个用例——症状是「单跑必绿、全量随机红」，而且红的是一条跟配置无关的用例。这与
``reset_keyword_search_provider_cache()`` 是同一条纪律，只是覆盖面从关键词扩到了模型类。
"""

import os
from typing import Any

import pytest

os.environ.setdefault("ENABLE_MCP", "false")
# 强制覆盖：见上。这一行让「无 ES」成为套件自己的事实，而不是机器的巧合。
os.environ["KEYWORD_SEARCH_PROVIDER"] = "none"
# 强制覆盖：**重排端点也必须是套件自己的事实**。#116 起生产工厂会按 ``RERANK_BASE_URL``
# 装配专用端点，而开发机 `.env` 里通常写着真实地址与真 key（评测要跑的就是它）。不钉死
# 的话，任何一条「打开重排」的用例都会真的发出一次**计费**请求，套件结果既取决于这台
# 机器的配置，也取决于那家服务今天通不通。需要端点的用例自己 monkeypatch 这几行。
os.environ["RERANK_BASE_URL"] = ""
# 强制覆盖：见上最后两段。这一行让「观测去向是丢弃」成为套件自己的事实。
os.environ["TRACE_SINK"] = "none"
# 强制覆盖：**鉴权开关也必须由套件自己说了算**，理由与上面三条一字不差。
#
# 不固定它，套件的结果就取决于本机 `.env` 里 `AUTH_ENABLED` 写的是什么：写成 `true`
# 之后，一批「带占位 Key 调数据面」的用例会突然全红（占位 Key 在 `api_keys` 表里不存在，
# 一律 20010），而代码一行没改——这正是本项目最怕的那种「红绿取决于这台机器今天有什么」。
#
# 基线定成 `false`（内置开发身份），因为绝大多数用例关心的不是鉴权，而是鉴权**之后**的
# 业务行为；需要真实 Key 那条路的用例会**显式**把开关拨过去（见
# `tests/test_tenant_wiring.py` 里两种身份各走一遍的用例），所以这里写死不会掩盖任何东西。
os.environ["AUTH_ENABLED"] = "false"


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

    **套餐缓存的默认实现也在这里换掉**（票 #129）：``resolve_plan`` 的 ``cache`` 缺省时走
    ``plan_service.default_plan_cache()``，而**登录**与「我的视图」正是这么调的。不换的话，
    一条与 Redis 无关的用例只要经过登录就会去连真 Redis——本机开着 Redis 就绿、没开就红；
    更糟的是异步客户端绑在**第一次使用它的那个事件循环**上，下一个用例换了个循环就报
    ``Event loop is closed``，症状与「登录坏了」一模一样。这同样不是套件自己的事实。
    """

    from app.core.http_clients import reset_http_clients
    from app.db.worker_loop import reset_worker_loop
    from app.core.runtime_settings_cache import reset_settings_cache, set_settings_cache
    from app.db import session_factory as factory_mod
    from app.observability import reset_trace_sink
    from app.providers.embedding import reset_embedding_provider_cache
    from app.providers.keyword_search import reset_keyword_search_provider_cache
    from app.providers.llm import reset_llm_provider_cache
    from app.providers.rerank import reset_rerank_provider_cache
    from app.services import plan_service
    from tests.fake_db import AsyncRecordingSession, MemoryPlanCache, RecordingSession

    factory_mod.set_session_factory(RecordingSession)
    # 异步那一路同理（票 #128 起反馈写入走它）：不注入的话，旁路短事务会真的去连 PostgreSQL。
    factory_mod.set_async_session_factory(AsyncRecordingSession)
    set_settings_cache(_MemorySettingsCache())
    plan_service.set_default_plan_cache(MemoryPlanCache())
    reset_trace_sink()
    reset_keyword_search_provider_cache()
    reset_llm_provider_cache()
    reset_rerank_provider_cache()
    reset_embedding_provider_cache()
    reset_http_clients()
    # Worker 常驻循环（#131 / ADR-0023）同样是**进程级**状态：它在自己的线程里持有一个
    # 事件循环与一个引擎的连接池。用例忘了清场，下一个用例就会看到一个属于**上一个**
    # 用例的循环——症状与「上一个用例留下的连接池」那条一字不差。没用过时是安静的 no-op。
    reset_worker_loop()
    try:
        yield
    finally:
        factory_mod.set_session_factory(None)
        factory_mod.set_async_session_factory(None)
        reset_settings_cache()
        plan_service.set_default_plan_cache(None)
        reset_trace_sink()
        reset_keyword_search_provider_cache()
        reset_llm_provider_cache()
        reset_rerank_provider_cache()
        reset_embedding_provider_cache()
        reset_http_clients()
        reset_worker_loop()
