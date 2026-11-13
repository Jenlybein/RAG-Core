"""#105 · 反馈以本地为准：归属校验、越界拒绝、观测旁路。

三个接缝，全是**外部可观察**的（#98 已定的 S1 一类，不新增接缝）：

* ASGI ``POST /api/v1/rag/feedback``：响应体（``code`` / ``msg`` / ``data``）；
* 会话注入点：请求会话 ``get_async_db`` 的替身（归属校验读的那一侧）与进程级
  ``app.db.session_factory.async_session_factory``（本地写入的那一侧）——两者**必须分开**，
  否则「写入走独立短事务、请求会话从不 commit」这件事无从证明；
* 观测 sink 注入点（``app.observability.set_trace_sink``）与 ``logs/app.log``：
  推分是旁路，失败只留一条 warning，绝不改响应。

数据库一律是 ``tests/fake_db.AsyncRecordingSession``（票 #128 起反馈读写全异步），
观测一律是假客户端 / 假 sink：整套不连 PostgreSQL、不连 Langfuse、不发网络。

``log_id`` 与 ``trace_id`` 的关系不是自由字段：``trace_id`` 由 ``log_id`` 确定性派生
（``app.services.trace_identity``），所以这里的合法样本就是「派生出来的那一个」。
"""

from __future__ import annotations

import importlib
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.auth import KeyContext
from app.core.exceptions import ErrorCode
from app.models.retrieval_feedback import RetrievalFeedback
from app.models.retrieval_trace import RetrievalTrace
from app.observability import OBSERVABILITY_SCORE_FAILED_MESSAGE, set_trace_sink
from app.services.trace_identity import trace_id_for
from tests.fake_db import AsyncRecordingSession

FEEDBACK_PATH = "/api/v1/rag/feedback"

TENANT_ID = "tenant_feedback"
OTHER_TENANT = "tenant_other"

#: 一次检索的 ``log_id``（归属索引那一行的 id）与它的确定性派生 ``trace_id``。
LOG_ID = "6f1d0b2a-4f57-4c3e-8a91-2d5b7c0e4a13"
TRACE_ID = trace_id_for(LOG_ID)

#: 调用方给的评论：**刻意不是问句原文**——评论是调用方自己写的，我们从不往里填原文。
COMMENT = "召回里混进了别的章节"


# ────────────────────────────────────────────────────────────────── 离线替身


def _trace_row(
    *,
    log_id: str = LOG_ID,
    trace_id: str = TRACE_ID,
    tenant_id: str = TENANT_ID,
) -> RetrievalTrace:
    """归属索引里的一行：合法样本由它校验「存在 + 归属当前租户 + 与 trace_id 对得上」。"""

    return RetrievalTrace(
        id=log_id,
        tenant_id=tenant_id,
        trace_id=trace_id,
        kb_ids=["kb-1"],
        chunk_ids=["chunk-1"],
        top_k=1,
        latency_ms=7,
        degraded_reason=None,
        empty_reason=None,
    )


class _SelectRows:
    """``await db.execute(select(...))`` 的最小替身：认得 ``.scalars().first()`` / ``.all()``。"""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = list(rows)

    def scalars(self) -> _SelectRows:
        return self

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return list(self._rows)


class _FeedbackSession(AsyncRecordingSession):
    """``AsyncRecordingSession`` + 「认得反馈表那条 ``select(...).where(log_id == ?)``」。

    ``upsert`` 要先判断「这个 ``log_id`` 是不是已经有一行了」（后写覆盖，不是新增一行）。
    真 ``AsyncSession`` 没有 ``db.query``，所以那条判断只能走 ``execute(select(...))``；
    父替身对模型查询一律回 ``tenant_ids``，答不了这个问题。这里只认**这一种**查询形状，
    别的语句仍落到父类——假会话越像真会话，越容易让人误以为测过的是数据库。
    """

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if _final_table_name(statement) == RetrievalFeedback.__tablename__:
            return _SelectRows(
                [row for row in self._rows.get(RetrievalFeedback, []) if _matches(row, statement)]
            )
        return await super().execute(statement, *args, **kwargs)


class _BrokenWriteSession(_FeedbackSession):
    """写到一半失败的会话：``flush`` 当场抛（约束冲突、连接断在半路都长这样）。

    它要证明的不是「异常被接住了」，而是「这一笔没有提交、且回滚过一次」——旁路短事务
    的收尾必须是**确定**的，否则半提交会以「库里有一行没人知道」的形态留下去。
    """

    async def flush(self) -> None:
        raise RuntimeError("psycopg: connection lost")


def _final_table_name(statement: Any) -> str | None:
    try:
        return statement.get_final_froms()[0].name
    except (AttributeError, IndexError):
        return None


def _matches(row: Any, statement: Any) -> bool:
    """只按等值 ``col = 字面量`` 比对——这条查询就一个条件，多一种形状说明实现变了。"""

    criterion = getattr(statement, "whereclause", None)
    pending = [criterion] if criterion is not None else []
    while pending:
        node = pending.pop()
        left = getattr(node, "left", None)
        right = getattr(node, "right", None)
        if left is not None and hasattr(left, "key") and hasattr(right, "value"):
            if getattr(row, left.key) != right.value:
                return False
            continue
        clauses = getattr(node, "clauses", None)
        if clauses:
            pending.extend(clauses)
    return True


class _ExplodingSink:
    """推分数必炸的观测去向：``record_score`` 是这条路径唯一会碰的方法。"""

    def record_score(self, _trace_id: str, _score: int, *, comment: str | None = None) -> None:
        raise RuntimeError("langfuse: 503")


class _FeedbackApi:
    """一个离线应用边界 + 两条会话（请求侧 / 写入侧）的记录。"""

    def __init__(
        self,
        client: TestClient,
        request_db: AsyncRecordingSession,
        write_session: AsyncRecordingSession,
    ) -> None:
        self.client = client
        self.request_db = request_db
        self.write_session = write_session

    def submit(self, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "log_id": LOG_ID,
            "trace_id": TRACE_ID,
            "score": 4,
            "comment": COMMENT,
        }
        payload.update(overrides)
        response = self.client.post(FEEDBACK_PATH, json=payload)
        assert response.status_code == 200, response.text
        return response.json()


@contextmanager
def _offline_api(
    monkeypatch: pytest.MonkeyPatch,
    *,
    traces: list[RetrievalTrace] | None = None,
    tenant_id: str = TENANT_ID,
    write_session: AsyncRecordingSession | None = None,
    session_factory: Any = None,
) -> Iterator[_FeedbackApi]:
    """离线 ASGI 边界；两条会话都是替身，观测去向是空实现（见 ``tests/conftest.py``）。

    ``traces`` 是请求会话里**能看到**的归属索引行（缺省：一条合法样本）。``session_factory``
    用来让「本地写入那一步失败」这件事可被观察——它与请求会话刻意是两条路。
    """

    main = importlib.import_module("app.main")
    async_session_mod = importlib.import_module("app.db.async_session")
    factory_mod = importlib.import_module("app.db.session_factory")

    from app.api.deps import get_current_key

    request_db = AsyncRecordingSession(rows={RetrievalTrace: list(traces or [])})
    write_session = write_session if write_session is not None else _FeedbackSession()
    factory_mod.set_async_session_factory(session_factory or (lambda: write_session))

    main.app.dependency_overrides[async_session_mod.get_async_db] = lambda: request_db
    main.app.dependency_overrides[get_current_key] = lambda: KeyContext(tenant_id=tenant_id)
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            yield _FeedbackApi(client, request_db, write_session)
    finally:
        main.app.dependency_overrides.clear()
        factory_mod.set_async_session_factory(None)


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FeedbackApi]:
    """缺省场景：归属索引里有一行属于当前租户的记录，``trace_id`` 与它一致。"""

    with _offline_api(monkeypatch, traces=[_trace_row()]) as harness:
        yield harness


@pytest.fixture(autouse=True)
def log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """每条测试一个干净的临时日志目录（与 ``test_trace_identity`` 同一套做法）。"""

    log_dir = tmp_path / "logs"
    logging_mod = importlib.import_module("app.core.logging")
    config_mod = importlib.import_module("app.core.config")
    monkeypatch.setattr(config_mod.settings, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(config_mod.settings, "LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(logging_mod, "_configured", False)
    logging_mod.setup_logging()
    yield log_dir
    logging.shutdown()
    logging_mod._configured = False
    logging_mod.setup_logging(log_dir=tmp_path / "logs-restored", log_level="INFO")


def _records(log_dir: Path, filename: str = "app.log") -> list[dict[str, Any]]:
    path = log_dir / filename
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _feedback_rows(session: AsyncRecordingSession) -> list[RetrievalFeedback]:
    return session.added_of(RetrievalFeedback)


# ────────────────────────────────────────────────────── 合法提交：本地留痕


def test_a_legal_score_lands_one_row_that_says_who_what_and_how_much(
    api: _FeedbackApi,
) -> None:
    """一次合法打分：本地留一行（哪个租户、哪次检索、几分），响应回显最终分数。"""

    body = api.submit(score=4)

    assert body["code"] == 0
    assert body["msg"] == "success"

    rows = _feedback_rows(api.write_session)
    assert len(rows) == 1, "一次打分只留一行"
    row = rows[0]
    assert row.log_id == LOG_ID, "哪次检索"
    assert row.tenant_id == TENANT_ID, "哪个租户"
    assert row.score == 4, "几分"
    assert row.comment == COMMENT
    assert row.id and row.id != row.log_id, "反馈有自己的行 id，与 log_id 不是一个东西"

    assert body["data"] == {"log_id": LOG_ID, "score": 4, "comment": COMMENT}

    # 写入走**独立短事务**：自己 commit、自己 close；请求会话一次都没被用来写 / 提交。
    assert api.write_session.commits == 1
    assert api.write_session.closed is True
    assert api.request_db.commits == 0
    assert _feedback_rows(api.request_db) == []


def test_scoring_the_same_trace_twice_overwrites_the_same_row(api: _FeedbackApi) -> None:
    """重复反馈是**幂等**的：同一个 ``log_id`` 第二次打分改那一行，不新增。

    后写覆盖是刻意的（#105 设计决定 2）：调用方重试不该留下两条互相矛盾的行，行的 ``id``
    也还是第一次那个——它回答的是「这份反馈什么时候开始存在」。迁移到异步栈之后这条
    语义必须原样成立，否则重试就从「覆盖」变成「多一行」。
    """

    first = api.submit(score=2)
    second = api.submit(score=5, comment="换成 5 分")

    assert first["code"] == second["code"] == 0
    assert second["data"] == {"log_id": LOG_ID, "score": 5, "comment": "换成 5 分"}

    rows = _feedback_rows(api.write_session)
    assert len(rows) == 1, "同一个 log_id 只该有一行"
    assert rows[0].score == 5, "后写覆盖"
    assert rows[0].comment == "换成 5 分"
    assert api.write_session.commits == 2, "两次提交，但只落一行"


# ────────────────────────────────────────────────────── 分数区间：两端合法，越界被拒


@pytest.mark.parametrize("score", [1, 5])
def test_both_ends_of_the_scale_are_accepted(api: _FeedbackApi, score: int) -> None:
    """1 与 5 都在契约里：区间是闭的，不是「2~4 随便挑一个」。"""

    body = api.submit(score=score)

    assert body["code"] == 0
    assert body["data"]["score"] == score
    rows = _feedback_rows(api.write_session)
    assert [row.score for row in rows] == [score]


@pytest.mark.parametrize("score", [0, 6])
def test_a_score_outside_the_scale_is_rejected_with_its_own_code(
    api: _FeedbackApi, score: int
) -> None:
    """越界是**明确的业务失败**（``20022``），不是参数校验失败的 ``20002``。

    这两件事对调用方是两回事：一个是「你填了个 6」，另一个是「你少传了一个字段」。
    驳回之后库里一行都不许留——先写后校验会把一次非法提交变成一条脏数据。
    """

    body = api.submit(score=score)

    assert body["code"] == ErrorCode.FEEDBACK_INVALID_SCORE
    assert body["code"] == 20022
    assert body["data"] is None
    assert body["msg"].strip(), "驳回要带一句能看懂的话"
    assert _feedback_rows(api.write_session) == []
    assert api.write_session.commits == 0


# ────────────────────────────────────────── 归属校验：三种失败，一个码、一句话


def _ownership_cases() -> dict[str, dict[str, Any]]:
    """三种「这次提交不成立」的场景，故意用**同一份请求**去撞。

    差别只在库里看到什么：没有那一行 / 那一行是别人的 / 那一行的 ``trace_id`` 对不上。
    对外的答复必须逐字相同——否则「不属于你」与「不存在」的差别就能被拿来试出
    「某个 ``log_id`` 在别人那里存在」。
    """

    return {
        "log_id_not_recorded": {"traces": []},
        "trace_id_does_not_belong_to_that_log_id": {
            "traces": [_trace_row(trace_id=trace_id_for("a-different-log"))]
        },
        "log_id_belongs_to_another_tenant": {"traces": [_trace_row(tenant_id=OTHER_TENANT)]},
    }


@pytest.mark.parametrize("case", sorted(_ownership_cases()))
def test_ownership_failures_are_rejected_with_the_mismatch_code(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """三种归属失败都回 ``20021``，且**一行都不写**。"""

    with _offline_api(monkeypatch, **_ownership_cases()[case]) as harness:
        body = harness.submit()

    assert body["code"] == ErrorCode.FEEDBACK_LOG_MISMATCH
    assert body["code"] == 20021
    assert body["data"] is None
    assert _feedback_rows(harness.write_session) == []
    assert harness.write_session.commits == 0


def test_every_ownership_failure_says_exactly_the_same_thing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一句话，一个字都不许差：文案本身就是防探测的一部分。"""

    answers = []
    for case in sorted(_ownership_cases()):
        with _offline_api(monkeypatch, **_ownership_cases()[case]) as harness:
            answers.append(harness.submit())

    assert len({answer["msg"] for answer in answers}) == 1, answers
    assert len({answer["code"] for answer in answers}) == 1, answers


# ──────────────────────────────────────────────── 本地写库失败：唯一的「提交失败」


def test_a_failed_local_write_is_a_business_failure_not_a_server_error(
    monkeypatch: pytest.MonkeyPatch, log_dir: Path
) -> None:
    """本地库写不进去 = ``20020``（「本地写入失败」）。

    这是本接口**唯一**能失败在服务端的那一类原因。它必须是业务失败（HTTP 200 + 非 0 码），
    不是 500：调用方能做的事很清楚——稍后重试。观测服务不可用不再属于这一类。
    """

    def explode() -> None:
        raise RuntimeError("psycopg: connection refused")

    with _offline_api(monkeypatch, traces=[_trace_row()], session_factory=explode) as harness:
        body = harness.submit()

    assert body["code"] == ErrorCode.FEEDBACK_FAILED
    assert body["code"] == 20020
    assert body["data"] is None
    assert body["msg"].strip(), "失败要带一句能看懂的话"
    assert not _records(log_dir, "error.log"), "写不进去是业务失败，不是服务异常"


def test_a_write_that_fails_half_way_rolls_back_and_reports_the_same_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写到一半失败（``flush`` 抛）：同一个 ``20020``，且**没有提交**、回滚过一次。

    与「开不出会话」是两类失败，但对外同一句话。这里额外钉住收尾：旁路短事务没有提交，
    所以「提交成功 = 库里有一行」这条承诺不会被半成品破坏。
    """

    write_session = _BrokenWriteSession()

    with _offline_api(monkeypatch, traces=[_trace_row()], write_session=write_session) as harness:
        body = harness.submit()

    assert body["code"] == ErrorCode.FEEDBACK_FAILED
    assert write_session.commits == 0, "失败的一笔不许落库"
    assert write_session.rollbacks == 1, "失败的一笔要明确回滚"
    assert write_session.closed is True, "旁路会话无论成败都要关掉"


# ──────────────────────────────────────────── 观测旁路失败：本地已收下，响应照常


def test_a_failed_observation_push_still_succeeds_with_one_warning(
    api: _FeedbackApi, log_dir: Path
) -> None:
    """推分数失败（观测栈挂了）**不影响响应**：本地权威已经写下了这一行。

    这是 #105 决策 2 的可验证形态，也是「观测全挂时响应逐字节相同」那条红线在反馈上的落点。
    迁移到异步栈之后它必须原样成立——推分仍然排在本地写入之后，失败仍然只留一条 warning。
    """

    set_trace_sink(_ExplodingSink())

    body = api.submit(score=3)

    assert body["code"] == 0
    assert body["data"] == {"log_id": LOG_ID, "score": 3, "comment": COMMENT}
    assert len(_feedback_rows(api.write_session)) == 1, "本地权威先落地，观测失败不改它"
    assert api.write_session.commits == 1

    warnings = [
        record
        for record in _records(log_dir)
        if record.get("msg") == OBSERVABILITY_SCORE_FAILED_MESSAGE
    ]
    assert len(warnings) == 1, "一次失败只留一条 warning"
    assert warnings[0]["level"] == "warning"
    assert warnings[0]["trace_id"] == TRACE_ID
    assert not _records(log_dir, "error.log"), "旁路失败不是 ERROR"
