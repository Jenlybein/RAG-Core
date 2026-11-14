"""结构守卫：轻量图谱（ADR-0027 / 第 11 步 #144 / #152）已被**有意撤销**，不许悄悄长回来。

**为什么是结构守卫而不是行为用例**：这是一次能力撤销，没有新行为可断言。可观察的事实只有
三件——那几个模块与名字不再存在、内核与组合根不再有注入位、响应契约里不再有那两样东西。
行为用例会一对一对地删掉（它们测的是被删掉的能力）；留下这一条，是为了让「重新长出来」
必须是一次显式的、会红的改动。

**撤销理由要写对**（#138 范围变更节，别写成「正则不可靠」）：抽取规则本身达标——补了 R1/R2
之后可确定性定位 37/38、上限 recall 0.974。砍它是因为**价值不成立**：边只存在于作者写了
「见 X 节」的地方，而文档之间大部分有用的关联是隐式的；注入在饱和语料上是 0 条（整库都在
top_k 里，没有「补」的空间）；每来一种新文档写法就要加一条规则，漏掉的边静默消失；而「作者
明确指路」这个低频场景可以由 Research 的定向补证覆盖。考古用 tag
``step11-with-graph-and-orchestration``。
"""

from __future__ import annotations

import inspect
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = REPO_ROOT / "app"
MIGRATION_ROOT = REPO_ROOT / "migrations" / "versions"

#: 被撤销的那几个模块：文件本身不该再存在。
RETIRED_MODULES: tuple[str, ...] = (
    "app/services/section_graph.py",
    "app/services/graph_injection.py",
    "app/services/section_edge_service.py",
    "app/models/section_edge.py",
    "app/repositories/section_edge_repository.py",
)

#: 被撤销的名字：**只列图谱专用的标识符**。「graph」单独一个词不能扫——``paragraph``
#: 这类无关单词里就含着它，那样的守卫会在正确的树上红，于是只能被人删掉。
RETIRED_NAMES: tuple[str, ...] = (
    "section_edge",
    "section_graph",
    "graph_injection",
    "graph_factory",
    "SectionEdge",
    "SectionGraph",
    "InjectionOutcome",
    "extract_section_edges",
)


def _offenders(root: Path, names: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        found.extend(
            f"{path.relative_to(REPO_ROOT).as_posix()}: {name}" for name in names if name in text
        )
    return found


def test_the_graph_modules_are_gone() -> None:
    residual = [module for module in RETIRED_MODULES if (REPO_ROOT / module).exists()]

    assert residual == [], "图谱模块已随 #138 撤销，这些文件不该还在"


def test_no_graph_name_survives_under_app() -> None:
    offenders = _offenders(APP_ROOT, RETIRED_NAMES)

    assert offenders == [], "app/ 里还有图谱的名字：删代码不留残名"


def test_the_kernel_and_the_pipeline_have_no_injection_slot() -> None:
    from app.services.rag_service import retrieve
    from app.services.retrieval.kernel import retrieve_kernel
    from app.services.retrieval.pipeline import hybrid_retrieve

    assert "graph_injection" not in inspect.signature(retrieve_kernel).parameters
    assert "graph_injection" not in inspect.signature(hybrid_retrieve).parameters
    assert "graph_factory" not in inspect.signature(retrieve).parameters


def test_the_response_contract_has_no_injection_marker() -> None:
    from app.schemas.rag import RetrievedChunk

    assert "retrieval_source" not in RetrievedChunk.model_fields


def test_only_the_historical_create_and_the_new_drop_mention_the_table() -> None:
    """迁移里提到这张表的只允许两条：建它的那条历史迁移、删它的这条新迁移。

    历史迁移一个字都不改（它已经在真库上跑过，`alembic_version` 认的是它的 revision id）；
    撤销只能靠**新增**一条 drop。所以「迁移里不再出现表名」这个说法在这里不成立，能守住
    的只有「不许出现第三条」。
    """

    mentioning = sorted(
        path.name
        for path in MIGRATION_ROOT.glob("*.py")
        if "section_edges" in path.read_text(encoding="utf-8")
    )

    assert len(mentioning) == 2, mentioning
    assert any("drop" in name for name in mentioning), "缺一条删表迁移"
