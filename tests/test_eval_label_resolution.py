"""评测标签解析的离线守卫：标签按节写，一个节可以被切成多片。

切块策略改细之后，同一 ``heading_path`` 下会有「整表片 + 每个数据行各一片」
或「超长节窗口切成多片」。标签是按节写的，不该因此判成「标注写错」；但
**0 片仍然必须当场失败**，否则语料被改了会静默算成漏召（见 issue #62）。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from types import ModuleType

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "eval_retrieval.py"


def _load_eval_module() -> ModuleType:
    """只加载一次评测脚本；它 import 时会读配置但不连 PG / ES / 网络。"""

    spec = importlib.util.spec_from_file_location("eval_retrieval_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ev() -> ModuleType:
    return _load_eval_module()


def _chunk(ev: ModuleType, chunk_id: str, document: str, heading: str):
    return ev.IndexedChunk(
        chunk_id=chunk_id,
        document_id=f"eval-doc-{document}",
        kb_id="kb-eval",
        heading_path=heading,
        order_index=0,
        content_head="",
    )


def _question(ev: ModuleType, qid: str, *, labels, trap=()):
    return ev.Question(
        id=qid,
        type="exact_code",
        query="q",
        note="",
        labels=[tuple(x) for x in labels],
        trap=[tuple(x) for x in trap],
    )


def test_a_heading_with_multiple_chunks_resolves_to_all_of_them(ev: ModuleType) -> None:
    """一节被切成 3 片 → 三片都算这一题的相关切片，不拒绝、不静默丢弃。"""

    chunks = [
        _chunk(ev, "c1", "price_list", "价格 > 整机指导价"),
        _chunk(ev, "c2", "price_list", "价格 > 整机指导价"),
        _chunk(ev, "c3", "price_list", "价格 > 整机指导价"),
    ]
    q = _question(ev, "q78", labels=[("price_list", "价格 > 整机指导价")])

    resolved = ev._resolve_question_labels([q], chunks, field="relevant")

    assert resolved["q78"] == ["c1", "c2", "c3"]


def test_zero_chunks_still_fails_loudly(ev: ModuleType) -> None:
    """0 片说明标注写错或语料被改，必须当场 SystemExit，不许算成漏召。"""

    q = _question(ev, "q1", labels=[("nope", "不存在的节")])

    with pytest.raises(SystemExit) as excinfo:
        ev._resolve_question_labels([q], [], field="relevant")

    assert "q1" in str(excinfo.value)


def test_trap_field_uses_the_same_rule(ev: ModuleType) -> None:
    """陷阱对那一栏也允许多片，否则 137 题里带 twin 标注的仍会卡住。"""

    chunks = [
        _chunk(ev, "c1", "support_errors", "存储告警 > 一"),
        _chunk(ev, "c2", "support_errors", "存储告警 > 一"),
    ]
    q = _question(ev, "q5", labels=[], trap=[("support_errors", "存储告警 > 一")])

    resolved = ev._resolve_question_labels([q], chunks, field="trap")

    assert resolved["q5"] == ["c1", "c2"]
