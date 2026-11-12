"""RerankProvider 抽象：重排是「和外部依赖说话」的一类能力，因此单独成 Provider。

一期由 ``LLMRerankProvider`` 实现（包着 ``LLMProvider``，自己不建 HTTP 客户端，
ADR-0011）；二期换独立重排端点（``Qwen3RerankProvider``）时也实现这同一个接口，
业务编排一行不用改。
"""

from abc import ABC, abstractmethod


class RerankProvider(ABC):
    @property
    def provider_name(self) -> str:
        """这一实现的名字（``qwen3`` / ``llm`` / ``none``）。

        它是**身份**而不是**状态**：响应元数据里的 ``rerank.provider`` 与评测环境快照
        都读它，用来回答「这一次到底是哪种重排在跑」。状态（这次排没排、排成没成）由
        ``available`` 与返回值表达，别把两者混成一个字段。

        缺省 ``"unknown"`` 只为了不让「新加了一个实现、忘了填身份」变成 import 期报错；
        三个既有实现都自己填（``tests/test_rerank_provider.py`` 逐个钉住）。
        """

        return "unknown"

    @property
    def model_name(self) -> str | None:
        """这一实现用的模型名；没有模型可言时是 ``None``（空实现）。

        与 ``provider_name`` 一起进响应元数据：同一种实现换模型（``RERANK_MODEL``）时，
        只写 provider 会让人以为读数没变。
        """

        return None

    @property
    def available(self) -> bool:
        """这项能力在这台机器上存不存在（会不会真的重排）。

        与 ``KeywordSearchProvider.available`` 是**同一个信号、同一个名字**：空实现
        （``RERANK_ENABLED=false``）返回 ``False``，编排层据此**干脆不调用**它。
        两个 Provider 用两个名字（曾经一个叫 ``enabled``、一个叫 ``available``）会让人
        以为它们说的不是一件事——「这一步没开」与「重排跑了但顺序没变」才是两件事，
        那条区分靠返回值与元数据表达，不靠属性名。

        「没开」与「跑了但顺序没变」必须能在日志与耗时里分开，否则排障时会误以为
        模型每次都排过。
        """

        return True

    @abstractmethod
    async def rerank(self, *, query: str, chunks: list[dict], top_n: int) -> list[dict]: ...
