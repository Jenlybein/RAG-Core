"""第 13 步 · T3：50003 与四条降级原因。

接缝：错误码枚举与权威全表的**数据契约**、降级原因词表常量。
两者都是「一处定义、多处取用」的契约，直接对常量断言就是测外部行为；不 mock 内部。

真值来自 spec 与既有权威表（``docs/设计决策.md`` 的错误码全表），不从被测模块反推。

``模型角色合一`` 与 ``query rewrite failed`` 退役不在本文件验收：前者与 Research 的
强模型兜底逻辑强耦合（T6 同批），后者产出方是 T4 要重构的改写链（T4 同批）。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.core.exceptions import ErrorCode
from app.services.retrieval.rerank import REASON_RERANK_FAILED, REASON_RERANK_QUOTA_EXHAUSTED

AUTHORITATIVE_DOC = Path(__file__).resolve().parents[1] / "docs" / "设计决策.md"


# ───────────────────────────────────────────────────────────────── 50003


def test_50003_is_the_model_call_failed_code() -> None:
    """所有大模型调用失败共用一个可重试的业务码。"""

    assert ErrorCode.MODEL_CALL_FAILED.value == 50003


def test_50003_is_in_the_authoritative_error_code_table() -> None:
    text = AUTHORITATIVE_DOC.read_text(encoding="utf-8")
    assert re.search(r"\|\s*`50003`\s*\|\s*MODEL_CALL_FAILED\s*\|", text), (
        "docs/设计决策.md 的错误码全表必须登记 50003"
    )


def test_50003_default_message_is_stable_and_not_an_exception_text() -> None:
    from app.core.exceptions import default_message

    assert default_message(ErrorCode.MODEL_CALL_FAILED) == "model call failed"


# ───────────────────────────────────────────────────────────────── 降级词表


def test_the_four_degraded_reasons_are_fixed_english_strings() -> None:
    """四条固定串：两条通道故障、一条重排失败、一条重排额度用尽。"""

    from app.services.retrieval.pipeline import REASON_KEYWORD_FAILED, REASON_VECTOR_FAILED

    reasons = (
        REASON_VECTOR_FAILED,
        REASON_KEYWORD_FAILED,
        REASON_RERANK_FAILED,
        REASON_RERANK_QUOTA_EXHAUSTED,
    )
    assert reasons == (
        "vector search failed",
        "keyword search failed",
        "rerank failed",
        "rerank quota exhausted",
    )
    assert len(set(reasons)) == 4, "一因一码：四条不许重复"
