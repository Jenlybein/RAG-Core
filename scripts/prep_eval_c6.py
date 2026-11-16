"""把 C6（#153）端到端验收要的那套评测环境**一次装好**，而且可以重跑。

```text
① 套餐      专业版：五个授权位 + 三本账额度（四档迁移已写进库里，这里做幂等修复）
② 租户+配置 一个专用租户、一套全能力密钥配置（hybrid / 5 条 / 要重排）、一把 Key
③ 知识库    一个独立库（不与 kb-eval* 相交），structure 切法
④ 入库      3 篇引用语料走**生产入库路径**：上传登记 → Worker 的索引体 → 切片
⑤ 构建      #142 的构建流水线：原样重建到 v2、四项计数校验、单事务激活
```

**④⑤ 里的「建边」已经不在了**（#138 的范围变更撤销了轻量图谱，ADR-0027 变成已被取代）：
切片照旧走生产路径写入，但不再建关系边，环境快照里的 `reference_edges` 因此恒为空——
它旁边有一行 `reference_edges_note` 说明这件事，别把「不测了」读成「一条都没建出来」。
要复现当年那组 on/off 读数，回到 tag `step11-with-graph-and-orchestration`。

## 为什么这些步骤不能省

* **套餐那三列必须显式打开**：迁移只回填 0（``plans`` 是运维编辑的表，迁移不该替运营
  决定额度），种子只对空表生效。不打开的话 Research 在门禁第 2 步就被拒（``20013``），
  而报告会把它读成「Research 不可用」——那是配置问题，不是能力问题。
* **必须有个独立知识库**：``eval/corpus`` 是 137 道基线题的尺子，往那些库里加文档等于
  基线当场失效（#138 验收第 1 条）。这批文档只服务 8 道定向题。
* **必须走生产入库路径**（上传 → 索引），不能直接写 chunks 表：直接写表能凑出「有切片」
  的样子，却拿不到切块器算的 ``section_id`` 与 ``retrieval_text``，于是检索那一路的读数
  与线上不是同一个东西。
* **必须真的构建一次**：那是 #142「索引即构建产物」的那条路径（版本行、四项计数、单事务
  激活），也是 C6 要对照的第二个版本（见下一节）。

## 两个「版本」是怎么来的

新库建出来就声明生效版本 ``v1``（``create_kb`` 的既定行为），上传入库写的就是它。于是
第一次跑 ``IndexBuildService.start_build`` 会把 ``v1`` **收养**成一行 active 版本，再把这次
构建命名为 ``v2``。这正好给出 C6 要的两个可对照版本：

| 版本 | 怎么来的 | 切片 |
| --- | --- | --- |
| ``v1`` | 逐篇摄入（生产上传路径的产物） | 当前切法 |
| ``v2`` | #142 构建流水线**原样重建** | 同一套切法 |

两个版本逐节身份相同、切片数相同，差别只在「谁建的」——那正是 D 票（索引即构建产物）要
证明的东西。切法这一维本轮按票面取的「原样重建」，没有改切分参数（理由与后果写在
``eval/reports/c6-v1-vs-v2.md``）。

## 幂等：重跑不叠加

每一步都先读现状：套餐已达标就零变更、租户在就复用、库在就复用、文档按
``source_filename`` 认领、已经有 ``v2`` 行就不再构建。**不做「先删后建」**：删库会连带删掉
本次的读数，而重跑的目的通常只是「上一次跑到一半」。

## 关键词索引用一个专用别名

本机集群里 ``rag_chunks``（配置里那个名字）还是**物理索引**——#142 之前的老形态。别名与
同名物理索引不能并存，所以在未迁移的集群上激活新版本必然 400
``invalid_alias_name_exception``。既有部署的正规做法是 ``scripts/migrate_keyword_alias.py
--execute``（它**删旧索引**，要在停写窗口里人工跑），本脚本**不做**这件事。

于是本套评测钉一个专用别名（缺省 ``rag_chunks_c6``）：机制与生产逐条相同（物理索引按版本
隔离 ``<别名>__<版本>``、别名指向 active），只是名字不撞老索引，也不碰它一个字节。评测语料
本来就该与线上索引隔离——137 道基线题那套用的是 ``rag_chunks_eval``，同一套做法。

跑 ``eval_refs.py --source live`` 时**必须**带上同一个名字：``ELASTICSEARCH_INDEX=rag_chunks_c6``，
否则它会去查另一个别名派生出的索引，而症状是「关键词一路诚实返回空」（不报错）。

## 用法

```bash
# 一次装完（默认 tenant_c6 / pro / 全能力配置）
uv run python scripts/prep_eval_c6.py

# 只补环境、不构建（想手工观察 v1 那一版时用）
uv run python scripts/prep_eval_c6.py --skip-build

# 并行装第二套
uv run python scripts/prep_eval_c6.py --tenant-id tenant_c6b
```

产物：

* ``eval/reports/c6/environment.json``——**可提交**的环境快照（id、套餐、参数、计数、
  版本行），报告里每个数字都能拿它复核；**不含 Key 原文**；
* ``eval/refs-runs/c6-state.json``——**gitignore** 的本机状态（含 Key 原文），给后续
  ``eval_refs.py`` 命令直接取用。

退出码：0 = 环境齐了且构建已 ready/active；1 = 中途失败（原因打印在 stderr）。
模块顶层不解析 argv、不连接数据库，导入即安全；纯判定与快照拼装都是独立函数，
``tests/test_prep_eval_c6.py`` 直接测它们。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # 以 ``python scripts/...`` 方式运行时补上仓库根
    sys.path.insert(0, str(REPO_ROOT))

from app.core.logging import setup_logging  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ops_common import print_summary  # noqa: E402
from rebuild_kb_version import run_rebuild  # noqa: E402

logger = structlog.get_logger(__name__)

CORPUS_DIR = REPO_ROOT / "eval" / "corpus-refs"
CORPUS_MANIFEST = CORPUS_DIR / "manifest.json"

#: 可提交的环境快照（只放非机密事实）。
ENVIRONMENT_FILE = REPO_ROOT / "eval" / "reports" / "c6" / "environment.json"
#: 本机状态：含 Key 原文，落在 .gitignore 里的目录（与 eval_refs.py 的产物同一处）。
LOCAL_STATE_FILE = REPO_ROOT / "eval" / "refs-runs" / "c6-state.json"

#: 这个脚本有一部分读数测的是**已否决方案**（轻量图谱的引用边）。它被 #138 的范围变更
#: 撤销了，实现只在 tag 里。撤销理由不是「规则不准」（上限 recall 0.974），而是价值不成立。
REJECTED_SCHEME_TAG = "step11-with-graph-and-orchestration"
REJECTED_SCHEME_NOTE = (
    f"图谱的引用边读数属于已否决方案，代码在 tag `{REJECTED_SCHEME_TAG}`；"
    "本次环境准备照常跑完，只是不再测量边数（撤销理由：价值不成立，不是抽取不准）。"
)

#: 定向题集里那个知识库的展示名（``eval/corpus-refs/manifest.json`` 的 ``kbs[0].name``）。
#: 生产建库**不接受调用方指定的 id**（``create_kb`` 用 ``new_kb_id()``），所以真实 id 由
#: 本脚本打印并写进快照，后续命令一律用它。
REFERENCE_KB_NAME = "引用语料库·会员权益"
REFERENCE_KB_DESCRIPTION = (
    "C6 验收用：3 篇同一业务域（退款 / 促销 / 积分）的中文文档，彼此带显式引用表达"
)

#: 专业版那两列的目标值（#153 §①：迁移回填的是 0，这是刻意的，别改种子）。
PRO_PLAN_CODE = "pro"
#: 专业版那五列授权位与六格额度。四档迁移（``d7e3b8a1f642``）已经把种子值写进库里，
#: 这里再显式给一遍是为了**幂等修复**：手工改过库的环境重跑本脚本能回到评测需要的形状，
#: 第二次跑则零变更（``plan_changes`` 只报不一致的列）。
PRO_PLAN_TARGETS: dict[str, Any] = {
    "retrieve_allowed": True,
    "rewrite_allowed": True,
    "decompose_allowed": True,
    "research_allowed": True,
    "rerank_allowed": True,
    "retrieve_qps": 50,
    "retrieve_daily": 25000,
    "rerank_qps": 15,
    "rerank_daily": 5000,
    "llm_qps": 5,
    "llm_daily": 1500,
}

#: 密钥配置的目标形状：五个授权位全开 + 三本账额度 + 「hybrid / 5 条 / 要重排」三项参数。
#: 第 13 步没有档位，所以这里写的就是最终值；``retrieval_rerank=True`` 要求同时给
#: ``rerank_allowed``，否则自洽校验会以 ``20015`` 拒掉（配置自相矛盾）。
POLICY_TARGETS: dict[str, Any] = {
    "retrieve_allowed": True,
    "rewrite_allowed": True,
    "decompose_allowed": True,
    "research_allowed": True,
    "rerank_allowed": True,
    "retrieve_qps": 50,
    "retrieve_daily": 25000,
    "rerank_qps": 15,
    "rerank_daily": 5000,
    "llm_qps": 5,
    "llm_daily": 1500,
    "retrieval_mode": "hybrid",
    "retrieval_top_k": 5,
    "retrieval_rerank": True,
    "include_parent": True,
    "expand_table_rows": True,
}

#: 本轮两个版本的版本名（见模块文档「两个版本是怎么来的」）。
INGEST_VERSION = "v1"
BUILD_VERSION = "v2"

#: Key 的标签：面板上认得出这把 Key 是干什么的。
KEY_LABEL = "c6-acceptance"

#: 本套评测专用的关键词**别名**。
#:
#: 为什么不用配置里那个 ``rag_chunks``：本机集群里它还是一个**物理索引**（#142 之前的老
#: 形态，既有部署靠 ``scripts/migrate_keyword_alias.py`` 迁到 ``rag_chunks__v1`` + 别名，
#: 而那一步要删旧索引、必须在停写窗口里人工跑）。别名与一个同名物理索引不能并存，所以
#: 在未迁移的集群上激活必然 400 ``invalid_alias_name_exception``。
#:
#: 这里改用一个专用别名：机制与生产**逐条相同**（物理索引按版本隔离、别名指向 active），
#: 只是名字不撞老索引，也不碰它一个字节。评测语料本来就该与线上索引隔离——137 道基线题
#: 那套用的是 ``rag_chunks_eval``（``eval_retrieval._isolate_settings``），同一套做法。
DEFAULT_KEYWORD_ALIAS = "rag_chunks_c6"


# ───────────────────────────────────────────────── 纯函数（可离线测，不碰库）


@dataclass(frozen=True)
class SettingPatch:
    """一次「把目标值写上去」的读数：改了哪些列（重跑第二次应当是空的）。"""

    changes: dict[str, Any] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.changes)

    def as_dict(self) -> dict[str, Any]:
        return {"changes": dict(sorted(self.changes.items()))}


def plan_changes(current: Any, targets: dict[str, Any] | None = None) -> SettingPatch:
    """套餐行还差哪些列（已一致的不出现）——幂等重跑因此能断言「第二次零变更」。"""

    wanted = dict(PRO_PLAN_TARGETS if targets is None else targets)
    return SettingPatch(
        changes={
            column: value
            for column, value in wanted.items()
            if getattr(current, column, None) != value
        }
    )


def policy_changes(
    current: Any, *, kb_id: str, targets: dict[str, Any] | None = None
) -> SettingPatch:
    """密钥配置还差哪些列；``allowed_kb_ids`` 单独判，且要求**恰好只圈这一个库**。

    「恰好一个」是刻意的：定向题只查一个库，圈两个会让「命中来自哪个库」多一层解释，
    而越权检查（``ensure_kb_scope``）也就查不到问题。
    """

    wanted = dict(POLICY_TARGETS if targets is None else targets)
    changes = {
        column: value for column, value in wanted.items() if getattr(current, column, None) != value
    }
    if list(getattr(current, "allowed_kb_ids", []) or []) != [kb_id]:
        changes["allowed_kb_ids"] = [kb_id]
    return SettingPatch(changes=changes)


def reference_edge_count(edges_by_type: dict[str, int]) -> int:
    """引用边（``reference``）的条数——与 ``manifest.json`` 的 ``reference_inventory`` 对账。

    ``sibling`` / ``parent`` 边也在这张表里（同一批节之间还有上下文关系），所以对账只能按
    边型数，不能数总行数：把另外两种算进来，「9 条引用边齐了没有」这件事就看不出来了。
    """

    return int(edges_by_type.get("reference", 0))


@dataclass(frozen=True)
class Correlation:
    """一次环境准备跑出来的**可复核事实**：谁、哪套配置、哪个库、几篇几片几行几边。"""

    tenant_id: str
    policy_id: str
    key_id: str
    kb_id: str
    documents: list[str] = field(default_factory=list)
    versions: list[dict[str, Any]] = field(default_factory=list)
    chunks_by_version: dict[str, int] = field(default_factory=dict)
    sections_by_version: dict[str, int] = field(default_factory=dict)
    edges_by_version: dict[str, dict[str, int]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "policy_id": self.policy_id,
            "key_id": self.key_id,
            "kb_id": self.kb_id,
            "documents": list(self.documents),
            "versions": [dict(row) for row in self.versions],
            "chunks_by_version": dict(sorted(self.chunks_by_version.items())),
            "sections_by_version": dict(sorted(self.sections_by_version.items())),
            "edges_by_version": {
                version: dict(sorted(counts.items()))
                for version, counts in sorted(self.edges_by_version.items())
            },
        }


def environment_snapshot(
    *,
    correlation: Correlation,
    plan_row: dict[str, Any],
    preset_row: dict[str, Any],
    corpus: dict[str, Any],
    keyword_alias: str = DEFAULT_KEYWORD_ALIAS,
) -> dict[str, Any]:
    """把「这次的环境长什么样」拼成一份**不含机密**的 JSON。

    这份文件进仓库，所以只放 id 与计数：报告里的每个数字都要能拿它复核，而 Key 原文
    留在 gitignore 的那份本机状态里。
    """

    versions = {row["version"]: row for row in correlation.versions}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "issue": "#153 (C6) 端到端验收的评测环境",
        "purpose": "v1/v2 对照 · 图谱开/关 · Research 开/关 · 20 题守分抽样",
        "tenant": {
            "tenant_id": correlation.tenant_id,
            "plan": plan_row,
        },
        "key_policy": {
            "policy_id": correlation.policy_id,
            "targets": dict(sorted(POLICY_TARGETS.items())),
        },
        "api_key": {"key_id": correlation.key_id, "label": KEY_LABEL, "value": "（不落这份文件）"},
        "knowledge_base": {
            "kb_id": correlation.kb_id,
            "name": REFERENCE_KB_NAME,
            "split_strategy": "structure",
            "documents": list(correlation.documents),
        },
        "retrieval_params": preset_row,
        "keyword_index": {
            "alias": keyword_alias,
            "note": (
                "评测专用别名（机制与生产相同：物理索引按版本隔离 <alias>__<version>、别名指向 "
                "active）。本机集群里的 rag_chunks 还是未迁移的物理索引，见模块文档。"
            ),
        },
        "corpus": corpus,
        "index_versions": {
            "ingest_version": INGEST_VERSION,
            "build_version": BUILD_VERSION,
            "rows": list(correlation.versions),
            "counts": {
                "chunks": dict(sorted(correlation.chunks_by_version.items())),
                "sections": dict(sorted(correlation.sections_by_version.items())),
                "edges": {
                    version: dict(sorted(counts.items()))
                    for version, counts in sorted(correlation.edges_by_version.items())
                },
            },
            "active": next(
                (name for name, row in versions.items() if row.get("status") == "active"), None
            ),
            "reference_edges": {
                version: reference_edge_count(counts)
                for version, counts in sorted(correlation.edges_by_version.items())
            },
            # 图谱撤销之后这一栏**恒为空**：留着键、并写明为什么空，别让读者把「不测了」
            # 读成「一条都没建出来」（#138 的范围变更）。
            "reference_edges_note": REJECTED_SCHEME_NOTE,
        },
    }


# ───────────────────────────────────────────────────────── ① 套餐（同步会话）


def _open_session() -> Any:
    """同步运维会话（惰性 import：模块导入时绝不建引擎）。"""

    from app.db.session import SessionLocal

    return SessionLocal()


async def _open_async_session() -> Any:
    from app.db.async_session import AsyncSessionLocal

    return AsyncSessionLocal()


def ensure_pro_plan(db: Any) -> dict[str, Any]:
    """把专业版那三列打开；返回这一档的完整读数（幂等，已达标时 ``changes`` 为空）。"""

    from sqlalchemy import select

    from app.models.plan import Plan, PlanSpec, plan_spec_of

    row = db.execute(select(Plan).where(Plan.code == PRO_PLAN_CODE)).scalar_one_or_none()
    if row is None:
        raise LookupError("plans 表里没有 pro 档：先跑 `uv run python scripts/seed_plans.py`")

    patch = plan_changes(row)
    for column, value in patch.changes.items():
        setattr(row, column, value)
    if patch.changed:
        db.commit()
        db.refresh(row)

    spec: PlanSpec = plan_spec_of(row)
    return {
        "code": spec.code,
        "retrieve_allowed": spec.retrieve_allowed,
        "rewrite_allowed": spec.rewrite_allowed,
        "decompose_allowed": spec.decompose_allowed,
        "research_allowed": spec.research_allowed,
        "rerank_allowed": spec.rerank_allowed,
        "retrieve_qps": spec.retrieve_qps,
        "retrieve_daily": spec.retrieve_daily,
        "rerank_qps": spec.rerank_qps,
        "rerank_daily": spec.rerank_daily,
        "llm_qps": spec.llm_qps,
        "llm_daily": spec.llm_daily,
        "max_kb": spec.max_kb,
        "changes": dict(sorted(patch.changes.items())),
    }


def ensure_quality_preset(db: Any) -> dict[str, Any]:
    """本组评测要的那套参数组合（第 13 步：没有档位表，直接就是三项参数）。

    图谱跟着重排走（ADR-0027 开关归属）：本对照需要 hybrid + 重排 + 5 条。
    返回值只进环境快照与打印文案，不对应任何库表行。
    """

    return {
        "mode": "hybrid",
        "top_k": 5,
        "rerank": True,
    }


def ensure_tenant(db: Any, *, tenant_id: str, name: str) -> dict[str, Any]:
    """开租户（在就复用）；返回租户与它那套默认配置的 id。"""

    from app.db.transaction import transaction
    from app.repositories import tenant_repository
    from app.services.tenant_service import create_tenant

    existing = tenant_repository.get_by_id(db, tenant_id)
    if existing is not None:
        return {"tenant_id": existing.id, "created": False, "plan_id": existing.plan_id}

    with transaction(db):
        created = create_tenant(
            db,
            tenant_id=tenant_id,
            name=name,
            plan_code=PRO_PLAN_CODE,
            # 数据范围留给下一步（库还没建）——空白名单表示「还没圈定」，不是「全部」。
            kb_ids=(),
        )
        # **在会话还活着时**抄成普通值：commit 之后行被 expire，close 再 detach，直接读字段
        # 会 ``DetachedInstanceError``（scripts/create_tenant.py 里踩过同一个坑）。
        report = {
            "tenant_id": created.tenant.id,
            "created": True,
            "plan_id": created.tenant.plan_id,
            "policy_id": created.policy.id,
        }
    return report


# ─────────────────────────────────────────────────────── ③ 知识库（异步会话）


async def ensure_kb(db: Any, *, tenant_id: str, plan: Any, name: str) -> dict[str, Any]:
    """建库（同租户同名就复用）；返回库 id 与切法。"""

    from sqlalchemy import select

    from app.models.knowledge_base import KnowledgeBase
    from app.services.knowledge_base_service import create_kb

    existing = (
        await db.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.tenant_id == tenant_id, KnowledgeBase.name == name
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {
            "kb_id": existing.id,
            "created": False,
            "split_strategy": existing.split_strategy,
            "active_index_version": existing.active_index_version,
        }

    created = await create_kb(
        db,
        name=name,
        description=REFERENCE_KB_DESCRIPTION,
        tenant_id=tenant_id,
        # 三篇都是 Markdown：structure 是这批语料唯一说得通的切法（按标题成节）。
        split_strategy="structure",
        plan=plan,
    )
    await db.commit()
    return {
        "kb_id": created["kb_id"],
        "created": True,
        "split_strategy": created["split_strategy"],
        "chunk_size": created["chunk_size"],
    }


async def ensure_policy(db: Any, *, tenant_id: str, kb_id: str, plan: Any) -> dict[str, Any]:
    """把该租户那套密钥配置改成全能力 + 三本账额度，并做一次自洽校验。"""

    from sqlalchemy import select

    from app.models.key_policy import KeyPolicy, policy_snapshot_of
    from app.services.profile_policy import ensure_policy_consistent

    row = (
        (
            await db.execute(
                select(KeyPolicy)
                .where(KeyPolicy.tenant_id == tenant_id)
                .order_by(KeyPolicy.created_at)
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        raise LookupError(f"租户 {tenant_id} 没有密钥配置：先跑 scripts/create_tenant.py")

    patch = policy_changes(row, kb_id=kb_id)
    for column, value in patch.changes.items():
        setattr(row, column, value)
    await db.commit()
    await db.refresh(row)

    # 配置时自洽校验（ADR-0018 第 2 条）：配置的位 ⊆ 套餐的位，数值不超过套餐额度。
    # 写坏了要在这里炸，而不是等到第一次调用才 ``20015``。
    ensure_policy_consistent(policy=policy_snapshot_of(row), plan=plan)
    return {
        "policy_id": row.id,
        "retrieval_mode": row.retrieval_mode,
        "changes": dict(sorted(patch.changes.items())),
        "allowed_kb_ids": list(row.allowed_kb_ids),
    }


# ──────────────────────────────────────────────── ④ 入库（生产路径：上传 → 索引）


class _NullPublisher:
    """把「投递索引任务」换成记账版：本脚本自己就地跑 Worker 的那一段。

    为什么可以这样：``enqueue_document`` 的唯一职责是把 ``document_id`` 交给 Broker。本地
    没有 Worker，投出去只会让那一行 ``PROCESSING`` 永远躺着；就地跑
    ``IndexingService.index_existing_document``（Worker 任务体的同一段）才是「索引真的发生了」。
    注入点就是既有那两个槽（``offline 套件`` 用的是同一条缝）。
    """

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    def apply_async(self, *, kwargs: dict[str, Any], task_id: str) -> None:
        self.published.append({"kwargs": dict(kwargs), "task_id": task_id})


async def _index_documents(db: Any, *, tenant_id: str, kb_id: str) -> list[dict[str, Any]]:
    """逐篇把已登记的文档索引成切片（就地跑 Worker 的那段），返回逐篇读数。"""

    from sqlalchemy import select

    from app.models.document import Document
    from app.services.indexing_service import IndexingService
    from app.utils.status import DocumentStatus

    rows = (
        (
            await db.execute(
                select(Document).where(Document.kb_id == kb_id, Document.tenant_id == tenant_id)
            )
        )
        .scalars()
        .all()
    )
    service = IndexingService()
    results: list[dict[str, Any]] = []
    for doc in rows:
        if doc.status == DocumentStatus.SUCCESS:
            results.append({"document_id": doc.id, "status": "already-success", "chunks": 0})
            continue
        chunks = await service.index_existing_document(db, doc.id)
        results.append({"document_id": doc.id, "status": "indexed", "chunks": chunks})
    return results


async def ensure_documents(
    db: Any, *, tenant_id: str, kb_id: str, plan: Any
) -> list[dict[str, Any]]:
    """把 ``eval/corpus-refs/`` 的三篇文档入库（已入库的同名文件不重复登记）。"""

    from sqlalchemy import select

    from app.db.transaction import async_transaction
    from app.models.document import Document
    from app.services import document_service
    from app.services.index_dispatch import set_task_publisher

    layout = json.loads(CORPUS_MANIFEST.read_text(encoding="utf-8"))
    publisher = _NullPublisher()
    set_task_publisher(publisher)
    registered: list[str] = []
    try:
        for meta in layout["documents"].values():
            filename = meta["file"]
            existing = (
                (
                    await db.execute(
                        select(Document).where(
                            Document.kb_id == kb_id, Document.source_filename == filename
                        )
                    )
                )
                .scalars()
                .first()
            )
            if existing is not None:
                continue
            data = (CORPUS_DIR / filename).read_bytes()
            async with async_transaction(db):
                await document_service.upload(
                    db,
                    tenant_id=tenant_id,
                    kb_id=kb_id,
                    plan=plan,
                    title=meta["title"],
                    filename=filename,
                    data=data,
                )
            registered.append(filename)
    finally:
        # 复原生产投递器：这一段之后任何一次上传都该真的去投递（本脚本不负责替别人吞任务）。
        set_task_publisher(None)

    indexed = await _index_documents(db, tenant_id=tenant_id, kb_id=kb_id)
    return [{"registered": registered, "published": len(publisher.published)}] + indexed


# ─────────────────────────────────────────────── ⑤ 构建（#142 的流水线）


async def ensure_build(db: Any, *, kb_id: str, tenant_id: str) -> dict[str, Any]:
    """建一次版本（已经建过就跳过），返回这一版的读数。"""

    from app.repositories import index_version_repository

    rows = await index_version_repository.list_for_kb(db, kb_id, tenant_id=tenant_id)
    if any(row.version == BUILD_VERSION for row in rows):
        current = next(row for row in rows if row.version == BUILD_VERSION)
        return {
            "created": False,
            "build_id": current.id,
            "version": current.version,
            "status": current.status,
        }

    row = await run_rebuild(
        db,
        kb_id=kb_id,
        tenant_id=tenant_id,
        # 版本名交给流水线算（它会先把 v1 收养成 active 行，再把这轮命名为 v2）。
        version=None,
        # 就地跑 + 顺带激活：本脚本跑在没有 Worker 的机器上，而「激活」本身是独立一步
        # （ADR-0025 决策三），这里只是把两步顺序跑完。
        activate=True,
    )
    return {
        "created": True,
        "build_id": row.id,
        "version": row.version,
        "status": row.status,
        "document_count": row.document_count,
        "chunk_count": row.chunk_count,
    }


# ─────────────────────────────────────────────────────────── 读数与快照


async def collect_correlation(
    db: Any, *, tenant_id: str, kb_id: str, policy_id: str, key_id: str
) -> Correlation:
    """把「本次环境到底装出了什么」取回来：版本行、逐版本切片数/节数/边数。

    **边数这一栏随图谱一起撤销了**（#138 的范围变更）：``section_edges`` 表已由迁移删除，
    关系边模型也不在了。这里因此**延迟**取那个模型，取不到就只跳过边数——环境准备本身
    （套餐 / 租户 / 库 / 入库 / 构建）照旧跑完，因为它是 Research 那一侧的实验前提，与
    图谱无关。跳过时打印一句指向 tag 的说明，不静默吞掉：读快照的人必须知道「0 条边」
    是「不测了」而不是「一条都没建出来」。
    """

    from sqlalchemy import func, select

    from app.models.chunk import Chunk
    from app.models.document import Document
    from app.repositories import index_version_repository

    section_edge: Any = None
    try:
        from app.models.section_edge import SectionEdge  # noqa: F401

        section_edge = SectionEdge
    except Exception:  # noqa: BLE001 - 撤销之后这就是常态，不必解释为什么
        print(REJECTED_SCHEME_NOTE)

    version_rows = await index_version_repository.list_for_kb(db, kb_id, tenant_id=tenant_id)
    versions = [
        {
            "version": row.version,
            "status": row.status,
            "document_count": row.document_count,
            "chunk_count": row.chunk_count,
        }
        for row in sorted(version_rows, key=lambda item: item.version)
    ]

    chunks_by_version: dict[str, int] = {}
    sections_by_version: dict[str, int] = {}
    edges_by_version: dict[str, dict[str, int]] = {}

    for row in version_rows:
        chunk_count = (
            await db.execute(
                select(func.count())
                .select_from(Chunk)
                .where(
                    Chunk.tenant_id == tenant_id,
                    Chunk.kb_id == kb_id,
                    Chunk.index_version == row.version,
                )
            )
        ).scalar_one()
        chunks_by_version[row.version] = int(chunk_count)

        # 节身份在 meta 里（``section_id``）：JSONB ->> 取出来再数不同的值。
        section_expr = Chunk.meta["section_id"].astext
        section_count = (
            await db.execute(
                select(func.count(func.distinct(section_expr))).where(
                    Chunk.tenant_id == tenant_id,
                    Chunk.kb_id == kb_id,
                    Chunk.index_version == row.version,
                )
            )
        ).scalar_one()
        sections_by_version[row.version] = int(section_count or 0)

        if section_edge is not None:
            edge_rows = (
                await db.execute(
                    select(section_edge.relation_type, func.count())
                    .where(
                        section_edge.tenant_id == tenant_id,
                        section_edge.kb_id == kb_id,
                        section_edge.index_version == row.version,
                    )
                    .group_by(section_edge.relation_type)
                )
            ).all()
            edges_by_version[row.version] = {str(kind): int(count) for kind, count in edge_rows}

    document_rows = (
        (
            await db.execute(
                select(Document.source_filename)
                .where(Document.kb_id == kb_id, Document.tenant_id == tenant_id)
                .order_by(Document.source_filename)
            )
        )
        .scalars()
        .all()
    )

    return Correlation(
        tenant_id=tenant_id,
        policy_id=policy_id,
        key_id=key_id,
        kb_id=kb_id,
        documents=[str(name) for name in document_rows if name],
        versions=versions,
        chunks_by_version=chunks_by_version,
        sections_by_version=sections_by_version,
        edges_by_version=edges_by_version,
    )


def ensure_api_key(db: Any, *, tenant_id: str, policy_id: str) -> dict[str, Any]:
    """签发一把 Key（同租户同配置已有 active 的就复用，不再多发一把）。"""

    from sqlalchemy import select

    from app.db.transaction import transaction
    from app.models.api_key import ApiKey
    from app.services.api_key_service import API_KEY_STATUS_ACTIVE, issue_api_key

    existing = (
        (
            db.execute(
                select(ApiKey)
                .where(
                    ApiKey.tenant_id == tenant_id,
                    ApiKey.policy_id == policy_id,
                    ApiKey.status == API_KEY_STATUS_ACTIVE,
                )
                .order_by(ApiKey.created_at)
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return {"key_id": existing.id, "key_value": existing.key_value, "created": False}

    with transaction(db):
        issued = issue_api_key(db, tenant_id=tenant_id, policy_id=policy_id, label=KEY_LABEL)
        report = {"key_id": issued.id, "key_value": issued.key_value, "created": True}
    return report


def _display(path: Path) -> str:
    """给人看的路径：仓库内用相对路径，仓库外（用例的 tmp_path）原样打印。"""

    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def write_state(
    *,
    correlation: Correlation,
    snapshot: dict[str, Any],
    key_value: str,
    keyword_alias: str = DEFAULT_KEYWORD_ALIAS,
) -> None:
    """落两份产物：可提交的环境快照 + gitignore 的本机状态。"""

    ENVIRONMENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENVIRONMENT_FILE.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    LOCAL_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_STATE_FILE.write_text(
        json.dumps(
            {
                **correlation.as_dict(),
                "key_value": key_value,
                "keyword_alias": keyword_alias,
                "environment_file": _display(ENVIRONMENT_FILE),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


# ────────────────────────────────────────────────────────────── 编排


@dataclass(frozen=True)
class _PrepStats:
    """``print_summary`` 要的那几行读数（与 ``_ops_common`` 的既有口径一致）。"""

    versions: str
    chunks: str
    sections: str
    reference_edges: str


async def _prepare(
    *,
    tenant_id: str,
    tenant_name: str,
    kb_name: str,
    keyword_alias: str,
    skip_documents: bool,
    skip_build: bool,
    preset_row: dict[str, Any],
    plan_row: dict[str, Any],
) -> int:
    from app.core.config import settings
    from app.services.plan_service import resolve_plan

    # 关键词别名**在写第一片之前**钉死：物理索引名、写入目标、激活时的别名切换、检索时
    # 查的那个名字，四处都从 ``settings.ELASTICSEARCH_INDEX`` 派生。晚一步钉，前面的写入
    # 就落在另一个别名派生出的物理索引里（不报错，只是搜不到）。
    settings.ELASTICSEARCH_INDEX = keyword_alias
    print(f"keyword   : alias={keyword_alias}（物理索引 {keyword_alias}__<版本>）")

    db = await _open_async_session()
    try:
        tenant_plan = await resolve_plan(db, await _plan_id_of(db, tenant_id))
        kb = await ensure_kb(db, tenant_id=tenant_id, plan=tenant_plan, name=kb_name)
        print(
            f"kb        : {kb['kb_id']}（{'新建' if kb['created'] else '复用'}，{kb['split_strategy']}）"
        )

        policy = await ensure_policy(db, tenant_id=tenant_id, kb_id=kb["kb_id"], plan=tenant_plan)
        print(
            f"policy    : {policy['policy_id']} mode={policy['retrieval_mode']} "
            f"kb_ids={policy['allowed_kb_ids']} changes={policy['changes'] or '（已达标）'}"
        )

        if skip_documents:
            print("documents : 跳过（--skip-documents）")
        else:
            for item in await ensure_documents(
                db, tenant_id=tenant_id, kb_id=kb["kb_id"], plan=tenant_plan
            ):
                print(f"documents : {item}")

        if skip_build:
            print("build     : 跳过（--skip-build）")
        else:
            build = await ensure_build(db, kb_id=kb["kb_id"], tenant_id=tenant_id)
            print(f"build     : {build}")
    finally:
        await db.close()

    session = _open_session()
    try:
        key = ensure_api_key(session, tenant_id=tenant_id, policy_id=policy["policy_id"])
    finally:
        session.close()

    db = await _open_async_session()
    try:
        correlation = await collect_correlation(
            db,
            tenant_id=tenant_id,
            kb_id=kb["kb_id"],
            policy_id=policy["policy_id"],
            key_id=key["key_id"],
        )
    finally:
        await db.close()

    corpus = json.loads(CORPUS_MANIFEST.read_text(encoding="utf-8"))
    snapshot = environment_snapshot(
        correlation=correlation,
        plan_row=plan_row,
        preset_row=preset_row,
        keyword_alias=keyword_alias,
        corpus={
            "dir": "eval/corpus-refs",
            "documents": sorted(corpus["documents"]),
            "reference_inventory": len(corpus["reference_inventory"]),
        },
    )
    write_state(
        correlation=correlation,
        snapshot=snapshot,
        key_value=key["key_value"],
        keyword_alias=keyword_alias,
    )

    print("")
    print(f"api key   : {key['key_value']}")
    print(f"snapshot  : {_display(ENVIRONMENT_FILE)}（不含 Key 原文，可提交）")
    print(f"local     : {_display(LOCAL_STATE_FILE)}（含 Key 原文，已 gitignore）")
    print(
        "next      : 跑 eval_refs 时带上同一套环境变量："
        f"`$env:ELASTICSEARCH_INDEX='{keyword_alias}'`（缺了它会查另一个别名派生出的索引）"
    )
    print("")
    expected = len(corpus["reference_inventory"])
    print_summary(
        f"prep_eval_c6: {tenant_id} / {kb['kb_id']}",
        stats=_PrepStats(
            versions=" ".join(f"{row['version']}:{row['status']}" for row in correlation.versions),
            chunks=" ".join(
                f"{version}:{count}"
                for version, count in sorted(correlation.chunks_by_version.items())
            ),
            sections=" ".join(
                f"{version}:{count}"
                for version, count in sorted(correlation.sections_by_version.items())
            ),
            reference_edges=" ".join(
                f"{version}:{reference_edge_count(counts)}/{expected}"
                for version, counts in sorted(correlation.edges_by_version.items())
            ),
        ),
        counters=(
            ("versions", "versions"),
            ("chunks", "chunks"),
            ("sections", "sections"),
            ("reference edges (found/expected)", "reference_edges"),
        ),
        note=(
            "NOTE: v1 是逐篇摄入的产物，v2 是 #142 流水线原样重建的结果——两边逐节身份相同、"
            "切片数相同，差别只在「谁建的」。引用边那一栏随图谱撤销（#138）恒为空，"
            f"要复现当年的读数回到 tag `{REJECTED_SCHEME_TAG}`。切版本用 "
            "`uv run python scripts/rebuild_kb_version.py --kb-id <kb> --rollback v1`。"
        ),
    )
    return 0


async def _plan_id_of(db: Any, tenant_id: str) -> int:
    """读该租户的 ``plan_id``（套餐在租户行上，检索那一路也是这么解析的）。"""

    from app.models.tenant import Tenant

    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise LookupError(f"租户不存在：{tenant_id}")
    return int(tenant.plan_id)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare the C6 (#153) eval environment: pro plan capabilities, a dedicated "
            "tenant + key policy + API key, a standalone knowledge base, the reference "
            "corpus ingested through the production path, and one v2 build on top of it."
        )
    )
    parser.add_argument("--tenant-id", default="tenant_c6", help="dedicated tenant id")
    parser.add_argument("--tenant-name", default="C6 验收租户", help="tenant display name")
    parser.add_argument("--kb-name", default=REFERENCE_KB_NAME, help="knowledge base name")
    parser.add_argument(
        "--keyword-alias",
        default=DEFAULT_KEYWORD_ALIAS,
        help=(
            "keyword alias for this eval set (physical indices are <alias>__<version>); "
            "defaults to a dedicated alias so the not-yet-migrated rag_chunks index is untouched"
        ),
    )
    parser.add_argument(
        "--skip-documents", action="store_true", help="do not upload/index the corpus (debug)"
    )
    parser.add_argument(
        "--skip-build", action="store_true", help="do not run the index build (debug)"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：解析参数 → 一次 ``setup_logging()`` → 同步段 → 异步段 → 落快照。"""

    args = _build_parser().parse_args(argv)
    setup_logging()
    if sys.platform == "win32":
        # psycopg 的异步连接在 Windows 的缺省 Proactor 循环上跑不起来（第一条查询就
        # InterfaceError），与 scripts/rebuild_kb_version.py 同一个理由。
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    session = _open_session()
    try:
        plan_row = ensure_pro_plan(session)
        preset_row = ensure_quality_preset(session)
        tenant = ensure_tenant(session, tenant_id=args.tenant_id, name=args.tenant_name)
    except (LookupError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        session.close()

    print(f"plan      : {plan_row['code']} changes={plan_row['changes'] or '（已达标）'}")
    print(f"params    : {preset_row}")
    print(f"tenant    : {tenant['tenant_id']}（{'新建' if tenant['created'] else '复用'}）")

    try:
        return asyncio.run(
            _prepare(
                tenant_id=tenant["tenant_id"],
                tenant_name=args.tenant_name,
                kb_name=args.kb_name,
                keyword_alias=args.keyword_alias,
                skip_documents=args.skip_documents,
                skip_build=args.skip_build,
                preset_row=preset_row,
                plan_row=plan_row,
            )
        )
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - 运维脚本：把类型打出来，别吞掉
        print(f"prepare failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise


if __name__ == "__main__":
    raise SystemExit(main())

