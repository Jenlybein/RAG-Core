"""旁证跑批的接线层：裁判与召回**从哪来**（#109）。

单独一层是为了让 :mod:`app.services.side_evidence` 保持纯净（纯函数 + 可注入依赖，
离线可测），而「真库里裁判用哪套模型、召回走哪条链路」这类环境问题集中在一处：

* **裁判用库里的模型配置**（``model_configs``），与运行时装配同一套；库不可达才回落
  ``.env``。本仓库里两个来源都可能存在，官方口径是「运行时优先读库」（#107 报告里记过
  这一条：只读 ``.env`` 会让人把「库配置可用」误判成「账户欠费」）。
* **召回走向量 + 关键词 + 融合的真实链路**（``rag_service.retrieve``，与 ``eval_runner``
  同一套）。旁证要回答的是「我们真正返回的片段能不能覆盖标准答案」，所以**不能**拿标注
  切片当召回结果——那样两指标必然接近 1.0，等于自己跟自己比。

裁判失败（欠费 / 限流 / 超时）会在这里表现为每题一条 ``error``，最终由
:func:`app.services.side_evidence.run_side_evidence` 汇总成 ``status=failed``，
而不是悄悄产出一份「CP/CR 都是 0」的报告。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import structlog

from app.services.side_evidence import run_side_evidence

logger = structlog.get_logger(__name__)


class RagasJudge:
    """把 ragas 的集合版指标包成 ``Judge``；``NaN`` 一律回 ``None``（裁判没评上）。"""

    def __init__(self, *, model: str, precision: Any, recall: Any) -> None:
        self._model = model
        self._precision = precision
        self._recall = recall

    @property
    def model(self) -> str:
        return self._model

    async def _score(
        self, metric: Any, *, question: str, contexts: Sequence[str], reference: str
    ) -> float | None:
        # **直接 await，不再 asyncio.run**：每次调用新起一个事件循环，等于每次丢掉 HTTP
        # 连接池（于是每次重做 TLS 握手），而且拆 loop 时会刷出一片
        # `RuntimeError: Event loop is closed` / `Unclosed client session`——那些噪音不是
        # 无害的，它们会把真正要看的那行日志淹掉。整轮跑批现在共用一个 loop。
        result = await metric.ascore(
            user_input=question, retrieved_contexts=list(contexts), reference=reference
        )
        value = getattr(result, "value", None)
        if value is None or value != value:  # NaN 自己不等于自己
            return None
        return float(value)

    async def context_precision(
        self, *, question: str, contexts: Sequence[str], reference: str
    ) -> float | None:
        return await self._score(
            self._precision, question=question, contexts=contexts, reference=reference
        )

    async def context_recall(
        self, *, question: str, contexts: Sequence[str], reference: str
    ) -> float | None:
        return await self._score(
            self._recall, question=question, contexts=contexts, reference=reference
        )


#: 库里没有模型配置、``.env`` 也没填 ``LLM_BASE_URL`` 时的兜底地址。
#:
#: 它**镜像** ``app/providers/llm/__init__.py`` 里 ``_VENDORS["ark"].default_base_url``：
#: 那张表是「供应商 → 默认地址」的唯一真相，但它对工厂是私有的，而这里只需要一个兜底值。
#: 与其把私有表 import 出来，不如把这份重复摆明面：改供应商地址时，两处一起改，
#: 且有 ``tests/test_llm_provider.py`` 那条名单守卫提醒你别忘。
ARK_DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"


def build_judge() -> RagasJudge:
    """按运行时同一套配置装配裁判：库里的模型配置优先，``.env`` 兜底。"""

    from openai import AsyncOpenAI
    from ragas.llms import llm_factory
    from ragas.metrics.collections import ContextPrecisionWithReference, ContextRecall

    from app.core.config import settings
    from app.db.session import SessionLocal
    from app.repositories import model_config_repository

    base_url = api_key = model = None
    try:
        with SessionLocal() as db:
            # 走仓储而不是自己拼 SQL：``model_config_repository`` 自称「唯一能碰
            # ``model_configs`` 的一层」，绕过去就没人再守那条约定了。
            rows = model_config_repository.list_all(db)
        if rows:
            first = rows[0]
            base_url, model, api_key = first.base_url, first.model, first.api_key
    except Exception as exc:  # noqa: BLE001 - 读配置失败不该让旁证整轮跑不了
        # 但**必须留痕**：静默 pass 会让「裁判用的是 .env 还是库里那套」变成一个
        # 查不出来的问题——那正是本轮实跑踩过的坑（.env 过期 key vs 库里有效 key）。
        logger.warning(
            "side_evidence_model_config_unreadable", error=type(exc).__name__
        )

    if not (base_url and model and api_key):
        base_url = settings.LLM_BASE_URL or ARK_DEFAULT_BASE_URL
        model = settings.LLM_MODEL
        api_key = getattr(settings, "ARK_API_KEY", "")
        logger.warning(
            "side_evidence_judge_falls_back_to_env", model=model, has_key=bool(api_key)
        )

    client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=120)
    llm = llm_factory(model, client=client)
    return RagasJudge(
        model=model,
        precision=ContextPrecisionWithReference(llm=llm),
        recall=ContextRecall(llm=llm),
    )


def build_retriever():
    """进程内真实链路：给定问题，回「这次真正返回的片段正文」。"""

    from app.core.config import settings
    from app.db.async_session import AsyncSessionLocal
    from app.services.eval_manifest import load_manifest
    from app.services.rag_service import retrieve
    from app.services.retrieval_options import RetrievalOptions

    # 只取 manifest 里**有语料**的那几个库：清单还会带上留给「空结果」用例的空库
    # ``kb-eval-empty``，那个库不在就不再建，检索会以「knowledge base not found」整单拒绝。
    #
    # 清单从 ``app.services.eval_manifest`` 读，**不是** ``scripts.eval_retrieval``：这条路径由
    # Worker 执行，应用不该依赖评测脚本的私有函数（``scripts/`` 也没有 ``__init__.py``，
    # 能不能 import 取决于 cwd）。
    layouts, _empty_kb_id = load_manifest()
    kb_ids = [layout.kb_id for layout in layouts]

    async def _run(question: str) -> list[str]:
        # 检索链路在异步栈上（第 11 步票 #127）：旁证由 Worker 执行，会话跟着 Worker 的
        # 常驻循环走（``app/db/worker_loop.py``），连接池因此跨任务复用。
        async with AsyncSessionLocal() as db:
            response = await retrieve(
                db,
                tenant_id=settings.DEV_TENANT_ID,
                kb_ids=kb_ids,
                query=question,
                options=RetrievalOptions(
                    mode="hybrid",
                    top_k=settings.TOP_K,
                    fold_to_sections=True,
                    expand_table_rows=True,
                    rerank=settings.RERANK_ENABLED,
                    rewrite=settings.QUERY_REWRITE_ENABLED,
                ),
            )
        return [str(chunk.get("content") or "") for chunk in response["retrieved_chunks"]]

    return _run


async def run_once(
    *,
    cases: Sequence[dict[str, Any]],
    out_dir: Path,
    run_id: str,
    limit: int | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """一次完整跑批：装配裁判与召回 → 跑 → 落盘。控制台触发与命令行共用这一条。

    ``tenant_id`` 只在控制台触发时传：它决定状态文件里记多少归属信息，不参与「怎么跑」。

    **它是异步的**，因为整轮要共用一个事件循环（见 ``RagasJudge._score`` 的注释）。
    同步的调用方（Celery 任务、命令行脚本）各自 ``asyncio.run(run_once(...))`` 一次。
    """

    from app.core.config import settings

    return await run_side_evidence(
        cases=cases,
        judge=build_judge(),
        retrieve=build_retriever(),
        out_dir=out_dir,
        run_id=run_id,
        limit=limit,
        tenant_id=tenant_id,
        concurrency=settings.SIDE_EVIDENCE_CONCURRENCY,
    )


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
