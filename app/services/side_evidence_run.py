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

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from app.services.side_evidence import run_side_evidence


class RagasJudge:
    """把 ragas 的集合版指标包成 ``Judge``；``NaN`` 一律回 ``None``（裁判没评上）。"""

    def __init__(self, *, model: str, precision: Any, recall: Any) -> None:
        self._model = model
        self._precision = precision
        self._recall = recall

    @property
    def model(self) -> str:
        return self._model

    def _score(self, metric: Any, *, question: str, contexts: Sequence[str], reference: str) -> float | None:
        result = asyncio.run(
            metric.ascore(user_input=question, retrieved_contexts=list(contexts), reference=reference)
        )
        value = getattr(result, "value", None)
        if value is None or value != value:  # NaN 自己不等于自己
            return None
        return float(value)

    def context_precision(self, *, question: str, contexts: Sequence[str], reference: str) -> float | None:
        return self._score(self._precision, question=question, contexts=contexts, reference=reference)

    def context_recall(self, *, question: str, contexts: Sequence[str], reference: str) -> float | None:
        return self._score(self._recall, question=question, contexts=contexts, reference=reference)


def build_judge() -> RagasJudge:
    """按运行时同一套配置装配裁判：库里的模型配置优先，``.env`` 兜底。"""

    from openai import AsyncOpenAI
    from ragas.llms import llm_factory
    from ragas.metrics.collections import ContextPrecisionWithReference, ContextRecall
    from sqlalchemy import text

    from app.core.config import settings
    from app.db.session import SessionLocal

    base_url = api_key = model = None
    try:
        with SessionLocal() as db:
            row = db.execute(
                text("select base_url, model, api_key from model_configs order by created_at limit 1")
            ).mappings().first()
        if row:
            base_url, model, api_key = row["base_url"], row["model"], row["api_key"]
    except Exception:  # noqa: BLE001 - 读配置失败不该让旁证整轮跑不了
        pass

    if not (base_url and model and api_key):
        base_url = settings.LLM_BASE_URL or "https://ark.cn-beijing.volces.com/api/v3"
        model = settings.LLM_MODEL
        api_key = getattr(settings, "ARK_API_KEY", "")

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
    from app.db.session import SessionLocal
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
        with SessionLocal() as db:
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

    return lambda question: asyncio.run(_run(question))


def run_once(
    *,
    cases: Sequence[dict[str, Any]],
    out_dir: Path,
    run_id: str,
    limit: int | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """一次完整跑批：装配裁判与召回 → 跑 → 落盘。控制台触发与命令行共用这一条。

    ``tenant_id`` 只在控制台触发时传：它决定状态文件里记多少归属信息，不参与「怎么跑」。
    """

    return run_side_evidence(
        cases=cases,
        judge=build_judge(),
        retrieve=build_retriever(),
        out_dir=out_dir,
        run_id=run_id,
        limit=limit,
        tenant_id=tenant_id,
    )


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
