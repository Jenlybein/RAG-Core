"""#138 收窄：独立编排块**整条撤销**之后的结构守卫（ADR-0029 决策二）。

这不是一次修复，是一次**有意的能力撤销**。撤销的理由写在 ADR-0029 与
`docs/adr/0010-retrieval-contract.md` 的第十一次修订里，一句话：材料一条没少、
每题多付 22.5 秒与两次模型调用，而「要点 + 角色」这个中间结构**对外没有消费者**。

因此本文件守的是**删除本身**：编排这个名字不许在 `app/` 里留下任何一条代码路径。
写这样的用例而不是「删完就算」的理由很直接：删除最容易在后续票里被悄悄复活的，
就是这类「先装开关、后长功能」的机制——补一个字段比删一个文件省事。

**保留什么**（同样是判决的一部分，见 ADR-0029「保留下来的两样东西」）：

* 「模型只点序号、正文回库重建」与三态完整度这两条**搬进了 Research**，所以
  ``app/services/citation_blocks.py`` 仍然导出 ``COMPLETENESS_*`` 并照旧使用；
* ``eval/reports/`` 里关于编排的测量不删——它们是这次否决的证据。

Research 的收窄（分解独立于材料）是另一张票的事，本文件只管「编排没有回来」。
"""

from __future__ import annotations

from pathlib import Path

from app.services.citation_blocks import (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_MISSING,
    COMPLETENESS_PARTIAL,
)

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

#: 被撤销的那个名字。大小写都算：常量（``ORCHESTRATION_TIMEOUT_MS``）与列
#: （``orchestration_allowed``）是同一个能力的两种写法。
_BANNED = "orchestration"


def _mentions_anywhere() -> list[str]:
    """`app/` 下任何一行（注释、文档串、代码）里出现这个名字，就算残留。"""

    hits: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _BANNED in line.lower():
                hits.append(f"{path.relative_to(APP_ROOT.parent)}:{number}: {line.strip()}")
    return hits


def test_the_service_and_its_deployment_budget_are_gone() -> None:
    """服务文件删掉、部署口径的那个超时也删掉——没有服务就不该留一个只服务它的预算。

    ``ORCHESTRATION_TIMEOUT_MS`` 是「两次串行结构化调用共用的整步预算」，它只被那个
    服务读；留着一个没有读者的配置项，下一次会有人以为它是别的东西的预算。
    """

    assert not (APP_ROOT / "services" / "orchestration.py").exists(), (
        "编排服务必须整条删除，不是改成空壳"
    )

    config = (APP_ROOT / "core" / "config.py").read_text(encoding="utf-8")
    assert "ORCHESTRATION" not in config.upper(), "部署配置里不许留一个没有读者的超时项"


def test_the_retrieve_path_no_longer_calls_it() -> None:
    """组合根不再引用它：`/rag/retrieve` 回到「毫秒级、不调模型、不编排」。"""

    service = (APP_ROOT / "services" / "rag_service.py").read_text(encoding="utf-8")
    assert _BANNED not in service.lower()


def test_the_three_state_vocabulary_survives_in_research() -> None:
    """三态完整度**跟着 Research 留下来**：撤销的是编排块，不是这套词。

    ``complete`` / ``partial`` / ``missing`` 的判据（由核心片数出来、不看模型自评）
    正是 ADR-0029 明确要搬进 Research 的那一部分，所以它的常量必须仍然可导入。
    """

    assert (COMPLETENESS_COMPLETE, COMPLETENESS_PARTIAL, COMPLETENESS_MISSING) == (
        "complete",
        "partial",
        "missing",
    )
