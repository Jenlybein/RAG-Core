"""RerankProvider 抽象：重排是「和外部依赖说话」的一类能力，因此单独成 Provider。

一期由 ``LLMRerankProvider`` 实现（包着 ``LLMProvider``，自己不建 HTTP 客户端，
ADR-0011）；二期换独立重排端点时也实现这同一个接口，业务编排一行不用改。
"""

from abc import ABC, abstractmethod


class RerankProvider(ABC):
    @property
    def enabled(self) -> bool:
        """这个 Provider 会不会真的重排。

        与 ``KeywordSearchProvider.available`` 是同一类信号：空实现（``RERANK_ENABLED=false``）
        返回 ``False``，编排层据此**干脆不调用**它。「这一步没开」与「重排跑了但顺序没变」
        是两件事——日志里耗时与是否发生必须能分开，否则排障时会误以为模型每次都排过。
        """

        return True

    @abstractmethod
    async def rerank(self, *, query: str, chunks: list[dict], top_n: int) -> list[dict]: ...
