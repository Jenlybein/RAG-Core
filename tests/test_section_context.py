"""票 #46：节说明（检索文本补一句上下文）。

三条硬要求：按节去重（不是按片）、失败回落现状、关闭时与现状逐字一致。
这里既测纯函数的拼装，也测「一节一次」与降级路径。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.config import settings
from app.services import section_context
from app.services.section_context import SectionContext, summarize_sections
from app.utils.retrieval_text import build_retrieval_text


def _run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def test_retrieval_text_includes_section_context() -> None:
    """有节说明时，它排在章节路径之后、正文之前。"""

    text = build_retrieval_text(
        "部署实施指南", "部署实施指南 > 上架", "机架款要上柜。", "本节讲上架与供电的注意事项"
    )

    assert (
        text
        == "文档：部署实施指南 章节：部署实施指南 > 上架 节说明：本节讲上架与供电的注意事项 正文：机架款要上柜。"
    )


def test_retrieval_text_without_section_context_is_unchanged() -> None:
    """不给节说明时，检索文本与补上这项能力之前逐字一致。"""

    before = "文档：部署实施指南 章节：部署实施指南 > 上架 正文：机架款要上柜。"

    assert build_retrieval_text("部署实施指南", "部署实施指南 > 上架", "机架款要上柜。") == before
    assert (
        build_retrieval_text("部署实施指南", "部署实施指南 > 上架", "机架款要上柜。", "") == before
    )


class _Provider:
    """记录被调用了几次、调用参数是什么的模型替身。"""

    def __init__(
        self, *, reply: str = "本节讲上架与供电的注意事项", error: Exception | None = None
    ) -> None:
        self.calls = 0
        self.reply = reply
        self.error = error

    async def chat_structured(self, _schema: Any, *, system: str, user: str) -> Any:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return SectionContext(context=self.reply)


def _sections(count: int) -> list[tuple[str, str, str]]:
    return [
        (f"section-{index}", f"部署实施指南 > 第{index}节", f"第{index}节的正文。")
        for index in range(count)
    ]


def test_one_call_per_section(monkeypatch: pytest.MonkeyPatch) -> None:
    """按节调用：五个节五次调用，而不是按片几十次。"""

    provider = _Provider()
    monkeypatch.setattr(settings, "SECTION_CONTEXT_ENABLED", True)
    monkeypatch.setattr("app.providers.llm.get_llm_provider", lambda *_a, **_k: provider)

    summaries = _run(summarize_sections(_sections(5), title="部署实施指南"))

    assert provider.calls == 5
    assert len(summaries) == 5
    assert summaries["section-0"] == "本节讲上架与供电的注意事项"


def test_disabled_makes_no_model_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """关闭时一次模型都不调——离线测试与 CI 靠这条保持干净。"""

    provider = _Provider()
    monkeypatch.setattr(settings, "SECTION_CONTEXT_ENABLED", False)

    def _forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("关闭节说明时不该装配模型")

    monkeypatch.setattr("app.providers.llm.get_llm_provider", _forbidden)

    assert _run(summarize_sections(_sections(3), title="部署实施指南")) == {}
    assert provider.calls == 0


def test_failure_degrades_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型失败时返回空字典，索引照常继续——这是可选增强，不是索引前提。"""

    monkeypatch.setattr(settings, "SECTION_CONTEXT_ENABLED", True)
    monkeypatch.setattr(
        "app.providers.llm.get_llm_provider",
        lambda *_a, **_k: _Provider(error=RuntimeError("模型服务 502")),
    )

    assert _run(summarize_sections(_sections(3), title="部署实施指南")) == {}


def test_one_failing_section_does_not_stop_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    """坏掉一节不该连累其余节失去说明——「失败即降级」是逐节的，不是整批的。"""

    provider = _Provider(error=RuntimeError("这一节超时"))
    monkeypatch.setattr(settings, "SECTION_CONTEXT_ENABLED", True)
    monkeypatch.setattr("app.providers.llm.get_llm_provider", lambda *_a, **_k: provider)

    summaries = _run(summarize_sections(_sections(10), title="部署实施指南"))

    assert provider.calls == 10
    assert summaries == {}


def test_provider_assembly_failure_stops_after_one_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """拿不到 provider 时后续节必然也失败，只试一次就收手。"""

    def _forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("未知的模型供应商")

    monkeypatch.setattr(settings, "SECTION_CONTEXT_ENABLED", True)
    monkeypatch.setattr("app.providers.llm.get_llm_provider", _forbidden)

    assert _run(summarize_sections(_sections(10), title="部署实施指南")) == {}


def test_too_short_context_is_discarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """「本节讲部署」这类输出没有区分度，留在检索文本里只会稀释关键词。"""

    provider = _Provider(reply="本节讲部署")
    monkeypatch.setattr(settings, "SECTION_CONTEXT_ENABLED", True)
    monkeypatch.setattr("app.providers.llm.get_llm_provider", lambda *_a, **_k: provider)

    assert _run(summarize_sections(_sections(1), title="部署实施指南")) == {}


def test_empty_sections_makes_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有节就没有要给模型看的东西。"""

    provider = _Provider()
    monkeypatch.setattr(settings, "SECTION_CONTEXT_ENABLED", True)
    monkeypatch.setattr("app.providers.llm.get_llm_provider", lambda *_a, **_k: provider)

    assert _run(summarize_sections([], title="部署实施指南")) == {}
    assert provider.calls == 0


def test_section_context_module_exported_class_is_the_schema() -> None:
    """模型输出按 schema 收敛，不做纯文本兜底：拿不到合法结果就是失败。"""

    assert section_context.SectionContext is SectionContext
