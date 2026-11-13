"""票 #151：MCP 第三个工具 ``rag_research``——签名、与门禁的接线、三种错误的翻译。

这一层只验**工具本身**：它递什么、不判什么、失败怎么表达。判定序列与状态机分别在
``test_research_entry.py``（HTTP 门禁）与 ``test_research_state_machine.py`` 里逐条钉住；
本文件要证明的是「MCP 这一侧**没有第二份**」——同一份 ``gated_research``、同一份
``ResearchRequest`` 校验、同一套错误码。

三类失败在这里是三种不同的东西（票 #151 的 AC 把它们并列，恰恰因为它们容易被做成一类）：

| 类别 | 例子 | 表达 | 有没有业务码 |
| --- | --- | --- | --- |
| 业务失败 | 越权库 / 没有 Research 授权 / 限流 / 额度用尽 | 工具执行失败（``isError``） | 有（``20008`` / ``20013`` / ``20005`` / ``20014``） |
| 参数非法 | 缺 ``kb_ids``、多给 ``rounds`` | **工具参数校验**失败（``isError``，文本来自调用参数） | 没有——它根本没走到业务层 |
| 内部异常 | 门禁之外的崩溃 | 交给 MCP 运行时（不冒充业务码） | 没有 |
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.mcp_server import create_mcp_server
from tests.fake_db import AsyncSessionAdapter, EmptySession


class _FakeSession(EmptySession):
    """只为 ``list_tools`` 存在：本段用例一个工具都不调用。"""


def _tools() -> dict[str, Any]:
    async def _retrieve(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    async def _list_kbs(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    mcp = create_mcp_server(
        get_async_db=lambda: AsyncSessionAdapter(_FakeSession()),
        retrieve=_retrieve,
        list_kbs=_list_kbs,
    )
    return {tool.name: tool for tool in asyncio.run(mcp.list_tools())}


def test_tools_list_grows_from_two_to_three() -> None:
    """``tools/list`` 从两个变三个，第三个与 HTTP 请求体同形（``kb_ids`` + ``query``）。"""

    tools = _tools()

    assert set(tools) == {"rag_retrieve", "rag_list_knowledge_bases", "rag_research"}
    schema = getattr(tools["rag_research"], "parameters", {}) or {}
    assert set(schema["properties"]) == {"kb_ids", "query"}
    assert schema.get("required") == ["kb_ids", "query"]


def test_the_description_states_the_cost_and_points_at_rag_retrieve() -> None:
    """描述与参数描述都要写明：约 30 秒、有额外模型成本、普通问题先用 ``rag_retrieve``。

    模型的工具选择完全读这两段文字（票 #138 用户故事 29）：描述里不写成本，它就会把
    一次 30 秒、3 次模型调用的调用当成普通检索来用。
    """

    tool = _tools()["rag_research"]
    description = tool.description or ""

    assert "30 秒" in description, "描述里必须写明耗时"
    assert "模型" in description and "成本" in description, "描述里必须写明额外模型成本"
    assert "rag_retrieve" in description, "描述里必须指回普通检索"

    query = tool.parameters["properties"]["query"]
    assert "rag_retrieve" in (query.get("description") or ""), "参数描述里再提一次"
