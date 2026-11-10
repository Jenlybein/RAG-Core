"""#105 · 反馈以本地为准：归属校验、越界拒绝、观测旁路。

三个接缝，全是**外部可观察**的（#98 已定的 S1 一类，不新增接缝）：

* ASGI ``POST /api/v1/rag/feedback``：响应体（``code`` / ``msg`` / ``data``）；
* 会话注入点：请求会话 ``get_db`` 的替身（归属校验读的那一侧）与进程级
  ``app.db.session_factory``（本地写入的那一侧）——两者**必须分开**，否则
  「写入走独立短事务、请求会话从不 commit」这件事无从证明；
* 观测 sink 注入点（``app.observability.set_trace_sink``）与 ``logs/app.log``：
  推分是旁路，失败只留一条 warning，绝不改响应。

数据库一律是 ``tests/fake_db.RecordingSession``，观测一律是假客户端 / 假 sink：
整套不连 PostgreSQL、不连 Langfuse、不发网络。

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
from app.core.config import settings
from app.core.exceptions import ErrorCode
from app.models.retrieval_feedback import RetrievalFeedback
from app.models.retrieval_trace import RetrievalTrace
from app.services.trace_identity import trace_id_for
from tests.fake_db import RecordingSession

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


class _FeedbackApi:
    """一个离线应用边界 + 两条会话（请求侧 / 写入侧）的记录。"""

    def __init__(
        self,
        client: TestClient,
        request_db: RecordingSession,
        write_session: RecordingSession,
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
    write_session: RecordingSession | None = None,
    session_factory: Any = None,
) -> Iterator[_FeedbackApi]:
    """离线 ASGI 边界；两条会话都是替身，观测去向是空实现（见 ``tests/conftest.py``）。

    ``traces`` 是请求会话里**能看到**的归属索引行（缺省：一条合法样本）。``session_factory``
    用来让「本地写入那一步失败」这件事可被观察——它与请求会话刻意是两条路。
    """

    main = importlib.import_module("app.main")
    session_mod = importlib.import_module("app.db.session")
    factory_mod = importlib.import_module("app.db.session_factory")

    from app.api.deps import get_current_key

    request_db = RecordingSession(rows={RetrievalTrace: list(traces or [])})
    write_session = write_session if write_session is not None else RecordingSession()
    factory_mod.set_session_factory(session_factory or (lambda: write_session))

    main.app.dependency_overrides[session_mod.get_db] = lambda: request_db
    main.app.dependency_overrides[get_current_key] = lambda: KeyContext(tenant_id=tenant_id)
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            yield _FeedbackApi(client, request_db, write_session)
    finally:
        main.app.dependency_overrides.clear()
        factory_mod.set_session_factory(None)


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


def _feedback_rows(session: RecordingSession) -> list[RetrievalFeedback]:
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
        "log_id_belongs_to_another_tenant": {
            "traces": [_trace_row(tenant_id=OTHER_TENANT)]
        },
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

    with _offline_api(
        monkeypatch, traces=[_trace_row()], session_factory=explode
    ) as harness:
        body = harness.submit()

    assert body["code"] == ErrorCode.FEEDBACK_FAILED
    assert body["code"] == 20020
    assert body["data"] is None
    assert body["msg"].strip(), "失败要带一句能看懂的话"
    assert not _records(log_dir, "error.log"), "写不进去是业务失败，不是服务异常"


