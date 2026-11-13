"""S0 · 只读压测入口：4 / 16 / 64 并发下的 P50 / P95 / 吞吐 / 错误率（#123）。

回答两个问题，产出一份可复算的读数：

1. 这条链路在 4 / 16 / 64 并发下的 **P50 / P95 / 吞吐 / 错误率**是多少；
2. 其中多少时间花在**主循环上的同步数据库调用**上。

**它只读，不改任何生产代码路径**：整条链路在进程内跑真的 FastAPI 应用（真路由、真服务、
真仓储、真折叠与融合），只把「驱动」换掉——数据库会话、嵌入、向量库、关键词引擎都是
离线替身，全部走仓库里既有的注入缝（``get_db`` / ``get_current_key`` / Provider 工厂 /
``app.db.session_factory`` / ``app.core.runtime_settings_cache``）。不连真 Redis、不连真
数据库、不写任何库，也不需要网络。

**延迟是模拟的，占比是近似**——这两件事必须写在读数旁边，否则这份报告会被当成生产容量：

* 每次同步 DB 调用、每次嵌入、每次向量召回、每次关键词召回都是**固定睡眠**，用来代表
  「这条路径上确实存在的那一段耗时」。绝对数只在同一组假设下可比；可比较的是**形状**：
  并发升高时谁劣化、劣化多少、吞吐封顶在哪。
* 「主循环同步 DB 占比」= 主循环线程上同步会话调用的耗时之和 ÷ 同一批请求的墙钟之和。
  分子来自插桩（会话替身的每个方法），分母来自实测墙钟；执行池线程上的调用不进分子。
  口径写在 ``scripts/_loadtest_core.py`` 的模块文档与报告第四节里，代码与报告读同一份。

怎么读结果：`--report-out` 写出 docs/ 下那份 markdown（人读），`--json-out` 写出机读的
读数（后面每一张票对比时只比它）。三档并发、同一组参数，改完再跑一次就是 S7 的对比。

输入问句来自 ``eval/questions.json``（137 道评测题，只取 ``query`` 字段循环使用）——
选它是因为它是仓库里**唯一**一份「真实用户问法」的固定集合，不需要另造语料；替身返回的
切片与这一批问句无关（离线替身不检索真数据），问句在这里只起「每次请求带一点真实长度
的文本」的作用。
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parent.parent
# 以 ``python scripts/loadtest_baseline.py`` 方式运行时 sys.path[0] 是 scripts/，
# 而 ``_loadtest_core`` 是按 ``scripts._loadtest_core`` 导入的（与 pytest 里一致）。
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts._loadtest_core import (  # noqa: E402 - 上面的 sys.path 引导必须先跑
    CONCURRENCY_LEVELS,
    STEP10_REFERENCE_P95_MS,
    STEP10_REFERENCE_SOURCE,
    DbLedger,
    LevelResult,
    render_report,
    summarize_latency,
)

TENANT_ID = "tenant_loadtest"
KB_ID = "kb-loadtest"
RETRIEVE_PATH = "/api/v1/rag/retrieve"
QUESTIONS_PATH = REPO_ROOT / "eval" / "questions.json"
DEFAULT_JSON_OUT = REPO_ROOT / "eval" / "loadtest-baseline" / "results.json"
DEFAULT_REPORT_OUT = REPO_ROOT / "docs" / "step11-loadtest-baseline.md"

#: 压测默认的「假设」：一组生产里说得通的量级，不是实测值（报告里会原样列出来）。
DEFAULT_DB_CALL_MS = 2.0
DEFAULT_EMBED_MS = 18.0
DEFAULT_VECTOR_MS = 12.0
DEFAULT_KEYWORD_MS = 15.0


# ─────────────────────────────────────────────────────────── 离线：进程级注入点


def pin_offline_env() -> None:
    """把「这台机器今天恰好开着什么服务」从读数里彻底拿掉。

    与 ``tests/conftest.py`` 同一条纪律：**强制覆盖**而不是 ``setdefault``。开发机的
    ``.env`` 里通常写着真 Redis、真 ES、真观测栈；不钉死的话，这份读数会取决于本机
    配置，甚至真的往那些服务里写东西。必须在 import ``app.*`` 之前调用（``settings``
    在 import 时就把环境变量读成对象了）。
    """

    os.environ["ENABLE_MCP"] = "false"
    os.environ["KEYWORD_SEARCH_PROVIDER"] = "none"
    os.environ["RERANK_BASE_URL"] = ""
    os.environ["TRACE_SINK"] = "none"
    os.environ["AUTH_ENABLED"] = "false"
    os.environ["RATE_LIMIT_ENABLED"] = "false"
    # 压测读数不该被「每个请求两条 info 日志」拖慢：httpx 与执行池的排队日志在
    # WARNING 以下，正好是这条链路上每请求都会刷的两条。异常仍然看得见。
    os.environ["LOG_LEVEL"] = "WARNING"


# ───────────────────────────────────────────────────── 替身：数据与「数据库」


@dataclass(frozen=True)
class Corpus:
    """离线替身返回的那份「数据」：一个库、四节、八片。

    形状刻意贴近真实切块（同一节里有多片、跨文档、带 ``meta``），这样折叠那一段的
    真实代码会被走到——它本来就要为一次检索额外查两次库，是主循环上的大头之一。
    """

    kb_row: Any
    count: int
    vector_rows: list[dict]
    keyword_rows: list[dict]
    meta_rows: list[Any]
    unit_rows: list[tuple]


def _build_corpus() -> Corpus:
    sections = [
        ("doc-1", "退款政策 > 申请流程"),
        ("doc-1", "退款政策 > 时限"),
        ("doc-2", "价目表 > 套餐"),
        ("doc-2", "价目表 > 加购"),
    ]
    membership = {
        0: ["c1", "c2"],
        1: ["c3"],
        2: ["c4", "c5"],
        3: ["c6", "c7", "c8"],
    }
    content = {f"c{index}": f"第 {index} 片的正文：这里放一段与问句无关的说明文字。" for index in range(1, 9)}

    def _row(chunk_id: str, section: int, *, score: float, kb_id: str | None = None) -> dict:
        document_id, heading_path = sections[section]
        row = {
            "document_id": document_id,
            "chunk_id": chunk_id,
            "title": heading_path.split(" > ")[-1],
            "content": content[chunk_id],
            "score": score,
            "heading_path": heading_path,
        }
        if kb_id is not None:
            row["kb_id"] = kb_id
        return row

    vector_rows = [
        _row("c1", 0, score=0.91),
        _row("c2", 0, score=0.83),
        _row("c3", 1, score=0.72),
        _row("c4", 2, score=0.65),
        _row("c5", 2, score=0.55),
    ]
    keyword_rows = [
        _row("c3", 1, score=12.4),
        _row("c4", 2, score=11.1),
        _row("c6", 3, score=10.2),
        _row("c7", 3, score=9.4),
        _row("c8", 3, score=8.1),
    ]
    meta_rows: list[Any] = []
    unit_rows: list[tuple] = []
    for section, chunk_ids in membership.items():
        document_id, heading_path = sections[section]
        for index, chunk_id in enumerate(chunk_ids):
            meta_rows.append(
                SimpleNamespace(
                    kb_id=KB_ID,
                    id=chunk_id,
                    meta={
                        "chunk_type": "text",
                        "section_id": f"sec-{section}",
                        "parent_section_id": f"sec-{section}",
                        "heading_path": heading_path,
                        "table_part": 0,
                        "page": 1,
                        "doc_version": 1,
                        "split_strategy": "structure",
                    },
                )
            )
            unit_rows.append(
                (document_id, heading_path, index, content[chunk_id], chunk_id, "text", 0)
            )
    return Corpus(
        kb_row=SimpleNamespace(
            id=KB_ID,
            tenant_id=TENANT_ID,
            name="压测知识库",
            description="S0 基线用的离线替身库",
            settings={},
        ),
        count=len(unit_rows),
        vector_rows=vector_rows,
        keyword_rows=keyword_rows,
        meta_rows=meta_rows,
        unit_rows=unit_rows,
    )


#: 定时器交给 ``asyncio.sleep`` 的那一段（秒）。剩下的用 ``await asyncio.sleep(0)`` 收尾。
#: Windows 的定时器粒度约 15.6ms：**0.5ms 的模拟往返会被睡成 15.6ms**（实测 ×31），
#: 那会把「异步栈的 P95」整体污染掉。基线（第 10 步）用的是 ``time.sleep(0.5ms)``，
#: 精确到微秒级；这里用同一把尺子，只是不阻塞事件循环。
_TIMER_SLEEP_SLACK = 0.0015


async def _precise_async_sleep(seconds: float) -> None:
    """等待 ``seconds`` 秒：不阻塞事件循环，也不受 Windows 定时器粒度影响。

    大间隔交给 ``asyncio.sleep``（它让出循环）；最后两毫秒用 ``await asyncio.sleep(0)``
    轮询——每次迭代都让出控制权，所以别的请求照跑，只是这一小段会占用一点 CPU。
    模拟延迟本来就是仪器开销，不该让仪器决定读数的形状。
    """

    if seconds <= 0:
        return
    deadline = time.perf_counter() + seconds
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return
        if remaining > _TIMER_SLEEP_SLACK:
            await asyncio.sleep(remaining - _TIMER_SLEEP_SLACK)
        else:
            await asyncio.sleep(0)


class ScriptedResult:
    """SQLAlchemy ``Result`` 的最小替身：只回答检索路径用到的那几种取值方式。"""

    def __init__(
        self,
        *,
        rows: Sequence[Any] = (),
        count: int = 0,
        row: Any = None,
    ) -> None:
        self._rows = list(rows)
        self._count = count
        self._row = row

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Any:
        return self._rows[0] if self._rows else self._row

    def scalar_one(self) -> Any:
        return self._count

    def scalar_one_or_none(self) -> Any:
        return self._row

    def scalars(self) -> ScriptedResult:
        return self

    def __iter__(self) -> Any:
        return iter(self._rows)


#: 向量替身发的那条 SQL。真实实现（``PgVectorStore.similarity_search_scope``）在异步栈上
#: ``await db.execute(...)``，这里同样是**一条被 await 的语句**（``TextClause``，与真实实现
#: 的形状一致）——所以它记进「被 await 的 DB 等待」那一列，而不是「阻塞主循环」那一列。
VECTOR_SQL = text(
    "SELECT chunk_id FROM chunks WHERE tenant_id = :tenant_id "
    "ORDER BY embedding <=> :query_vector LIMIT :top_k"
)

class ScriptedSession:
    """**异步**会话替身：只回答检索路径真正用到的那几种语句形状（第 11 步票 #127）。

    两种失败方式刻意不同：

    * 语句形状不认识 → **当场 ``AssertionError``**。静默返回空结果会让「后续票往检索路径上
      新增了一次查询」这件事在读数里完全看不见；报错则会先红一次，逼人先想清楚那条查询
      该不该在那里。
    * 语句认识但形状不对（比如 count 语句走了 ``all()``）→ 也报错。

    每个方法都先 ``await`` 一次精确睡眠（模拟一次往返），再记账。**睡眠是异步的**：这正是
    「异步数据库访问不阻塞事件循环」的形状，记账时因此记成 ``blocking=False``。真出现同步
    调用（``time.sleep``）时账本会记 ``blocking=True``，读数立刻不为 0。
    """

    def __init__(
        self,
        *,
        ledger: DbLedger,
        corpus: Corpus,
        latency_ms: float,
    ) -> None:
        self._ledger = ledger
        self._corpus = corpus
        self._latency_ms = latency_ms
        self.added: list[Any] = []
        self.closed = False
        #: 事务入口把「当前这一笔」挂在 ``session.info`` 上（``app/db/transaction.py``）。
        #: 归属索引的旁路短事务走的就是 ``async with async_transaction(session)``，替身
        #: 没有这个字典的话，每次检索都会多一条「写不进去」的 warning，而读数照旧。
        self.info: dict[str, Any] = {}

    # ── 记账 ──────────────────────────────────────────────────
    def _record(self, method: str, started: float) -> None:
        # 这一层全是异步会话上的调用：``blocking=False``（让出循环，不占主循环）。
        self._ledger.record(method, (time.perf_counter() - started) * 1000, blocking=False)

    async def _sleep(self) -> None:
        await _precise_async_sleep(self._latency_ms / 1000)

    # ── 读 ────────────────────────────────────────────────────
    async def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> ScriptedResult:
        # 语句分派**在计时之外**：那是替身自己的记账开销（一次 compile 约 0.1~0.5ms），
        # 算进「数据库花了多久」会让读数凭空多出十几个百分点。
        shape = _shape_of(statement)
        started = time.perf_counter()
        try:
            await self._sleep()
            return self._answer(shape, statement)
        finally:
            self._record("execute", started)

    def _answer(self, shape: str, statement: Any) -> ScriptedResult:
        if shape == "count":
            return ScriptedResult(count=self._corpus.count)
        if shape == "kb_row":
            return ScriptedResult(row=self._corpus.kb_row)
        if shape == "vector":
            return ScriptedResult(rows=self._corpus.vector_rows)
        if shape == "unit_rows":
            return ScriptedResult(rows=self._corpus.unit_rows)
        if shape == "chunk_meta":
            return ScriptedResult(rows=self._corpus.meta_rows)
        raise AssertionError(
            "压测替身不认识这条语句形状——检索路径上多了一次数据库访问，"
            "先确认它该不该在主循环上，再补这条形状：\n" + _describe(statement)[:400]
        )

    def query(self, *_args: Any, **_kwargs: Any) -> Any:
        """``db.query(...)``：检索路径不用它（仓储层走 ``execute``）。

        真走到这里说明有一条**新**的读路径进了主循环。静默返回空结果会让它在读数里
        完全看不见，所以这里直接报错——代价是那一次请求 500（错误率会说话）。
        """

        raise AssertionError(
            "压测替身没有实现 db.query(...)：检索路径上出现了新的读路径，先确认它该不该"
            "在主循环上，再补这条形状"
        )

    # ── 写（旁路：归属索引） ───────────────────────────────────
    def add(self, obj: Any) -> None:
        started = time.perf_counter()
        try:
            self.added.append(obj)
        finally:
            self._record("add", started)

    async def flush(self) -> None:
        started = time.perf_counter()
        try:
            await self._sleep()
        finally:
            self._record("flush", started)

    async def commit(self) -> None:
        started = time.perf_counter()
        try:
            await self._sleep()
        finally:
            self._record("commit", started)

    async def rollback(self) -> None:
        started = time.perf_counter()
        try:
            pass
        finally:
            self._record("rollback", started)

    async def close(self) -> None:
        started = time.perf_counter()
        try:
            self.closed = True
        finally:
            self._record("close", started)


def _shape_of(statement: Any) -> str:
    """按语句的**结构**分派（不编译：一次 compile 0.1~0.5ms，会污染读数）。

    认识的形状就是检索路径上真实发出的那几种：

    * ``count``：``SELECT count(*) FROM chunks ...``（空结果原因）；
    * ``kb_row``：``SELECT ... FROM knowledge_bases ...``（可见性校验，每库一次）；
    * ``chunk_meta``：``SELECT chunks.kb_id, chunks.id, chunks.metadata ...``（折叠回查）；
    * ``unit_rows``：``SELECT ... chunks.content ...``（折叠取整节）；
    * ``vector``：向量分支那条 pgvector 语句（``<=>``，在**执行池线程**上）。
    """

    text = getattr(statement, "text", None)
    if isinstance(statement, str):
        return "vector" if "<=>" in statement else "unknown"
    if isinstance(text, str):  # TextClause：向量替身自己发的 SQL
        return "vector" if "<=>" in text else "unknown"
    try:
        froms = statement.get_final_froms()
    except Exception:  # noqa: BLE001 - 不是 Select（UPDATE/DELETE 等）就不认识
        return "unknown"
    table = getattr(froms[0], "name", "") if froms else ""
    if table == "knowledge_bases":
        return "kb_row"
    if table != "chunks":
        return "unknown"
    names = {getattr(column, "name", "") or "" for column in statement.selected_columns}
    if "count" in names:
        return "count"
    if "content" in names:
        return "unit_rows"
    if "metadata" in names:
        return "chunk_meta"
    return "unknown"


def _describe(statement: Any) -> str:
    """报错时给出语句原文（只在出错路径上编译一次，不进读数）。"""

    try:
        return str(statement.compile(compile_kwargs={"literal_binds": True})).lower()
    except Exception:  # noqa: BLE001 - TextClause 等直接 str() 就是原文
        return str(statement).lower()


# ───────────────────────────────────────────────────────── 替身：三类 Provider


class LatencyEmbedding:
    """嵌入替身：异步 HTTP（在主循环上 await，不阻塞它），只睡一段固定时间。"""

    def __init__(self, latency_ms: float) -> None:
        self._latency_ms = latency_ms
        self.calls = 0

    async def embed_query(self, query: str) -> list[float]:
        self.calls += 1
        await asyncio.sleep(self._latency_ms / 1000)
        return [0.01] * 8


class LatencyKeyword:
    """关键词替身：异步 HTTP（同上），返回预置行。"""

    available = True

    def __init__(self, rows: Sequence[dict], latency_ms: float) -> None:
        self._rows = list(rows)
        self._latency_ms = latency_ms
        self.calls = 0

    async def keyword_search(
        self,
        query: str,
        *,
        tenant_id: str,
        index_versions: dict[str, str],
        top_k: int,
    ) -> list[dict]:
        self.calls += 1
        await asyncio.sleep(self._latency_ms / 1000)
        return [dict(row) for row in self._rows]


class LatencyVectorStore:
    """向量库替身：``await`` 一次范围召回——第 11 步票 #127 之后的真实形状。

    它真的用**请求那个异步会话**发一条语句（``VECTOR_SQL``），于是「向量召回也在异步栈上」
    这件事在账本里是实测的（``blocking=False``），而不是靠注释声明的。
    """

    def __init__(self, latency_ms: float) -> None:
        self._latency_ms = latency_ms
        self.calls = 0

    async def similarity_search_scope(
        self,
        db: Any,
        query_vector: Sequence[float],
        *,
        tenant_id: str,
        index_versions: dict[str, str],
        top_k: int,
    ) -> list[dict]:
        self.calls += 1
        # 异步调用：与真实实现一致（``await db.execute(...)``，不卡住循环）。
        result = await db.execute(VECTOR_SQL)
        rows = []
        for row in result.all():
            row = dict(row)
            row["kb_id"] = next(iter(index_versions))
            rows.append(row)
        return rows


# ──────────────────────────────────────────────────────── 装配：真应用 + 替身


class Harness:
    """一次压测的装配：真应用 + 离线替身 + 账本；``close`` 把注入点还原。

    只碰**既有注入缝**（与离线套件用的是同一组），所以生产代码一行不用改：

    * ``app.dependency_overrides[get_async_db]`` / ``[get_current_key]``：请求会话与身份；
    * ``app.services.rag_service`` 上的三个 Provider 工厂：嵌入 / 向量库 / 关键词；
    * ``app.db.session_factory.set_async_session_factory``：拿不到 ``Depends`` 的归属索引写入
      （第 11 步票 #127 起它在异步栈上）；
    * ``app.core.runtime_settings_cache.set_settings_cache``：配置刷新不读真 Redis。

    ``app.services.retrieval.pipeline.SessionLocal`` 那条注入**已经没有了**：向量分支不再
    另开会话，也不再有执行池。
    """

    def __init__(self, *, ledger: DbLedger, corpus: Corpus, profile: dict[str, float]) -> None:
        self.ledger = ledger
        self.corpus = corpus
        self.profile = profile
        self._restore: list[tuple[Any, str, Any]] = []
        self._async_session_factory_restore: Any = None
        self._settings_cache_restore: Any = None

    # ── 装配 ──────────────────────────────────────────────────
    def __enter__(self) -> Harness:
        import app.main as main_module
        from app.api.deps import get_current_key
        from app.core import runtime_settings_cache
        from app.db import async_session as async_session_module
        from app.db import session_factory as factory_module
        from app.services import rag_service
        from tests.retrieval_identity import RetrievalIdentity

        self.app = main_module.app
        self.embedding = LatencyEmbedding(self.profile["embed_ms"])
        self.vector = LatencyVectorStore(self.profile["vector_ms"])
        self.keyword = LatencyKeyword(self.corpus.keyword_rows, self.profile["keyword_ms"])

        def _new_session() -> ScriptedSession:
            return ScriptedSession(
                ledger=self.ledger,
                corpus=self.corpus,
                latency_ms=self.profile["db_call_ms"],
            )

        self._new_session = _new_session

        # 身份：custom 档、参数走部署缺省（hybrid / top_k=5 / 不重排 / 不改写）。
        identity = RetrievalIdentity(TENANT_ID)
        identity.use()

        # 两处都**间接**调用 ``self._new_session``：敏感性跑换的是这个属性，注入缝本身不动。
        self.app.dependency_overrides[async_session_module.get_async_db] = (
            lambda: self._new_session()
        )
        self.app.dependency_overrides[get_current_key] = identity.context

        for module, name, replacement in (
            (rag_service, "get_embedding_provider", lambda: self.embedding),
            (rag_service, "get_vector_store", lambda: self.vector),
            (rag_service, "get_keyword_search_provider", lambda: self.keyword),
        ):
            self._restore.append((module, name, getattr(module, name)))
            setattr(module, name, replacement)

        self._async_session_factory_restore = factory_module.async_session_factory()
        factory_module.set_async_session_factory(lambda: self._new_session())
        self._settings_cache_restore = runtime_settings_cache.get_settings_cache()
        runtime_settings_cache.set_settings_cache(_MemorySettingsCache())
        return self

    def __exit__(self, *_exc: Any) -> None:
        from app.core import runtime_settings_cache
        from app.db import session_factory as factory_module

        self.app.dependency_overrides.clear()
        for module, name, original in reversed(self._restore):
            setattr(module, name, original)
        factory_module.set_async_session_factory(self._async_session_factory_restore)
        runtime_settings_cache.set_settings_cache(self._settings_cache_restore)


class _MemorySettingsCache:
    """运行时配置覆盖层的**空**内存缓存（契约见 ``app.core.runtime_settings_cache``）。

    返回 ``{}`` 而不是 ``None``：``{}`` 是「缓存命中，覆盖层一行都没有」，于是中间件那一次
    刷新只做合并、不读库；``None`` 是「未命中」，每个 30 秒窗口都会真的读一次库——那一次
    读会混进压测读数里，而且它的耗时与本机装了什么无关。

    空覆盖 = 读路径永远回落到部署缺省，不会因为本机 Redis 里留着某次手工实验的
    ``RERANK_ENABLED=true`` 而让读数一次一个样。
    """

    def __init__(self) -> None:
        self._overrides: dict[str, Any] = {}

    def get(self) -> dict[str, Any] | None:
        return dict(self._overrides)

    def set(self, overrides: dict[str, Any]) -> None:
        self._overrides = dict(overrides)

    def delete(self) -> None:
        self._overrides = {}


# ────────────────────────────────────────────────────────────── 压测：一档并发


async def _drive(
    client: Any,
    questions: Sequence[str],
    *,
    concurrency: int,
    total: int,
) -> tuple[list[float], int, int, int]:
    """``concurrency`` 个虚拟用户跑满 ``total`` 个请求；返回 (耗时样本, 成功, 失败, 降级)。

    闭环比开环更接近真实调用方：一个用户等到响应才发下一个，所以「并发」= 同时在飞的
    请求数上限。开环（按固定速率注入）会测出「排了多长的队」，那是另一件事。
    """

    cursor = itertools.count()
    samples: list[float] = []
    ok = 0
    failed = 0
    degraded = 0

    async def _user() -> None:
        nonlocal ok, failed, degraded
        while True:
            index = next(cursor)
            if index >= total:
                return
            question = questions[index % len(questions)]
            started = time.perf_counter()
            try:
                response = await client.post(
                    RETRIEVE_PATH,
                    json={"kb_ids": [KB_ID], "query": question},
                )
            except Exception:  # noqa: BLE001 - 连接层失败也是一次失败请求
                samples.append((time.perf_counter() - started) * 1000)
                failed += 1
                continue
            samples.append((time.perf_counter() - started) * 1000)
            body = _json_of(response)
            code = body.get("code")
            if response.status_code != 200 or code != 0:
                failed += 1
                continue
            ok += 1
            retrieval = (body.get("data") or {}).get("metadata", {}).get("retrieval") or {}
            if retrieval.get("degraded"):
                degraded += 1

    await asyncio.gather(*(_user() for _ in range(max(1, concurrency))))
    return samples, ok, failed, degraded


def _json_of(response: Any) -> dict:
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - 非 JSON 响应就是一次失败请求
        return {}
    return payload if isinstance(payload, dict) else {}


async def _run_level(
    *,
    app: Any,
    questions: Sequence[str],
    concurrency: int,
    requests: int,
    warmup: int,
    ledger: DbLedger,
    db_call_ms: float,
) -> LevelResult:
    """一档并发：先预热（不进读数），再清账本、跑满、把读数算成 ``LevelResult``。"""

    import httpx

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://loadtest") as client:
        if warmup > 0:
            await _drive(
                client,
                questions,
                concurrency=min(max(1, concurrency), warmup),
                total=warmup,
            )
        ledger.reset()
        started = time.perf_counter()
        samples, ok, failed, degraded = await _drive(
            client,
            questions,
            concurrency=concurrency,
            total=requests,
        )
        wall_ms = (time.perf_counter() - started) * 1000

    total_ms = sum(samples)
    share = ledger.share(total_request_ms=total_ms)
    per_request_ms = (share.blocking_ms / requests) if requests else 0.0
    throughput = (requests / (wall_ms / 1000)) if wall_ms > 0 else 0.0
    return LevelResult(
        concurrency=concurrency,
        requests=requests,
        ok=ok,
        failed=failed,
        degraded=degraded,
        throughput_rps=throughput,
        wall_ms=wall_ms,
        latency=summarize_latency(samples),
        db=share,
        db_call_ms=db_call_ms,
        # 两个都是**实测**数：每请求在主循环上串行做多少毫秒的同步 DB 调用；以及整档墙钟里
        # 事件循环有多少比例正卡在这些调用里（后者才是「吞吐为什么封顶」的那个数）。
        cross_check={
            "serial_ms_per_request": per_request_ms,
            "loop_busy_share": (share.blocking_ms / wall_ms) if wall_ms > 0 else 0.0,
        },
    )


# ──────────────────────────────────────────────────────────────── 命令行入口


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="S0 只读压测：4 / 16 / 64 并发下的 P50 / P95 / 吞吐 / 错误率（离线）",
    )
    parser.add_argument(
        "--levels",
        default=",".join(str(level) for level in CONCURRENCY_LEVELS),
        help="并发档位，逗号分隔（缺省 4,16,64）",
    )
    parser.add_argument("--requests", type=int, default=160, help="每档请求数（缺省 160）")
    parser.add_argument("--warmup", type=int, default=8, help="每档预热请求数（缺省 8）")
    parser.add_argument(
        "--db-call-ms", type=float, default=DEFAULT_DB_CALL_MS, help="单次同步 DB 调用（模拟，ms）"
    )
    parser.add_argument(
        "--embed-ms", type=float, default=DEFAULT_EMBED_MS, help="嵌入调用（模拟，ms）"
    )
    parser.add_argument(
        "--vector-ms", type=float, default=DEFAULT_VECTOR_MS, help="向量召回（模拟，ms）"
    )
    parser.add_argument(
        "--keyword-ms", type=float, default=DEFAULT_KEYWORD_MS, help="关键词召回（模拟，ms）"
    )
    parser.add_argument(
        "--sweep-db",
        default="",
        help="敏感性：在最高一档上再跑这几个「单次同步 DB」值（逗号分隔，缺省不跑）",
    )
    parser.add_argument(
        "--questions",
        default=str(QUESTIONS_PATH),
        help="输入问句文件（缺省 eval/questions.json）",
    )
    parser.add_argument("--json-out", default=str(DEFAULT_JSON_OUT), help="机读读数落盘路径")
    parser.add_argument(
        "--report-out",
        default=str(DEFAULT_REPORT_OUT),
        help="markdown 报告落盘路径（缺省 docs/step11-loadtest-baseline.md）",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="不把报告打到 stdout（落盘仍然发生）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="允许覆盖缺省路径上**已经存在**的读数文件（缺省拒绝，见 _refuse_to_overwrite_baseline）",
    )
    return parser.parse_args(argv)


def _levels_of(raw: str) -> list[int]:
    levels = [int(item) for item in raw.replace(" ", "").split(",") if item]
    if not levels:
        raise SystemExit("--levels 不能为空")
    if any(level < 1 for level in levels):
        raise SystemExit("--levels 必须都是正整数")
    return levels


def load_questions(path: Path) -> list[str]:
    """评测题集里的问句原文（只取 ``query``）；文件缺失就是配置错误，不静默造语料。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    questions = [str(item["query"]) for item in payload["questions"] if item.get("query")]
    if not questions:
        raise SystemExit(f"{path} 里没有可用问句")
    return questions


def _refuse_to_overwrite_baseline(*, json_out: Path, report_out: Path, force: bool) -> None:
    """缺省路径上的读数**已经存在**时拒绝覆盖，除非显式 ``--force``。

    这条护栏是赔出来的（第 11 步 #134 实测赔过一次）：两个缺省输出指向的正是**已提交**
    的基线文件，于是「忘了带 ``--json-out`` / ``--report-out``」的一次复跑就会把别人的
    读数悄悄换成这一次的数字。``git status`` 会显示它，但那时你已经以为读数没变。

    只挡**缺省路径**：显式传了路径的人显然知道自己在往哪写。
    """

    for path, default, flag in (
        (json_out, DEFAULT_JSON_OUT, "--json-out"),
        (report_out, DEFAULT_REPORT_OUT, "--report-out"),
    ):
        if force or path.resolve() != default.resolve() or not path.exists():
            continue
        raise SystemExit(
            f"{path} 已经存在，且它是缺省读数路径。\n"
            f"要么显式给 {flag} 指到别处（推荐，例如 docs/step11-loadtest-after-<票>.md），\n"
            f"要么确认要覆盖它并加 --force。"
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    levels = _levels_of(args.levels)
    if args.requests < 1:
        raise SystemExit("--requests 至少 1")
    sweep_values = [
        float(item) for item in args.sweep_db.replace(" ", "").split(",") if item
    ]

    # 覆盖护栏放在开跑之前：跑到一半才拒绝，等于白跑一轮还让人以为成功了。
    _refuse_to_overwrite_baseline(
        json_out=Path(args.json_out),
        report_out=Path(args.report_out),
        force=bool(getattr(args, "force", False)),
    )

    pin_offline_env()

    questions = load_questions(Path(args.questions))
    corpus = _build_corpus()
    ledger = DbLedger()
    profile = {
        "db_call_ms": args.db_call_ms,
        "embed_ms": args.embed_ms,
        "vector_ms": args.vector_ms,
        "keyword_ms": args.keyword_ms,
    }

    with Harness(ledger=ledger, corpus=corpus, profile=profile) as harness:
        results = asyncio.run(
            _run_all(
                harness=harness,
                questions=questions,
                levels=levels,
                requests=args.requests,
                warmup=args.warmup,
                ledger=ledger,
                profile=profile,
                sweep_values=sweep_values,
            )
        )

    params = {
        "levels": levels,
        "requests": args.requests,
        "warmup": args.warmup,
        "db_call_ms": args.db_call_ms,
        "embed_ms": args.embed_ms,
        "vector_ms": args.vector_ms,
        "keyword_ms": args.keyword_ms,
        "db_stack": "async",
    }
    reference = {"p95_ms": STEP10_REFERENCE_P95_MS, "source": STEP10_REFERENCE_SOURCE}
    report = render_report(
        levels=results["levels"],
        params=params,
        reference=reference,
        db_sweep=results["sweep"],
    )

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "params": params,
        "reference": reference,
        "levels": [level.as_dict() for level in results["levels"]],
        "sweep": [level.as_dict() for level in results["sweep"]],
        "environment": _environment_snapshot(),
    }
    json_out = Path(args.json_out)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    report_out = Path(args.report_out)
    report_out.parent.mkdir(parents=True, exist_ok=True)
    report_out.write_text(report, encoding="utf-8")
    if not args.quiet:
        # Windows 控制台缺省是 GBK，报告里的 ⌈⌉ 与中文标点会直接把 print 打成
        # UnicodeEncodeError。落盘始终是 utf-8；这里只把 stdout 也调成 utf-8。
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(report)
    return 0


async def _run_all(
    *,
    harness: Harness,
    questions: Sequence[str],
    levels: Sequence[int],
    requests: int,
    warmup: int,
    ledger: DbLedger,
    profile: dict[str, float],
    sweep_values: Sequence[float],
) -> dict[str, list[LevelResult]]:
    """三档并发依次跑（同一个进程、同一个执行池：线程保持热）；再按需跑敏感性。"""

    measured: list[LevelResult] = []
    for concurrency in levels:
        measured.append(
            await _run_level(
                app=harness.app,
                questions=questions,
                concurrency=concurrency,
                requests=requests,
                warmup=warmup,
                ledger=ledger,
                db_call_ms=profile["db_call_ms"],
            )
        )

    sweep: list[LevelResult] = []
    if sweep_values:
        top = max(levels)
        for db_call_ms in sweep_values:
            # 敏感性只换「单次同步 DB」这一个假设：其余三个保持不变，读数才可比。
            harness.profile = {**profile, "db_call_ms": db_call_ms}
            _repatch_sessions(harness, db_call_ms)
            sweep.append(
                await _run_level(
                    app=harness.app,
                    questions=questions,
                    concurrency=top,
                    requests=requests,
                    warmup=warmup,
                    ledger=ledger,
                    db_call_ms=db_call_ms,
                )
            )
        harness.profile = dict(profile)
    return {"levels": measured, "sweep": sweep}


def _repatch_sessions(harness: Harness, db_call_ms: float) -> None:
    """敏感性跑之前把会话替身的延迟换掉（注入缝不变，只换替身参数）。"""

    def _new_session() -> ScriptedSession:
        return ScriptedSession(
            ledger=harness.ledger,
            corpus=harness.corpus,
            latency_ms=db_call_ms,
        )

    harness._new_session = _new_session


def _environment_snapshot() -> dict[str, Any]:
    """读数旁边要有的「在哪台机器、哪次提交上跑的」。"""

    import platform

    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "cpu_count": os.cpu_count(),
        "git_commit": _git_commit(),
        "offline": True,
        "note": "进程内 ASGI + 离线替身；无真库、无真 Redis、无网络",
    }


def _git_commit() -> str:
    try:
        import subprocess

        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 - 拿不到提交不影响读数
        return "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
