"""二期重排：专用 Cross-Encoder 端点（``qwen3.7-text-rerank``，OpenAI 兼容 ``/reranks``）。

ADR-0011 的边界图里 ``RerankProvider`` 是「我们自写」的那一类：本类**只认识 HTTP
与自己的 ABC**——不 import ``app.services``、不读配置、不建业务对象。它不知道「知识库」
「租户」「五列字段从哪来」，只知道「收下一批候选、把它们变成一段报文、按端点给的
下标把分数搬回原切片」。

四条与「重排追求准、但不能把系统搞挂」直接相关的约定：

1. **只改最终排序分**：``score`` / ``rerank_score`` 换成分数本身，融合分与五列分列字段
   原样搬运（走与融合层同一个 ``with_score_columns``）。重排一旦改写「这条怎么来的」，
   可解释性就没了——那正是本项目的验收点。
2. **报文是文档，不是对象**：文档串只有 ``标题`` + 截断后的 ``正文``。分数、租户、
   内部 id 一律不拼：Cross-Encoder 读的是「问题与正文像不像」，其余都是噪音；而把内部
   标识发到外部端点，也是一次没必要的暴露。
3. **候选上限与单条截断都在客户端**：官方对**单条超限**直接 400 且服务端不截断，所以
   截断不是优化而是**必要条件**；候选数封在 :data:`MAX_DOCUMENTS`（50）——配置写多大
   都越不过去，这是这一层自己的护栏。
4. **失败就是失败**：超时、非 2xx、结构不合法、空返回全部抛出去，**不**在这里退回
   「按原顺序返回」。降级是编排层（``app/services/retrieval/rerank.py``）的职责——Provider
   一降级，上层就不知道「这次没重排」，降级标记与原因也就丢了。

**连接从注册表来**（第 10 步票 4）：没有注入客户端时向 ``app.core.http_clients`` 要
「当前事件循环的客户端」，超时（``RERANK_TIMEOUT_SECONDS``）按请求传——同一个池子服务
不同预算的调用，预算不该焊在客户端上。注入的客户端仍优先，且**归调用方管**：本类不替
它决定什么时候关。
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

import httpx

from app.core import http_clients
from app.providers.rerank.base import RerankProvider

# 契约字段的唯一填充点在 ``app/utils``：它不依赖 app 内任何其它层，因此 Provider 可以
# 放心 import——Provider 仍只认识自己的 ABC 与这个纯函数。
from app.utils.retrieval_fields import with_score_columns

#: 官方要求的**英文**指令。中文 instruct 是教程里的说法（内容对、语种错）：官方文档
#: 明确要求英文，中文输入是否被接受过没有任何保证——这不是「换个更好的提示词」，而是
#: 「不用没被验证过的输入」。它是常量而不是配置项：换一句话就是换一次口径，该走代码评审。
INSTRUCT = (
    "Given a question, retrieve passages that contain evidence needed to answer the question."
)

#: 单请求的候选硬上限。官方模型能吃 500 条，但重排池从来不超过几十条；50 是这一层
#: 自己的护栏——配置（``RERANK_MAX_CANDIDATES``）写多大都越不过去。
MAX_DOCUMENTS = 50

#: 单条正文的硬上限（字符）。官方对超过单条 token 上限的文档直接返回 400 且**不做
#: 截断**，所以客户端截断是必要条件；同理，配置（``RERANK_CHUNK_MAX_CHARS``）写大了
#: 也越不过这里——护栏必须在越不过去的地方。
MAX_CHARS_PER_DOC = 1024

#: 与本类配套的默认模型名（``Settings.RERANK_MODEL`` 的缺省值也是它）。两处各写一份是
#: 刻意的：``app.core.config`` 不许 import Provider 层（那会把 httpx 拖进启动路径）。
DEFAULT_MODEL = "qwen3.7-text-rerank"


def _document_of(chunk: dict[str, Any], *, chunk_max_chars: int) -> str:
    """一条候选 → 一段文档串：只有标题与（截断后的）正文。

    截断只影响**送进端点**的内容，不影响回给调用方的切片原文。
    """

    title = str(chunk.get("title") or "")
    content = str(chunk.get("content") or "")
    return f"标题：{title}\n正文：{content[:chunk_max_chars]}"


def _finite_score(raw: Any) -> float:
    """``relevance_score`` 必须是能转成**有限浮点**的值。

    ``nan`` / ``inf`` 不能参与排序：它们会让 ``sorted`` 的顺序变成实现细节，而「同分
    保持原候选顺序」这条承诺的前提是分数可比。字符串数字（``"0.5"``）按可转换处理——
    那不会误导任何人。
    """

    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"relevance_score 不能转成浮点数：{raw!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"relevance_score 必须是有限数，当前是 {value!r}")
    return value


def _scores_by_index(data: Any, *, count: int) -> dict[int, float]:
    """把端点的返回值收敛成 ``{原候选下标: 分数}``；结构不合法就抛 ``ValueError``。

    **按下标回填，不做文本匹配**：端点给的 ``index`` 就是「这是我收到的第几条」，
    拿正文去反查既慢又会在重复切片上出错。下标越界同样算结构不合法——它说明端点
    回的不是这次请求的结果。
    """

    if not isinstance(data, dict):
        raise ValueError("重排端点返回的不是 JSON 对象")
    results = data.get("results")
    if not isinstance(results, list):
        raise ValueError("重排端点返回的 results 不是列表")
    if not results:
        # 空返回是失败：上层据此走降级，而不是把「什么都没排」当成「排完了」。
        raise ValueError("重排端点没有返回任何排序")

    scores: dict[int, float] = {}
    for item in results:
        if not isinstance(item, dict):
            raise ValueError("重排端点返回的 results 里有非对象项")
        index = item.get("index")
        # ``True`` 是 ``int`` 的子类：不排除的话 ``index=True`` 会被当成第 1 条。
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError(f"重排端点返回的下标不是整数：{index!r}")
        if index < 0 or index >= count:
            raise ValueError(f"重排端点返回的下标越界：{index}（本次共 {count} 条候选）")
        score = _finite_score(item.get("relevance_score"))
        # 同一条说了两遍时取最高分：顺序必须可复现，不能取决于端点的行文顺序。
        previous = scores.get(index)
        scores[index] = score if previous is None else max(previous, score)
    return scores


class Qwen3RerankProvider(RerankProvider):
    """把融合后的候选交给专用 Cross-Encoder 端点重排。

    ``base_url`` / ``api_key`` / ``model`` 与三个预算由工厂从配置读出来传进来：本类
    不读 ``settings``（Provider 不认识配置从哪来），也不建 ``httpx.AsyncClient`` 以外
    的任何东西。``client`` 是给测试与 #117 的注入点——注入时**不**由本类关闭。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = DEFAULT_MODEL,
        max_candidates: int = MAX_DOCUMENTS,
        chunk_max_chars: int = 1024,
        timeout_seconds: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("重排端点地址不能为空")
        if not api_key.strip():
            raise ValueError("重排端点密钥不能为空")
        if max_candidates < 1:
            raise ValueError(f"候选上限必须为正数，当前是 {max_candidates!r}")
        if chunk_max_chars < 1:
            raise ValueError(f"单条内容上限必须为正数，当前是 {chunk_max_chars!r}")
        if timeout_seconds <= 0:
            raise ValueError(f"重排超时必须为正数，当前是 {timeout_seconds!r}")

        # 路径段是**端点自己的**：embedding 走 ``compatible-mode/v1``，重排走
        # ``compatible-api/v1``，两者不能互相顶替（见 .env.example）。
        self._url = base_url.rstrip("/") + "/reranks"
        self._api_key = api_key
        self._model = model
        self._max_candidates = min(max_candidates, MAX_DOCUMENTS)
        # 单条上限同样**在 Provider 内部夹死**：官方对超 token 的单条直接 400 且不截断，
        # 所以「配置写大了」不能变成一次 400——护栏必须在越不过去的地方。
        self._chunk_max_chars = min(chunk_max_chars, MAX_CHARS_PER_DOC)
        self._timeout_seconds = timeout_seconds
        self._client = client

    @property
    def provider_name(self) -> str:
        return "qwen3"

    @property
    def model_name(self) -> str:
        return self._model

    async def rerank(self, *, query: str, chunks: list[dict], top_n: int) -> list[dict]:
        """按端点给的分数降序回填原切片；同分保持原候选顺序（确定性）。"""

        candidates = list(chunks[: self._max_candidates])
        if not candidates or top_n < 1:
            # 没有候选 / 没有要的条数：不必花一次远程调用，也不是失败。
            return []

        # 返回条数只由调用方的 ``top_n`` 决定；同时不可能多于送去的文档条数——
        # 向端点要 10 条却只发 2 条，是自相矛盾的请求。
        limit = min(top_n, len(candidates))
        payload = {
            "model": self._model,
            "query": query,
            "documents": [
                _document_of(chunk, chunk_max_chars=self._chunk_max_chars) for chunk in candidates
            ],
            "top_n": limit,
            "instruct": INSTRUCT,
        }

        data = await asyncio.wait_for(self._post(payload), timeout=self._timeout_seconds)
        scores = _scores_by_index(data, count=len(candidates))

        ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        return [
            {
                # 五个分列字段**只搬运、不重算**，且走与融合层同一个 ``with_score_columns``。
                **with_score_columns(candidates[index]),
                "score": score,
                "rerank_score": score,
            }
            for index, score in ordered[:limit]
        ]

    async def _post(self, payload: dict[str, Any]) -> Any:
        """发一次请求并返回解析后的 JSON；非 2xx 由 ``raise_for_status`` 抛出。"""

        headers = {"Authorization": f"Bearer {self._api_key}"}
        client = self._client if self._client is not None else http_clients.get_http_client()
        response = await client.post(
            self._url,
            headers=headers,
            json=payload,
            # 预算按用途传：注册表里的客户端是共用的，超时不能写死在它身上。
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        return response.json()
