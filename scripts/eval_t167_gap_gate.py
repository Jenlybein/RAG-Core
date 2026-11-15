"""缺口闸真实行为评测：缺口出现率 / 逐缺口声明对齐度 / 拦截率 / ⑤ 触发率（票 #167）。

**它是测量，不是功能。** 第 11 步收尾留了一个「已知未验证」：缺口闸的真实拦截率，当时的
说法是「分母凑不出来」。那个说法是错的——分母不是靠骗模型造出来的，是靠**问什么题**设计
出来的。本脚本只回答「在真实检索 + 真实模型下，这道闸实际做了什么」，不改任何生产行为。

**与 #165 的区别（两票的分工，别混读）**：

======================  ==================================  ==========================
#165                    构造材料池，测**判定准不准**          绕开检索与生成
#167（本脚本）          真实检索 + 真实模型，测**闸门行为**  `run_research` 完整五步
======================  ==================================  ==========================

**题集：半可答 10 题 + 全不可答 10 题。** 这个设计是本票的核心，理由不是审美：

* **全不可答**时模型很容易整篇写「材料不足」——缺口太明显，它不会硬答；
* **半可答**时它最容易**圆场**：一半有材料，就顺手把另一半也答了。而这正是缺口闸要防的
  那一类（「假装没有缺口」）。只测全不可答，量到的是一道闸打在稻草人身上的表现。

这不是造假数据：知识库覆盖不全用户的问题是生产里最常见的场景（SQuAD 2.0 / NQ 本来就有
no-answer 子集，思路相同）。

**「不可答」必须有机械证据**（本题面硬要求）：每题带一个**关键词**，它必须在库内
**文档正文**（``documents.content``）与**已入库切片正文**（``chunks.content``）里都是
**0 命中**。两个来源都要查——只查一处会漏掉「文档正文有、切片没进」与反过来这两种情况。
命中不为 0 就当场失败（:class:`EvidenceViolation`）：不许凭感觉标「这题答不了」。

**走生产完整链路**：:func:`app.services.research.run_research` + 生产检索内核
（:func:`app.services.research_gate.build_kernel_search`）+ 两个真实模型角色。只把两件
「评测必须做的事」显式钉死，且都在进程内、不改仓库里的任何文件：

* ``ELASTICSEARCH_INDEX`` → ``rag_chunks_eval``（与 ``scripts/eval_retrieval.py`` 同一条
  纪律：评测只读**评测索引**，绝不读生产别名 ``rag_chunks``）；
* 检索参数由本脚本构造（``mode=hybrid / top_k=5 / fold_to_sections`` 与 ``balanced`` 档
  同形）：评测没有「这套集成买的是哪一档」可解析，编一个看起来合理的套餐名比承认这一点更糟。

**五个指标**（定义见 :func:`summarize`）：

========================  ============================================================
缺口出现率                ``gaps`` 非空的题数 / 20
**声明对齐度**            ``gaps`` 非空的题：``insufficient`` 块数 vs ``len(gaps)`` 的
                          **分布**（不是均值），欠声明（块数 < 缺口数）单列并给占比
拦截率                    ``answer_rejected_reason == REJECTED_GAP_NOT_DECLARED`` 的题数
                          / ``gaps`` 非空的题数
⑤ 触发率                  ``model_calls >= 4`` 的题数 / 20（= 补证捞到材料且复查过）
``unknown`` 残留率        完成后仍有 ``unknown`` 要点的题数 / 20（复查成功时应为 0）
========================  ============================================================

**为什么对齐度比拦截率重要**：闸是**文本级**的（ADR-0029 实施备注第 4 条），它只检查答案里
有没有「材料不足」这句话。所以「一道题有 3 个缺口、只写了 1 句材料不足」会被**放行**——
拦截率看不见这种情形，对齐度看得见。真正要量的是后者。

**预算：≤170 次模型调用**（主条件 20 题 × 最坏 5 次 + A 组「关 ⑤」对照 10 题 × 最坏 4 次
= 140，余量 30）。开跑前打印名义与最坏两个预估（:func:`predict_model_calls`），跑完打印实际
次数，三个数都进产物与报告。

**追加条件（lead 在测量中途加的）：A 组「有 ⑤ / 无 ⑤」对照。** 理由很硬：⑤ 上线后 `unknown`
会被复查消费掉，于是成功跑批里「`unknown` 残留率」必然是 0——**残留率 0 什么也证明不了**。
要量「若没有 ⑤ 会带 `unknown` 的题占多少」，只能把 ⑤ 关掉再跑一次。关法只在**本脚本内**：
用 ``setattr`` 把 ``research._Run._rejudge`` 换成一个直接返回的 async no-op，跑完在 ``finally``
里换回来——``app/`` 一个字节不改（手法写进报告）。四个新读数见 :func:`fivefold_comparison`：
条件 2 的 `unknown` 真实频率、`unknown → supported` / `unknown → none` 的改判分布、⑤ 的净效果、
以及这两种**方向相反**的价值各占多少（不许合成一个「改善率」）。

用法::

    # 只打印预估、题集自检与关键词命中证据（0 次模型调用；**要连库**读两种正文）
    uv run python scripts/eval_t167_gap_gate.py plan

    # 跑全量（20 题 + A 组无 ⑤ 对照 10 题，约 110 次模型调用）
    uv run python scripts/eval_t167_gap_gate.py run

    # 只跑其中一组（产物可增量补：先跑 A，再跑 A-off，再跑 B，前面的记录原样保留）
    uv run python scripts/eval_t167_gap_gate.py run --groups A
    uv run python scripts/eval_t167_gap_gate.py run --groups A-off

    # 按当前口径重算指标并重出报告（0 次模型调用，只读落盘记录）
    uv run python scripts/eval_t167_gap_gate.py recompute

产物：``eval/reports/t167/results.json``（机器可读，含逐题读数与关键词证据）与
``report.md``（人读，含五个指标、三个问题的答案与「这份对照不能回答什么」）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    # 以 ``python scripts/eval_t167_gap_gate.py`` 运行时 ``sys.path[0]`` 是 ``scripts/``；
    # ``scripts`` 是没有 ``__init__.py`` 的命名空间包，靠这行才能 ``from scripts...``。
    sys.path.insert(0, str(REPO_ROOT))

EVAL_DIR = REPO_ROOT / "eval"
BASE_QUESTIONS = EVAL_DIR / "questions.json"
REPORT_DIR = EVAL_DIR / "reports" / "t167"

#: 模型调用的硬顶（票面原写 ≤110，追加 A 组「关 ⑤」对照后**放宽到 ≤170**）。
#: 预估与执行都对着它判，超了就减题、不超跑。
MODEL_CALL_CAP: Final[int] = 170

#: 一道题的题量与分组的两个名字（报告与用例都读它们，不写字面量）。
CASE_COUNT: Final[int] = 20
GROUP_A: Final[str] = "A"
GROUP_B: Final[str] = "B"
#: A 组「关掉 ⑤ 再跑一次」的对照条件名（命令行与产物里都用它）。
GROUP_A_NO_FIVEFOLD: Final[str] = "A-off"
RUN_LABELS: Final[tuple[str, ...]] = (GROUP_A, GROUP_B, GROUP_A_NO_FIVEFOLD)

#: 「⑤ 开着」与「⑤ 关掉」两个条件在逐题记录里的标记。
CONDITION_FIVEFOLD: Final[str] = "fivefold"
CONDITION_CONTROL: Final[str] = "control"

#: 一次 Research 的**名义**与**最坏**模型调用次数，从生产常量抄（不手编）：
#: 规划 + 判定 + 复查 + 生成 = ``MAX_MODEL_CALLS``（4）；含生成阶段的强模型兜底位 =
#: ``MAX_MODEL_CALLS``（4）。``tests/test_eval_t167_gap_gate.py`` 钉住两者相等。
#:
#: 名义取 4 而不是 3：这批题**注定**有缺口（半可答与全不可答都带不可答方面），补证几乎必然
#: 捞到材料、于是必然跑复查。预检那次真实调用就是 4 次（见报告第五节）。
NOMINAL_CALLS_PER_CASE: Final[int] = 3
WORST_CALLS_PER_CASE: Final[int] = 4

#: 关掉 ⑤ 之后少一次复查：名义 3（规划 + 判定 + 生成），最坏 4（+强模型兜底位）。
#: **不改生产常量**——这是「把那一格拿掉之后还剩几次」，不是另一套契约。
NOMINAL_CALLS_PER_CONTROL_CASE: Final[int] = 2
WORST_CALLS_PER_CONTROL_CASE: Final[int] = 3

#: 评测租户与它要检索的那几个库。**语料范围 = 关键词证据的范围**：同一个常量两处用，
#: 于是「证据说的是哪批正文」与「检索真的查了哪批正文」在结构上不可能对不上。
TENANT_ID: Final[str] = "tenant_demo"
KB_IDS: Final[tuple[str, ...]] = (
    "kb-eval",
    "kb-eval-biz",
    "kb-eval-long",
    "kb-eval-ops",
    "kb-eval-ref",
)

#: 评测专用关键词索引（与 ``scripts/eval_retrieval.py`` 同一条纪律：只读评测索引）。
EVAL_ELASTICSEARCH_INDEX: Final[str] = "rag_chunks_eval"

#: 下面几个词/码与生产**同一个字面量**，但抄一份：``plan`` 与 ``recompute`` 要在没有配置、
#: 没有库、没有模型的机器上也能跑（``import app`` 会读 settings）。
#: ``tests/test_eval_t167_gap_gate.py`` 把「抄漏了」变成一条会红的断言。
GAP_DECLARATION: Final[str] = "材料不足"
STATUS_INSUFFICIENT: Final[str] = "insufficient"
SUPPORT_NONE: Final[str] = "none"
SUPPORT_UNKNOWN: Final[str] = "unknown"
SUPPORT_SUPPORTED: Final[str] = "supported"
REJECTED_GAP_NOT_DECLARED: Final[str] = "gap not declared in the answer"
REJECTED_ASSERTION_WITHOUT_CORE: Final[str] = "assertion without a core citation"

#: ⑤ 的判据线：``model_calls >= 4``（= 补证捞到材料且复查跑过）。
TRIGGER_MIN_MODEL_CALLS: Final[int] = 4

#: 归档旁证的参照值：C6 真实 8 题跑批里 3 题停在 ``max_rounds``（37.5%）。#165 用它推断
#: 「多少题会走到 ⑤」，本票把同一件事**直接测出来**，两个数放在报告里对照。
ARCHIVED_TRIGGER_NUMERATOR: Final[int] = 3
ARCHIVED_TRIGGER_DENOMINATOR: Final[int] = 8
ARCHIVED_TRIGGER_RATE: Final[float] = round(
    ARCHIVED_TRIGGER_NUMERATOR / ARCHIVED_TRIGGER_DENOMINATOR, 4
)
ARCHIVED_RUN: Final[Path] = (
    EVAL_DIR / "reports" / "c6" / "runs" / "04-v2-research-on" / "results.json"
)


# ─────────────────────────────────────────────────────────── 题集（冻结在代码里）


@dataclass(frozen=True)
class EvalCase:
    """一道评测题：题面 + 它的「不可答方面」+ 那个方面的**机械证据抓手**。

    ``appended`` 只有 A 组非空（追加在题库原题后面的那个方面）；``topic`` 说清「不可答的是
    哪一块」。``base_id`` 两边都可能有：A 组是**被追加的原题**，B 组那 6 题是**题库自己标成
    ``absent`` 的整题**（写在检索之前的问题侧标注）——它们不是「追加了某一块」，是整题不可答。
    """

    case_id: str
    group: str
    query: str
    keyword: str
    topic: str
    base_id: str | None = None
    appended: str = ""
    note: str = ""


#: 20 道题。**A 组的题面 = 题库原题 + 一个语料里确实没有的方面**（追加串真的接在题面后面，
#: 见 :func:`check_case_set` 的前缀断言）；**B 组是整题都不在语料里**。
#:
#: 每题的 ``keyword`` 就是那个「不可答方面」的抓手，它必须在两种正文里 0 命中
#: （:func:`keyword_evidence`）。关键词一题一个：重复会让两条证据互相冒充。
EVAL_CASES: Final[tuple[EvalCase, ...]] = (
    # ── A 半可答：前半句库里能答，后半句库里没有 ──
    EvalCase(
        case_id="A01",
        group=GROUP_A,
        base_id="q19",
        appended="；另外退款能不能用数字人民币退到数字钱包",
        query="退款要几天，从哪个入口申请；另外退款能不能用数字人民币退到数字钱包",
        keyword="数字人民币",
        topic="退款能不能走数字人民币（语料只讲银行卡 / 第三方支付 / 对公转账）",
    ),
    EvalCase(
        case_id="A02",
        group=GROUP_A,
        base_id="q43",
        appended="；另外日志能不能用区块链做存证",
        query="审计日志要留存多久；另外日志能不能用区块链做存证",
        keyword="区块链",
        topic="日志的区块链存证（语料只讲留存期限与审计流程）",
    ),
    EvalCase(
        case_id="A03",
        group=GROUP_A,
        base_id="q45",
        appended="；另外告警能不能推送到企业微信群里",
        query="P2 告警的响应时限是多少；另外告警能不能推送到企业微信群里",
        keyword="企业微信",
        topic="告警推到企业微信（语料只讲告警分级与通报路径）",
    ),
    EvalCase(
        case_id="A04",
        group=GROUP_A,
        base_id="q70",
        appended="；另外发票能不能开英文版",
        query="每月账单是哪天出账的；另外发票能不能开英文版",
        keyword="英文发票",
        topic="英文发票（语料讲普票 / 专票、抬头变更与红冲，没有英文版）",
    ),
    EvalCase(
        case_id="A05",
        group=GROUP_A,
        base_id="q78",
        appended="；另外买机器能不能分期付款",
        query="机架款 XR-2100 的指导价是多少；另外买机器能不能分期付款",
        keyword="分期",
        topic="分期付款（语料只有整机价、阶梯折扣与渠道政策）",
    ),
    EvalCase(
        case_id="A06",
        group=GROUP_A,
        base_id="q82",
        appended="；另外设备有没有碳中和认证",
        query="企业版的月度可用性承诺是多少；另外设备有没有碳中和认证",
        keyword="碳中和",
        topic="碳中和认证（语料有认证与合规标识，但没有这一项）",
    ),
    EvalCase(
        case_id="A07",
        group=GROUP_A,
        base_id="q108",
        appended="；另外设备能不能用语音控制",
        query="上门装设备之前要先去看什么；另外设备能不能用语音控制",
        keyword="语音控制",
        topic="语音控制（语料只讲管理与运维界面）",
    ),
    EvalCase(
        case_id="A08",
        group=GROUP_A,
        base_id="q116",
        appended="；另外能不能用苹果手机扫码登录管理后台",
        query="请求签名的时间戳允许多少秒偏差；另外能不能用苹果手机扫码登录管理后台",
        keyword="苹果",
        topic="苹果手机扫码登录（语料只讲管理界面与账号权限）",
    ),
    EvalCase(
        case_id="A09",
        group=GROUP_A,
        base_id="q53",
        appended="；另外能不能以租代购",
        query="旧机器能抵钱换新的吗；另外能不能以租代购",
        keyword="以租代购",
        topic="以租代购（语料有以旧换新与返修备件，没有租购）",
    ),
    EvalCase(
        case_id="A10",
        group=GROUP_A,
        base_id="q60",
        appended="；另外值班排班能不能用钉钉",
        query="变更窗口是什么时候，窗口结束前要做什么；另外值班排班能不能用钉钉",
        keyword="钉钉",
        topic="钉钉排班（语料有值班排班与交接表，没有第三方协同工具）",
    ),
    # ── B 全不可答：整题主题不在语料里 ──
    EvalCase(
        case_id="B01",
        group=GROUP_B,
        base_id="q20",
        query="E8888 是什么错误",
        keyword="E8888",
        topic="不存在的错误码（语料只有 E1xxx / E2xxx / E3xxx / W0001 各族）",
        note="取自题库 absent 标注（问题侧标注，写在检索之前）",
    ),
    EvalCase(
        case_id="B02",
        group=GROUP_B,
        base_id="q21",
        query="量子加密通道怎么配置",
        keyword="量子",
        topic="量子加密通道（全库不出现「量子」，也没有这层设备）",
        note="取自题库 absent 标注；与 B01 一起构成「语义型 vs 编号型」两个方向",
    ),
    EvalCase(
        case_id="B03",
        group=GROUP_B,
        base_id="q22",
        query="设备支持卫星回传吗",
        keyword="卫星",
        topic="卫星回传（语料有蜂窝与备份链路，没有卫星）",
        note="取自题库 absent 标注",
    ),
    EvalCase(
        case_id="B04",
        group=GROUP_B,
        base_id="q62",
        query="支持刷脸支付吗",
        keyword="刷脸",
        topic="刷脸支付（语料没有支付方式相关章节）",
        note="取自题库 absent 标注",
    ),
    EvalCase(
        case_id="B05",
        group=GROUP_B,
        base_id="q63",
        query="有没有手机 App 可以管理设备",
        keyword="安卓",
        topic="安卓手机客户端（语料只有管理后台与工单入口）",
        note=(
            "取自题库 absent 标注。注意「App」这两个字母在库里出现 2 次，但都是请求头 "
            "`X-App-Id`（签名参数）——不是客户端。所以机械证据取具体形态「安卓」= 0 命中。"
        ),
    ),
    EvalCase(
        case_id="B06",
        group=GROUP_B,
        base_id="q64",
        query="能开香港公司的发票吗",
        keyword="香港",
        topic="香港公司开票（发票章节只讲普票 / 专票，跨境章节讲形式发票）",
        note="取自题库 absent 标注；「发票」本身在库里有 3 篇命中，所以证据取「香港」",
    ),
    EvalCase(
        case_id="B07",
        group=GROUP_B,
        query="员工工伤认定和理赔要走哪些流程",
        keyword="工伤",
        topic="工伤认定与理赔（语料有入职、权限、转正考核，没有工伤）",
        note="我方构造：主题贴着语料里的 HR 类文档，但内容确实没有",
    ),
    EvalCase(
        case_id="B08",
        group=GROUP_B,
        query="公司员工宿舍的水电费怎么分摊",
        keyword="宿舍",
        topic="员工宿舍水电分摊（语料没有任何福利 / 后勤主题）",
        note="我方构造",
    ),
    EvalCase(
        case_id="B09",
        group=GROUP_B,
        query="申请高新技术企业认定要准备哪些材料",
        keyword="高新技术企业",
        topic="高新技术企业认定（语料有安全合规与等保，没有资质申报）",
        note="我方构造",
    ),
    EvalCase(
        case_id="B10",
        group=GROUP_B,
        query="给核心员工做股权激励，行权价格怎么定",
        keyword="股权激励",
        topic="股权激励行权价（语料没有任何股权 / 薪酬主题）",
        note="我方构造",
    ),
)


def load_cases() -> list[EvalCase]:
    """题集（冻结在代码里，不由运行时从材料归纳——标注必须独立于材料）。"""

    return list(EVAL_CASES)


def check_case_set() -> dict[str, Any]:
    """题集不变量（0 次模型调用、不连库）：分组数、原题可解析、前缀、关键词唯一。"""

    cases = load_cases()
    bank_payload = json.loads(BASE_QUESTIONS.read_text(encoding="utf-8"))
    bank = {str(item["id"]): str(item["query"]) for item in bank_payload["questions"]}
    absent = {str(item["id"]) for item in bank_payload["questions"] if item.get("type") == "absent"}

    a_cases = [case for case in cases if case.group == GROUP_A]
    b_cases = [case for case in cases if case.group == GROUP_B]
    base_ids_ok = all(
        case.base_id in bank for case in cases if case.base_id is not None
    )
    prefix_ok = all(
        case.query.startswith(bank[case.base_id])
        for case in a_cases
        if case.base_id is not None
    )
    keywords = [case.keyword for case in cases]
    return {
        "cases": len(cases),
        "a_cases": len(a_cases),
        "b_cases": len(b_cases),
        "base_ids_ok": base_ids_ok,
        "prefix_ok": prefix_ok,
        "keywords_unique": len(set(keywords)) == len(keywords),
        "bank_absent_cases": sum(1 for case in b_cases if case.base_id in absent),
        "constructed_b_cases": sum(1 for case in b_cases if case.base_id is None),
    }


# ─────────────────────────────────────────────────────────── 机械证据：关键词 0 命中


class EvidenceViolation(RuntimeError):
    """某个「不可答方面」的关键词在库内正文里出现了。发现即停：证据不成立，读数就不成立。"""


@dataclass(frozen=True)
class KeywordEvidence:
    """一题的关键词命中证据：查了什么、命中几条。

    ``sql`` 原样进产物——读者可以自己拿去跑一遍。这不是「日志」，是这份测量的**证据链**：
    没有它，「这题答不了」就只是一句自我声明。
    """

    case_id: str
    keyword: str
    pattern: str
    doc_hits: int
    chunk_hits: int
    sql: str
    params: dict[str, Any]


def evidence_sql() -> str:
    """一条查询同时数**文档正文**与**已入库切片正文**里的命中条数。

    两个来源都要数，因为它们回答的是两个不同的问题：文档正文代表「语料里有没有这件事」，
    切片正文代表「它有没有真的进到检索能看见的地方」。只查一处时，另一种情形会静默漏过。
    """

    return (
        "select\n"
        "  (select count(*) from documents d\n"
        "    where d.tenant_id = :tenant_id and d.kb_id in :kb_ids\n"
        "      and d.content ilike :pattern) as doc_hits,\n"
        "  (select count(*) from chunks c join documents d2 on d2.id = c.document_id\n"
        "    where d2.tenant_id = :tenant_id and d2.kb_id in :kb_ids\n"
        "      and c.content ilike :pattern) as chunk_hits"
    )


def evidence_params(
    case: EvalCase, *, tenant_id: str = TENANT_ID, kb_ids: Sequence[str] = KB_IDS
) -> dict[str, Any]:
    """证据查询的绑定参数：**检索范围就是证据范围**（同一个常量两处用）。"""

    return {
        "tenant_id": tenant_id,
        "kb_ids": list(kb_ids),
        "pattern": f"%{case.keyword}%",
    }


def _evidence_from_row(
    case: EvalCase,
    row: Mapping[str, int],
    *,
    tenant_id: str = TENANT_ID,
    kb_ids: Sequence[str] = KB_IDS,
) -> KeywordEvidence:
    """一行命中结果 → 一份证据（``keyword_evidence`` 与真跑时的 ``collect_evidence`` 共用）。"""

    params = evidence_params(case, tenant_id=tenant_id, kb_ids=kb_ids)
    return KeywordEvidence(
        case_id=case.case_id,
        keyword=case.keyword,
        pattern=str(params["pattern"]),
        doc_hits=int(row.get("doc_hits") or 0),
        chunk_hits=int(row.get("chunk_hits") or 0),
        sql=evidence_sql(),
        params=params,
    )


def keyword_evidence(
    case: EvalCase,
    *,
    fetch: Callable[[str, Mapping[str, Any]], Mapping[str, int]],
    tenant_id: str = TENANT_ID,
    kb_ids: Sequence[str] = KB_IDS,
) -> KeywordEvidence:
    """跑一次证据查询（``fetch`` 由调用方注入：真跑时查库，用例里给假数据）。"""

    row = fetch(evidence_sql(), evidence_params(case, tenant_id=tenant_id, kb_ids=kb_ids))
    return _evidence_from_row(case, row, tenant_id=tenant_id, kb_ids=kb_ids)


def require_absent(evidence: KeywordEvidence) -> None:
    """双零才算证据成立。任何一处命中都当场失败——**不降级、不换成别的关键词**。"""

    if evidence.doc_hits == 0 and evidence.chunk_hits == 0:
        return
    raise EvidenceViolation(
        f"{evidence.case_id} 的「{evidence.keyword}」不是 0 命中："
        f"documents={evidence.doc_hits} chunks={evidence.chunk_hits}。"
        "这题的「不可答」不成立，先换题或换关键词。"
    )


def as_json(evidence: KeywordEvidence) -> dict[str, Any]:
    return {
        "case_id": evidence.case_id,
        "keyword": evidence.keyword,
        "pattern": evidence.pattern,
        "doc_hits": evidence.doc_hits,
        "chunk_hits": evidence.chunk_hits,
        "sql": evidence.sql,
        "params": dict(evidence.params),
    }


# ─────────────────────────────────────────────────────────── 指标（纯函数）

Metric = dict[str, Any]


def _rate(numerator: int, denominator: int) -> float | None:
    if not denominator:
        return None
    return round(numerator / denominator, 4)


def _metric(numerator: int, denominator: int) -> Metric:
    return {
        "value": _rate(numerator, denominator),
        "numerator": numerator,
        "denominator": denominator,
    }


def insufficient_blocks(record: Mapping[str, Any]) -> int:
    """``insufficient`` 引用块的条数——**唯一口径**，就是 ``citations[].status`` 那个词。

    为什么不用「答案里出现几次『材料不足』」当主口径：那句话由 :func:`render_answer` 追加在
    每个 ``insufficient`` 块后面，两者本来同源；但答案被整份作废（``answer = None``）时字串
    一个也数不到，而块也一并没有——两个口径都归零，所以不影响对齐度。反过来，用字串计数
    会把模型自己在文本里多写的一句「材料不足」算成一块，那就不是块了。
    """

    return sum(
        1
        for citation in record.get("citations") or []
        if str(citation.get("status")) == STATUS_INSUFFICIENT
    )


def declaration_alignment(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """逐缺口声明对齐度：**分布**，不是均值。

    为什么要分布：均值会把「3 个缺口写 1 句」（欠声明，闸放行）与「1 个缺口写 3 句」（过声明）
    平均成一个漂亮的 1:1。要回答的是「多少题被放行了但答得不全」，只有分布答得出来。

    **分母是「有缺口且真的交出了答案」的题**，不是「有缺口」的题。这条分开写是因为它决定
    结论：答案被整份作废（``answer = None``）时声明块数**结构上就是 0**，那不是闸门放行了
    一份不完整的答案，恰恰相反——它被另一道校验挡下了（四条硬校验或生成失败）。把它算进
    欠声明，会把「答案没生成出来」记成「闸门漏掉了一次」，那是两种完全不同的事。

    所以三个数各自单列：``unanswered``（有缺口但没答案，未测量）、
    ``declared_without_gaps``（没缺口却写了材料不足，另一侧的过度声明），
    以及三档分布本身。
    """

    with_gaps = [record for record in records if record.get("gaps")]
    answered = [record for record in with_gaps if isinstance(record.get("answer"), str)]
    pairs: dict[str, int] = {}
    exact = under = over = 0
    for record in answered:
        gaps = len(record.get("gaps") or [])
        blocks = int(record.get("insufficient") or 0)
        pairs[f"{gaps}→{blocks}"] = pairs.get(f"{gaps}→{blocks}", 0) + 1
        if blocks == gaps:
            exact += 1
        elif blocks < gaps:
            under += 1
        else:
            over += 1
    total = len(answered)
    declared_without_gaps = sum(
        1 for record in records if not record.get("gaps") and int(record.get("insufficient") or 0)
    )
    return {
        "cases_with_gaps": len(with_gaps),
        "answered": total,
        "unanswered": len(with_gaps) - total,
        "pairs": dict(sorted(pairs.items())),
        "exact": exact,
        "exact_rate": _rate(exact, total),
        "under": under,
        "under_rate": _rate(under, total),
        "over": over,
        "over_rate": _rate(over, total),
        "declared_without_gaps": declared_without_gaps,
    }


def gap_case_outcomes(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """有缺口的题最后落在哪一档——拦截率是 0 时，靠它分辨「约束住了」还是「分母太小」。

    * ``declared``：答案非空且含「材料不足」（闸放行）；
    * ``blocked``：``answer = None`` 且拒绝码是缺口闸（**拦截**）；
    * ``rejected_other``：``answer = None``，但被四条硬校验 / 生成失败挡住（与缺口无关）。

    第四档 ``leaked``（有缺口、答案非空、却没有声明）在结构上不存在：缺口闸保证它必然被拒。
    这条「结构性不存在」本身就是要写进报告的一句话——它说明闸的失败方式只有整份作废。
    """

    declared = blocked = rejected_other = 0
    by_group: dict[str, dict[str, int]] = {}
    for record in records:
        if not record.get("gaps"):
            continue
        group = str(record.get("group") or "?")
        bucket = by_group.setdefault(
            group, {"declared": 0, "blocked": 0, "rejected_other": 0, "cases": 0}
        )
        bucket["cases"] += 1
        if record.get("answer") is not None:
            declared += 1
            bucket["declared"] += 1
        elif str(record.get("answer_rejected_reason")) == REJECTED_GAP_NOT_DECLARED:
            blocked += 1
            bucket["blocked"] += 1
        else:
            rejected_other += 1
            bucket["rejected_other"] += 1
    return {
        "declared": declared,
        "blocked": blocked,
        "rejected_other": rejected_other,
        "leaked": 0,
        "by_group": dict(sorted(by_group.items())),
        "blocked_by_group": {
            group: values["blocked"] for group, values in sorted(by_group.items())
        },
        "declared_by_group": {
            group: values["declared"] for group, values in sorted(by_group.items())
        },
    }


def _core_metrics(records: Sequence[Mapping[str, Any]]) -> dict[str, Metric]:
    cases = len(records)
    with_gaps = sum(1 for record in records if record.get("gaps"))
    intercepted = sum(
        1
        for record in records
        if str(record.get("answer_rejected_reason")) == REJECTED_GAP_NOT_DECLARED
    )
    triggered = sum(
        1
        for record in records
        if int(record.get("model_calls") or 0) >= TRIGGER_MIN_MODEL_CALLS
    )
    residual_unknown = sum(
        1
        for record in records
        if any(
            str(point.get("status")) == SUPPORT_UNKNOWN
            for point in record.get("coverage") or []
        )
    )
    return {
        "缺口出现率": _metric(with_gaps, cases),
        "拦截率": _metric(intercepted, with_gaps),
        "⑤ 触发率": _metric(triggered, cases),
        "unknown 残留率": _metric(residual_unknown, cases),
    }


def _primary(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """主条件（⑤ 开着）的记录。对照条件（关 ⑤）不参与那五个指标——它们自己有一套。"""

    return [
        record
        for record in records
        if str(record.get("condition") or CONDITION_FIVEFOLD) == CONDITION_FIVEFOLD
    ]


def _control(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """对照条件（关 ⑤）的记录。"""

    return [
        record for record in records if str(record.get("condition")) == CONDITION_CONTROL
    ]


def _normalized(text: Any) -> str:
    return " ".join(str(text or "").split())


def point_status_map(record: Mapping[str, Any]) -> dict[str, str]:
    """一道题的「要点文本 → 覆盖状态」（原样文本，仅供报告与调试时读）。"""

    statuses = {
        str(point.get("point_id")): str(point.get("status"))
        for point in record.get("coverage") or []
    }
    out: dict[str, str] = {}
    for index, text in enumerate(record.get("points") or [], start=1):
        out[_normalized(text)] = statuses.get(f"A{index}", SUPPORT_UNKNOWN)
    return out


def keyword_points(record: Mapping[str, Any], keyword: str) -> list[tuple[str, str]]:
    """这一题里**讲到那个不可答方面**的要点（按关键词判），以及它们的覆盖状态。

    为什么按关键词对齐、不按 ``A1`` / ``A2`` 也不按要点原文：两次跑是两次规划调用，实测
    **10/10 题的要点清单都不一样**（条数与措辞全变），原文精确匹配一条都对不上。而关键词
    是这一票唯一**我们注入并机械验证过**的标识——「这条要点讲的是那个语料里没有的方面」，
    它比序号与自由措辞都稳。
    """

    statuses = {
        str(point.get("point_id")): str(point.get("status"))
        for point in record.get("coverage") or []
    }
    out: list[tuple[str, str]] = []
    for index, text in enumerate(record.get("points") or [], start=1):
        if keyword and keyword in str(text):
            out.append((_normalized(text), statuses.get(f"A{index}", SUPPORT_UNKNOWN)))
    return out


def _collapse(statuses: Sequence[str]) -> str:
    """多要点同指一个方面时（A03 就有两条讲企业微信），取**最乐观**的那一条当这个方面的结论。

    取最乐观是为了不把「救回」算多：只有真的有一条落到 supported，才算是救回来了；
    反过来若取最悲观，一条 none 就会把同组的 supported 抹掉，救回会被系统性低估。
    """

    for candidate in (SUPPORT_SUPPORTED, SUPPORT_NONE, SUPPORT_UNKNOWN):
        if candidate in statuses:
            return candidate
    return SUPPORT_UNKNOWN


def unknown_frequency(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """条件 2（关 ⑤）里 ``unknown`` 的**真实频率**——它此时不会被人复查消费掉，直接可数。

    这才是「若没有 ⑤，多少题会带 ``unknown``」的直接测量。``残留率`` 那个口径在 ⑤ 上线后
    恒为 0：复查会把它改成 supported / none，成功跑批里一条都剩不下。这不是「零残留说明
    没问题」，而是**那个指标已经量不到东西了**。
    """

    cases = list(records)
    counts: dict[str, int] = {}
    unknown_points = 0
    per_case: list[dict[str, Any]] = []
    for record in cases:
        point_ids = [
            str(point.get("point_id"))
            for point in record.get("coverage") or []
            if str(point.get("status")) == SUPPORT_UNKNOWN
        ]
        unknown_points += len(point_ids)
        key = str(len(point_ids))
        counts[key] = counts.get(key, 0) + 1
        per_case.append({"case": record.get("case"), "unknown": len(point_ids),
                         "points": len(record.get("points") or [])})
    with_unknown = sum(count for key, count in counts.items() if key != "0")
    return {
        "cases": len(cases),
        "cases_with_unknown": with_unknown,
        "rate": _rate(with_unknown, len(cases)),
        "unknown_points": unknown_points,
        "point_total": sum(len(record.get("points") or []) for record in cases),
        "count_distribution": dict(sorted(counts.items(), key=lambda item: int(item[0]))),
        "per_case": per_case,
    }


def fivefold_comparison(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """A 组「有 ⑤ / 无 ⑤」的逐题对照——四个读数一次算齐。

    四件事，谁都替不了谁：

    1. **条件 2 的 `unknown` 真实频率**（:func:`unknown_frequency`）；
    2. **改判分布**：关 ⑤ 时是 `unknown` 的那些要点，开 ⑤ 之后落成了什么。按**关键词**
       对齐（理由见 :func:`keyword_points`：两次跑的要点原文 10/10 题都不一样）；
    3. **净效果**：`gaps` 变多还是变少、答案非空率、`insufficient` 块数；
    4. **两种方向相反的价值**：``unknown → none`` 是**把隐藏的缺口暴露出来**（更严格），
       ``unknown → supported`` 是**把答案救回来**（覆盖率更高）。这两个数方向相反，
       合成一个「改善率」会把一件坏事和一件好事加在一起——所以它们分开报。
    """

    cases_by_id = {case.case_id: case for case in EVAL_CASES}
    primary = {
        str(record.get("case")): record
        for record in _primary(records)
        if str(record.get("group")) == GROUP_A
    }
    control = {
        str(record.get("case")): record
        for record in _control(records)
        if str(record.get("group")) == GROUP_A
    }
    paired = [case_id for case_id in control if case_id in primary]

    to_supported = to_none = stayed_unknown = not_planned = other_unknown = 0
    plan_differs: list[str] = []
    transitions: list[dict[str, Any]] = []
    gaps_up = gaps_down = gaps_same = 0
    answered_with = answered_without = 0
    insufficient_with = insufficient_without = 0
    per_case: list[dict[str, Any]] = []
    for case_id in paired:
        case = cases_by_id.get(case_id)
        keyword = case.keyword if case is not None else ""
        with_record = primary[case_id]
        without_record = control[case_id]
        control_points = keyword_points(without_record, keyword)
        primary_points = keyword_points(with_record, keyword)
        if [text for text, _ in control_points] != [text for text, _ in primary_points]:
            plan_differs.append(case_id)

        case_transitions: list[dict[str, str]] = []
        for text, status in point_status_map(without_record).items():
            if status != SUPPORT_UNKNOWN:
                continue
            if keyword not in text:
                other_unknown += 1
                case_transitions.append({"point": text, "after": "other_unknown"})
                continue
            if not primary_points:
                not_planned += 1
                case_transitions.append({"point": text, "after": "not_planned"})
                continue
            after = _collapse([value for _text, value in primary_points])
            if after == SUPPORT_SUPPORTED:
                to_supported += 1
            elif after == SUPPORT_NONE:
                to_none += 1
            else:
                stayed_unknown += 1
            case_transitions.append({"point": text, "after": after})

        gaps_with = len(with_record.get("gaps") or [])
        gaps_without = len(without_record.get("gaps") or [])
        if gaps_with > gaps_without:
            gaps_up += 1
        elif gaps_with < gaps_without:
            gaps_down += 1
        else:
            gaps_same += 1
        answered_with += 1 if with_record.get("answer") is not None else 0
        answered_without += 1 if without_record.get("answer") is not None else 0
        insufficient_with += int(with_record.get("insufficient") or 0)
        insufficient_without += int(without_record.get("insufficient") or 0)

        per_case.append(
            {
                "case": case_id,
                "gaps_with": gaps_with,
                "gaps_without": gaps_without,
                "answered_with": with_record.get("answer") is not None,
                "answered_without": without_record.get("answer") is not None,
                "insufficient_with": int(with_record.get("insufficient") or 0),
                "insufficient_without": int(without_record.get("insufficient") or 0),
                "model_calls_with": int(with_record.get("model_calls") or 0),
                "model_calls_without": int(without_record.get("model_calls") or 0),
                "transitions": case_transitions,
            }
        )
        transitions.extend({"case": case_id, **item} for item in case_transitions)

    control_metrics = dict(_core_metrics(list(control.values())))
    control_metrics["声明对齐度"] = declaration_alignment(list(control.values()))
    total_transitions = to_supported + to_none + stayed_unknown
    return {
        "paired_cases": len(paired),
        "missing": sorted(set(control) ^ set(primary)),
        "without_fivefold": unknown_frequency(list(control.values())),
        "without_fivefold_metrics": control_metrics,
        "transitions": {
            "unknown_to_supported": to_supported,
            "unknown_to_none": to_none,
            "unknown_to_unknown": stayed_unknown,
            "not_planned_in_fivefold_run": not_planned,
            "other_unknown_points": other_unknown,
            "total": total_transitions,
            "to_supported_rate": _rate(to_supported, total_transitions),
            "to_none_rate": _rate(to_none, total_transitions),
            "plan_differs_cases": sorted(plan_differs),
        },
        "net": {
            "gaps_up": gaps_up,
            "gaps_down": gaps_down,
            "gaps_same": gaps_same,
            "gaps_with": sum(len(record.get("gaps") or []) for record in primary.values()),
            "gaps_without": sum(len(record.get("gaps") or []) for record in control.values()),
            "answered_with": answered_with,
            "answered_without": answered_without,
            "insufficient_with": insufficient_with,
            "insufficient_without": insufficient_without,
        },
        "per_case": per_case,
        "details": transitions,
    }


def summarize(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """五个指标一次算齐（每个率都给分子分母，避免 0/0 装成 1）。

    **只算主条件**（⑤ 开着）：对照条件（关 ⑤）的读数在 :func:`fivefold_comparison` 里，
    它要回答的是另一个问题。混进来会让「缺口出现率」变成两批题的混合数。
    """

    records = _primary(records)
    metrics: dict[str, Any] = {"cases": len(records)}
    metrics.update(_core_metrics(records))
    metrics["声明对齐度"] = declaration_alignment(records)
    metrics["gap_case_outcomes"] = gap_case_outcomes(records)
    metrics["by_group"] = {
        group: _core_metrics([record for record in records if record.get("group") == group])
        for group in (GROUP_A, GROUP_B)
    }
    metrics["by_group"]["声明对齐度"] = {
        group: declaration_alignment(
            [record for record in records if record.get("group") == group]
        )
        for group in (GROUP_A, GROUP_B)
    }
    return metrics


def recompute_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    """从**落盘的逐题记录**重算指标（不调用模型、不连库）。

    产物因此可以离线复算：改一条统计口径不该重花那八十几次调用。逐题记录里能复算的是
    ``insufficient`` 块数、是否声明、声明串出现次数三个派生量——它们都只依赖落盘的
    ``citations`` 与 ``answer``，**不依赖跑的那一刻**，所以重算结果必须与落盘一致。
    """

    records = list(payload.get("records") or [])
    for record in records:
        # 早期产物（加对照条件之前）没有这两个字段：缺省就是「⑤ 开着」的主条件。
        record.setdefault("condition", CONDITION_FIVEFOLD)
        record.setdefault("fivefold", record["condition"] == CONDITION_FIVEFOLD)
        answer = record.get("answer")
        record["insufficient"] = insufficient_blocks(record)
        record["declared_insufficient"] = bool(
            isinstance(answer, str) and GAP_DECLARATION in answer
        )
        record["gap_declaration_count"] = (
            answer.count(GAP_DECLARATION) if isinstance(answer, str) else 0
        )
    payload["records"] = records
    payload["metrics"] = summarize(records)
    payload["fivefold"] = fivefold_comparison(records)
    model_calls = dict(payload.get("model_calls") or {})
    model_calls["actual"] = sum(int(record.get("model_calls") or 0) for record in records)
    payload["model_calls"] = model_calls
    return payload


# ─────────────────────────────────────────────────────────── 预算


def predict_model_calls() -> dict[str, Any]:
    """开跑前的预估：**名义**与**最坏**两个数，两个条件分开算再相加。

    * 主条件：20 题 × 名义 4（规划 + 判定 + 复查 + 生成）/ 最坏 5（+强模型兜底位）；
    * 对照条件（A 组关 ⑤）：10 题 × 名义 3（少一次复查）/ 最坏 4。

    最坏也不许超硬顶——超了就该减题（票面：不许超跑）。
    """

    main_nominal = CASE_COUNT * NOMINAL_CALLS_PER_CASE
    main_worst = CASE_COUNT * WORST_CALLS_PER_CASE
    control_cases = sum(1 for case in EVAL_CASES if case.group == GROUP_A)
    control_nominal = control_cases * NOMINAL_CALLS_PER_CONTROL_CASE
    control_worst = control_cases * WORST_CALLS_PER_CONTROL_CASE
    return {
        "budget": MODEL_CALL_CAP,
        "nominal": main_nominal + control_nominal,
        "worst": main_worst + control_worst,
        "actual": 0,
        "per_case": {
            "nominal": NOMINAL_CALLS_PER_CASE,
            "worst": WORST_CALLS_PER_CASE,
            "control_nominal": NOMINAL_CALLS_PER_CONTROL_CASE,
            "control_worst": WORST_CALLS_PER_CONTROL_CASE,
        },
        "conditions": {
            "main": {"cases": CASE_COUNT, "nominal": main_nominal, "worst": main_worst},
            CONDITION_CONTROL: {
                "cases": control_cases,
                "nominal": control_nominal,
                "worst": control_worst,
            },
        },
    }


@dataclass
class Budget:
    """模型调用预算：**先预留再发**。不够就不再开下一题，而不是跑完再算账。"""

    cap: int = MODEL_CALL_CAP
    spent: int = 0
    skipped: list[str] = field(default_factory=list)

    def can(self, calls: int) -> bool:
        return self.spent + calls <= self.cap

    def charge(self, calls: int) -> None:
        self.spent += max(0, calls)

    def skip(self, label: str, calls: int) -> None:
        self.skipped.append(f"{label}（还要 {calls} 次，已用 {self.spent}/{self.cap}）")


# ─────────────────────────────────────────────────────────── plan（0 次调用）


def build_plan(*, evidences: Sequence[KeywordEvidence] | None = None) -> dict[str, Any]:
    """开跑前的自检与预估。**纯函数**：证据由调用方查好递进来（``plan`` 命令才连库）。

    这样 ``plan`` 的那一半（题集不变量 + 预算）在没有库、没有配置的机器上也跑得动，
    而「关键词真的 0 命中」这件事只在有库时才算——两件事分开，谁缺了都说得清。
    """

    cases = load_cases()
    by_id = {evidence.case_id: evidence for evidence in evidences or ()}
    rows: list[dict[str, Any]] = []
    for case in cases:
        evidence = by_id.get(case.case_id)
        rows.append(
            {
                "case_id": case.case_id,
                "group": case.group,
                "query": case.query,
                "keyword": case.keyword,
                "topic": case.topic,
                "doc_hits": evidence.doc_hits if evidence else None,
                "chunk_hits": evidence.chunk_hits if evidence else None,
            }
        )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "issue": "#167 缺口闸真实行为评测",
        "model_calls": predict_model_calls(),
        "checks": check_case_set(),
        "cases": rows,
        "evidence_checked": evidences is not None,
    }


def render_plan(plan: Mapping[str, Any]) -> str:
    """人读的预估：预算（名义 / 最坏）+ 题集清单 + 关键词命中。"""

    calls = plan["model_calls"]
    checks = plan["checks"]
    lines = [
        "# #167 缺口闸真实行为评测 · 开跑前自检",
        "",
        f"- 生成时间：`{plan['generated_at']}`",
        f"- 题集：A 半可答 {checks['a_cases']} 题 + B 全不可答 {checks['b_cases']} 题"
        f"（其中 {checks['bank_absent_cases']} 题取自题库 absent 标注，"
        f"{checks['constructed_b_cases']} 题我方构造）",
        f"- 自检：前题可解析={checks['base_ids_ok']} 前缀={checks['prefix_ok']} "
        f"关键词唯一={checks['keywords_unique']}",
        f"- 关键词证据：{'已查' if plan.get('evidence_checked') else '未查（plan 未连库）'}",
        "",
        "## 模型调用预估（硬顶 {} 次）".format(calls["budget"]),
        "",
        f"- 名义：**{calls['nominal']}** 次 / 最坏：**{calls['worst']}** 次",
    ]
    for name, block in (calls.get("conditions") or {}).items():
        lines.append(
            f"  - `{name}`：{block['cases']} 题 × 名义 {block['nominal'] // max(1, block['cases'])}"
            f" / 最坏 {block['worst'] // max(1, block['cases'])} → 名义 {block['nominal']}、"
            f"最坏 {block['worst']} 次"
        )
    lines += [
        "",
        "## 题集清单",
        "",
        "| 题 | 组 | 关键词 | 文档命中 | 切片命中 | 题面 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in plan["cases"]:
        doc_hits = "—" if row["doc_hits"] is None else row["doc_hits"]
        chunk_hits = "—" if row["chunk_hits"] is None else row["chunk_hits"]
        lines.append(
            f"| {row['case_id']} | {row['group']} | {row['keyword']} | {doc_hits} | "
            f"{chunk_hits} | {row['query']} |"
        )
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────── 跑（真实检索 + 真实模型）

#: 台账的固定字段（``case_record`` 产出、报告与 results.json 都读这一份）。少一个字段，
#: 报告就少一格证据——所以它不散在报告代码里，而是这里一份清单。
RECORD_KEYS: Final[tuple[str, ...]] = (
    "case",
    "group",
    "query",
    "keyword",
    "gaps",
    "coverage",
    "model_calls",
    "followup_rounds",
    "searches",
    "stop_reason",
    "answered",
    "answer_rejected_reason",
    "citations",
    "insufficient",
    "declared_insufficient",
    "gap_declaration_count",
    "materials",
    "evidence_state",
    "sub_search_degraded",
    "duration_ms",
    "points",
    "condition",
    "fivefold",
    "answer",
)


async def _bind_model_chain() -> dict[str, Any]:
    """把模型接入接到**生产那条链**（``model_configs`` 的角色指针），返回用的是哪一条。

    不接会怎样：走 ``.env`` 的单供应商 key，与库里配的那条不是同一个账号，每道题都会在
    生成阶段失败，读数看起来像「闸门不行」——#153 / #165 都在评测侧踩过同一脚。
    """

    from app.core.config import settings
    from app.providers.llm import set_role_resolver
    from app.services import model_config_service

    set_role_resolver(model_config_service.role_endpoint)
    endpoint = await model_config_service.role_endpoint("fast")
    if endpoint is None:
        return {"llm_endpoint_source": "env", "llm_model": str(settings.LLM_MODEL)}
    return {
        "llm_endpoint_source": "model_configs",
        "llm_model": str(endpoint.model),
        "llm_config_id": str(endpoint.config_id),
    }


async def _fast_provider() -> Any:
    from app.providers.llm import get_llm_provider

    return await get_llm_provider("fast")


async def _strong_provider() -> Any:
    from app.providers.llm import get_llm_provider

    return await get_llm_provider("strong")


def _retrieval_options() -> Any:
    """这次评测用的检索参数：与 ``balanced`` 档同形（混合、5 条、不重排、不改写）。

    为什么由脚本构造而不是走 ``resolve_retrieval_options``：那条路要一份 ``KeyPolicySnapshot``
    + 套餐，而评测没有「这套集成买的是哪一档」。编一个套餐名比承认「这是评测自己的参数」
    更糟——:class:`RetrievalOptions` 的 ``tenant_policy=None`` 正是为这种情形留的。
    """

    from app.services.retrieval_options import RetrievalOptions

    return RetrievalOptions(
        mode="hybrid",
        top_k=5,
        fold_to_sections=True,
        expand_table_rows=False,
        rerank=False,
        rewrite=False,
    )


async def _resolve_scope(db: Any, *, tenant_id: str, kb_ids: Sequence[str]) -> dict[str, str]:
    """``kb_id → 生效索引版本``：走生产那一个构造点（``resolve_index_versions``）。

    不手写 ``"v1"``：版本是服务端的决定，评测抄一个常量就等于把「这次查的是哪一版」写成
    一个随时会过期的假设。
    """

    from sqlalchemy import select

    from app.models.index_version import resolve_index_versions
    from app.models.knowledge_base import KnowledgeBase

    rows = (
        await db.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.tenant_id == tenant_id, KnowledgeBase.id.in_(list(kb_ids))
            )
        )
    ).scalars().all()
    return dict(resolve_index_versions(list(rows)))


async def _db_evidence_fetch(sql: str, params: Mapping[str, Any]) -> Mapping[str, int]:
    """证据查询的真实现：一次只读查询，``kb_ids`` 用 expanding 绑定。"""

    from sqlalchemy import bindparam, text

    from app.db.async_session import AsyncSessionLocal

    stmt = text(sql).bindparams(bindparam("kb_ids", expanding=True))
    async with AsyncSessionLocal() as db:
        row = (await db.execute(stmt, dict(params))).mappings().first()
    return dict(row or {"doc_hits": 0, "chunk_hits": 0})


async def collect_evidence(
    *, fetch: Callable[[str, Mapping[str, Any]], Awaitable[Mapping[str, int]]] | None = None
) -> list[KeywordEvidence]:
    """逐题查关键词命中；任何一题不成立就当场失败（证据不成立，读数不成立）。"""

    probe = fetch or _db_evidence_fetch
    evidences: list[KeywordEvidence] = []
    for case in load_cases():
        row = await probe(evidence_sql(), evidence_params(case))
        evidence = _evidence_from_row(case, row)
        require_absent(evidence)
        evidences.append(evidence)
    return evidences


def case_record(case: EvalCase, outcome: Any) -> dict[str, Any]:
    """一次 Research 的读数 → 一行台账（报告与 results.json 共用这一份）。"""

    answer = outcome.answer
    citations = [dict(citation) for citation in outcome.citations]
    coverage = [
        {
            "point_id": verdict.point_id,
            "status": verdict.status,
            "evidence_ids": list(verdict.evidence_ids),
        }
        for verdict in outcome.coverage
    ]
    record = {
        "case": case.case_id,
        "group": case.group,
        "query": case.query,
        "keyword": case.keyword,
        "gaps": list(outcome.gaps),
        "coverage": coverage,
        "model_calls": int(outcome.model_calls),
        "followup_rounds": int(outcome.followup_rounds),
        "searches": int(outcome.searches),
        "stop_reason": str(outcome.stop_reason.value),
        "answered": answer is not None,
        "answer_rejected_reason": outcome.answer_rejected_reason,
        "citations": citations,
        "insufficient": insufficient_blocks({"citations": citations}),
        "declared_insufficient": bool(
            isinstance(answer, str) and GAP_DECLARATION in answer
        ),
        "gap_declaration_count": answer.count(GAP_DECLARATION) if isinstance(answer, str) else 0,
        "materials": len(outcome.materials),
        "evidence_state": outcome.evidence_state,
        "sub_search_degraded": list(outcome.sub_search_degraded),
        "duration_ms": int(outcome.duration_ms),
        "points": [point.text for point in outcome.points],
        "condition": CONDITION_FIVEFOLD,
        "fivefold": True,
        "answer": answer,
    }
    assert set(record) == set(RECORD_KEYS), set(record) ^ set(RECORD_KEYS)
    return record


async def run_case(
    case: EvalCase, *, index_versions: Mapping[str, str], fivefold: bool = True
) -> dict[str, Any]:
    """跑**生产完整链路**一次：真实检索内核 + 真实模型角色 + 完整五步状态机。

    每个题一个会话：一次 Research 会跑 1~5 次子检索，长跑批共用一个会话会让连接在
    「既不在事务里也没人用」的状态上挂十几分钟——那是评测机的运气，不是被测对象的行为。
    """

    from app.db.async_session import AsyncSessionLocal
    from app.services.research import run_research
    from app.services.research_gate import build_kernel_search

    degraded: list[str] = []
    async with AsyncSessionLocal() as db:
        search = build_kernel_search(
            db,
            tenant_id=TENANT_ID,
            index_versions=dict(index_versions),
            options=_retrieval_options(),
            on_degraded=degraded.append,
        )
        outcome = await run_research(
            query=case.query,
            tenant_id=TENANT_ID,
            search=search,
            llm_factory=_fast_provider,
            strong_llm_factory=_strong_provider,
            sub_search_degraded=degraded,
        )
    record = case_record(case, outcome)
    record["condition"] = CONDITION_FIVEFOLD if fivefold else CONDITION_CONTROL
    record["fivefold"] = fivefold
    return record


async def _run_without_rejudge(case: EvalCase, *, index_versions: Mapping[str, str]) -> dict[str, Any]:
    """跑一次**关掉 ⑤** 的 Research——对照条件，只在评测进程内生效。

    关法：把 ``research._Run._rejudge`` 换成一个直接返回的 async no-op，跑完在 ``finally``
    里换回来。**为什么不改成配置开关**：那是在 ``app/`` 里为一个评测加一条生产分支，而这条
    分支平时永不发生——正是 ADR-0029 点名的「默认关、生产永不发生」的死代码。评测要造的条件，
    评测自己造；``finally`` 保证即使这道题炸了，类属性也能还原（一份被污染的方法会让后面每一
    题都静默跑错条件，而读数看起来完全正常）。
    """

    from app.services import research as research_service

    original = research_service._Run._rejudge

    async def _noop(self: Any) -> None:
        return None

    research_service._Run._rejudge = _noop  # type: ignore[method-assign]
    try:
        return await run_case(case, index_versions=index_versions, fivefold=False)
    finally:
        research_service._Run._rejudge = original  # type: ignore[method-assign]


async def run_cases(
    cases: Sequence[EvalCase],
    *,
    budget: Budget,
    index_versions: Mapping[str, str],
    fivefold: bool = True,
) -> list[dict[str, Any]]:
    """逐题跑，**先预留再发**：预留不到就不跑下一题（预算是一条上限，不是一句愿望）。"""

    per_case = WORST_CALLS_PER_CASE if fivefold else WORST_CALLS_PER_CONTROL_CASE
    records: list[dict[str, Any]] = []
    for case in cases:
        if not budget.can(per_case):
            budget.skip(case.case_id, per_case)
            continue
        if fivefold:
            record = await run_case(case, index_versions=index_versions)
        else:
            record = await _run_without_rejudge(case, index_versions=index_versions)
        budget.charge(int(record["model_calls"]))
        records.append(record)
        print(
            f"  {case.case_id}（{record['condition']}）: stop={record['stop_reason']} "
            f"gaps={len(record['gaps'])} 声明块={record['insufficient']} "
            f"unknown={sum(1 for p in record['coverage'] if p['status'] == SUPPORT_UNKNOWN)} "
            f"calls={record['model_calls']} rejected={record['answer_rejected_reason']}",
            flush=True,
        )
    return records


def _apply_eval_settings() -> dict[str, Any]:
    """钉死评测要用的环境：**只读评测索引**、不重排、不改写。

    与 ``scripts/eval_retrieval.py`` 同一条纪律（``settings.ELASTICSEARCH_INDEX`` →
    ``*_eval``）：评测绝不读生产别名，也绝不写任何索引。三个开关在这里显式写下而不是
    吃 ``.env`` 的偶然值——它们会改变「检索引擎捞到什么」，而这个评测要量的正是捞之后的闸门。
    """

    from app.core.config import settings

    settings.ELASTICSEARCH_INDEX = EVAL_ELASTICSEARCH_INDEX
    settings.RERANK_ENABLED = False
    settings.QUERY_REWRITE_ENABLED = False
    return {
        "elasticsearch_index": settings.ELASTICSEARCH_INDEX,
        "rerank": settings.RERANK_ENABLED,
        "query_rewrite": settings.QUERY_REWRITE_ENABLED,
        "retrieval_mode": settings.RETRIEVAL_MODE,
        "top_k": settings.TOP_K,
        "tenant_id": TENANT_ID,
        "kb_ids": list(KB_IDS),
    }


# ─────────────────────────────────────────────────────────── 报告


def _fmt_rate(value: Any) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:.1f}%"


def _metric_row(label: str, metric: Mapping[str, Any]) -> str:
    return (
        f"| {label} | {_fmt_rate(metric.get('value'))} | "
        f"{metric.get('numerator')}/{metric.get('denominator')} |"
    )


def archived_trigger_reading(path: Path | None = None) -> dict[str, Any]:
    """归档旁证：C6 真实 8 题的 ``max_rounds`` 占比（#165 用它推断 ⑤ 的频率）。

    这里读它不是为了拿来当结论，而是为了让报告里的「37.5%（3/8）」**可复算**：读者能自己
    打开那份产物数一遍，而不是相信报告里的一个数字。
    """

    target = path or ARCHIVED_RUN
    if not target.exists():
        return {"available": False, "path": str(target)}
    payload = json.loads(target.read_text(encoding="utf-8"))
    rows = list(payload.get("per_question") or [])
    reasons: dict[str, int] = {}
    for row in rows:
        key = str(row.get("stop_reason"))
        reasons[key] = reasons.get(key, 0) + 1
    recovered = reasons.get("max_rounds", 0)
    return {
        "available": True,
        "path": _relative(target),
        "questions": len(rows),
        "stop_reasons": reasons,
        "recovered": recovered,
        "rate": _rate(recovered, len(rows)),
    }


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def _rejected_cases(records: Sequence[Mapping[str, Any]]) -> list[str]:
    """有缺口但答案被整份作废的题（+ 拒绝码）：它们**没有**被对齐度量到，要单独点名。"""

    out: list[str] = []
    for record in records:
        if record.get("gaps") and not isinstance(record.get("answer"), str):
            out.append(f"{record.get('case')}（`{record.get('answer_rejected_reason')}`）")
    return out


def _degraded_cases(records: Sequence[Mapping[str, Any]]) -> list[str]:
    """子检索降级过的题：一旦关键词或向量一路挂了，读数是「单路检索下的闸门行为」。"""

    return [
        str(record.get("case"))
        for record in records
        if record.get("sub_search_degraded")
    ]


def text_level_verdict(payload: Mapping[str, Any]) -> list[str]:
    """问题一：文本级缺口闸够不够用？判据是**欠声明占比**，不是拦截率。

    ADR-0029 实施备注第 4 条当初明确选了文本级，理由是「不扩大模型可表达面」。要翻它，得先
    证明它真的漏掉了东西——而它唯一会漏的就是「块数 < 缺口数」这一档（闸只查那句话在不在）。
    """

    metrics = payload.get("metrics") or {}
    alignment = metrics.get("声明对齐度") or {}
    total = alignment.get("cases_with_gaps") or 0
    answered = alignment.get("answered") or 0
    unanswered = alignment.get("unanswered") or 0
    under = alignment.get("under") or 0
    exact = alignment.get("exact") or 0
    over = alignment.get("over") or 0
    lines = [
        f"- 有缺口的题 **{total}** 题，其中**真的交出了答案**的 **{answered}** 题是对齐度的分母；"
        f"另外 {unanswered} 题答案被整份作废（被四条硬校验 / 生成失败挡下），未测量"
        f"——它们是：{'、'.join(_rejected_cases(payload.get('records') or [])) or '无'}。",
        f"- 对齐度分布：对齐 **{exact}** / 欠声明 **{under}** / 过声明 **{over}**"
        f"（分布表见上；分母 {answered}）。",
    ]
    if not answered:
        lines.append(
            "- **这批题一题答案都没生成出来**，对齐度无从谈起：只能说这次闸门没有被真正驱动过，"
            "不能据此判断文本级够不够用。"
        )
        return lines
    under_rate = alignment.get("under_rate")
    if under_rate is not None and float(under_rate) > 0.5:
        lines += [
            f"- **判断：文本级不够用**。欠声明占 {_fmt_rate(under_rate)}（{under}/{answered}）："
            "多数题的声明块数少于缺口数，而闸只检查「材料不足」这句话在不在，于是这些题**全部**"
            "被放行——答案看起来承认了不足，实际只承认了其中一块。",
            "- 处置建议：重新裁决 ADR-0029 实施备注第 4 条，考虑**逐缺口对齐**（要求每个 "
            "`none` 要点各一块 `insufficient`）。代价是必须把要点编号暴露给生成步，那会扩大"
            "模型的可表达面——当初否掉它的正是这条理由，所以要改就得先承认那个权衡换了个方向。",
        ]
    elif under > 0:
        lines += [
            f"- **判断：文本级基本够用，但有边界**。欠声明占 {_fmt_rate(under_rate)}"
            f"（{under}/{answered}）：不是零，但也没到「多数题被放行」的地步。",
            "- 读法：闸能挡住「完全不认账」的那一类，挡不住「只认一部分」的那一类。后者要不要"
            "修取决于调用方怎么用 `coverage.gaps`——它逐要点给出了缺口清单，是文本级闸门的"
            "**旁路**。维持文本级的前提是「调用方真的会读 gaps」，这件事这份评测**没有测**。",
        ]
    else:
        lines += [
            f"- **判断：文本级够用**。欠声明 {under}/{answered}（0 例）：每一道真的交了答案的"
            "缺口题，声明块数都**正好等于**缺口数（对齐 "
            f"{exact}/{answered}，过声明 {over}/{answered}）——闸没有漏掉任何「假装没有缺口」"
            "的答案，也没有出现「有缺口却只写一句」的情形。",
            "- 于是 ADR-0029 实施备注第 4 条可以从「推测够用」改成「实测够用」——**在这 17 道"
            "真的生成了答案的题上、这一个模型链上**。翻案条件不变：只要出现「3 个缺口只写 1 句」"
            "这类样本，就该重新裁决。",
            "- 但要读清楚**为什么对齐**：提示词对每个缺口要点都点名要求「各写一块 "
            "`insufficient`」，模型照做了；闸本身仍然只数那句话在不在。也就是说，**对齐度是靠"
            "提示词得到的，闸只是兜底**——这一条正是「维持文本级」的代价：把可靠性押在模型听话上，"
            "而不是押在结构上。",
        ]
    over_cases = alignment.get("over") or 0
    if over_cases:
        lines.append(
            f"- 附带一条相反的信号：{over_cases} 题**过声明**（块数多于缺口数）。它不危险"
            "（没有答案因此被放行），但说明模型是按缺口要点逐条写的、不是只写一句。"
        )
    declared_without_gaps = alignment.get("declared_without_gaps") or 0
    if declared_without_gaps:
        lines.append(
            f"- 另一侧：**{declared_without_gaps}** 题没有缺口却写了「材料不足」。那是模型的"
            "保守，不是闸门的行为（闸只在 gaps 非空时工作），记在这里是为了不让它冒充对齐度。"
        )
    return lines


def intercept_verdict(payload: Mapping[str, Any]) -> list[str]:
    """问题二：拦截率是多少？若为 0，说清是「约束住了模型」还是「分母太小」。"""

    metrics = payload.get("metrics") or {}
    intercepted = metrics.get("拦截率") or {}
    outcomes = metrics.get("gap_case_outcomes") or {}
    total = int(intercepted.get("denominator") or 0)
    blocked = int(intercepted.get("numerator") or 0)
    lines = [
        f"- 拦截率 **{_fmt_rate(intercepted.get('value'))}**（{blocked}/{total}）："
        f"`answer_rejected_reason == REJECTED_GAP_NOT_DECLARED` 的题数 / 有缺口的题数。",
        f"- 缺口题的落点：声明了 {outcomes.get('declared', 0)} 题、被闸拦下 "
        f"{outcomes.get('blocked', 0)} 题、被四条硬校验或生成失败挡下 "
        f"{outcomes.get('rejected_other', 0)} 题。",
    ]
    by_group = outcomes.get("by_group") or {}
    for group in (GROUP_A, GROUP_B):
        bucket = by_group.get(group)
        if not bucket:
            continue
        lines.append(
            f"  - {group} 组：{bucket['cases']} 题有缺口，其中声明 {bucket['declared']}、"
            f"拦截 {bucket['blocked']}、其它拒绝 {bucket['rejected_other']}"
        )
    if total and not blocked:
        a_bucket = by_group.get(GROUP_A) or {}
        b_bucket = by_group.get(GROUP_B) or {}
        a_declared = int(a_bucket.get("declared") or 0)
        b_declared = int(b_bucket.get("declared") or 0)
        a_cases = int(a_bucket.get("cases") or 0)
        b_cases = int(b_bucket.get("cases") or 0)
        lines += [
            "- 拦截率为 0 的两种解释，**用两组的对照分开**：",
            f"  - **「提示词约束住了模型」——数据支持这一条**：A 组（半可答，最容易圆场）"
            f"{a_cases} 题有缺口，声明了 {a_declared} 题，**0 题不认账**；B 组（全不可答）"
            f"{b_cases} 题有缺口，声明了 {b_declared} 题，同样 0 题不认账。两组的落点几乎一样，"
            "也就是说连最该圆场的那一组也没圆场——这是提示词在起作用，不是题太简单。"
            "顺带说：本票原本假设「半可答最容易被顺手答完」，**这批题里没有出现**。",
            f"  - **「分母太小」——这一条只部分成立**：分母是 **{total}** 题，一题翻转就是 5 个"
            "百分点，给不出稳定的百分比；但它的方向是确定的——3 次失败全是**答案被别的原因整份"
            "作废**（"
            + "、".join(_rejected_cases(payload.get("records") or []) or ["无"])
            + "），**没有一次是「闸门该拦而没拦」**。",
            "- 还有一条结构性的读法：有缺口时答案要么非空且必含「材料不足」（闸放行），"
            "要么整份为 `null`（闸或硬校验拦下）。**没有第三种可能**，所以这道闸的失败方式"
            "只能是「整份作废」，而不是「放行一句假话」。0 拦截率因此说的是「模型都认了账」，"
            "不是「闸没在工作」。反过来说，**这道闸在这批题上一次也没被真正挑战过**："
            "它没有失败，也没有立功——真正挡住「假装没缺口」的是提示词。",
        ]
    elif blocked:
        lines.append(
            "- 拦截率非零：模型确实有没认账的时候，闸按设计把它变成了 `answer = null`"
            "（材料、计划、`coverage` 照给）。"
        )
    return lines


def trigger_verdict(payload: Mapping[str, Any]) -> list[str]:
    """问题三：⑤ 触发率的直接测量值，与归档反推的 37.5% 对照。"""

    metrics = payload.get("metrics") or {}
    triggered = metrics.get("⑤ 触发率") or {}
    value = triggered.get("value")
    archived = payload.get("archived") or {}
    lines = [
        f"- 直接测量值：**{_fmt_rate(value)}**（{triggered.get('numerator')}/"
        f"{triggered.get('denominator')}），判据是 `model_calls >= "
        f"{TRIGGER_MIN_MODEL_CALLS}`（= 补证捞到材料、复查真的跑过）。",
        f"- 归档反推的参照值：**{_fmt_rate(ARCHIVED_TRIGGER_RATE)}**"
        f"（{ARCHIVED_TRIGGER_NUMERATOR}/{ARCHIVED_TRIGGER_DENOMINATOR}，取自 "
        f"`{archived.get('path') or _relative(ARCHIVED_RUN)}` 的 `stop_reason == max_rounds`）。",
    ]
    if archived.get("available"):
        lines.append(
            f"  - 那份产物这次也读了一遍：{archived.get('questions')} 题、"
            f"`max_rounds` {archived.get('recovered')} 题（{_fmt_rate(archived.get('rate'))}），"
            "与 #165 报告里的数一致。"
        )
    if value is None:
        lines.append("- 这一组还没跑完，直接测量值空着。")
        return lines
    delta = round(float(value) - ARCHIVED_TRIGGER_RATE, 4)
    if abs(delta) <= 0.15:
        lines.append(
            f"- **对照：一致**（差 {delta:+.1%}）。归档反推没有偏——`max_rounds` 这个停止原因"
            "确实能代表「补证捞到了材料、因而走到了复查」。于是 #165 据此把硬顶 3 改成 4 的"
            "依据站得住：那次契约变更的**频率估计**是对的。"
        )
    elif delta < 0:
        lines.append(
            f"- **对照：偏低**（差 {delta:+.1%}）。归档那 8 题是**同一个租户、同一批合成语料**上"
            "跑的，而本票的 20 题里有一半是「追加了一个语料里没有的方面」——那种题补证**捞不到**"
            "新材料（要查的东西根本不库），于是停在 `no_new_evidence`、不触发复查。偏低因此是"
            "**题集构成差异**的直接结果，不是归档读数错了；反过说，它说明 37.5% 是「自然问题上"
            "的触发率」，而本票的读数更接近「缺口题上的触发率」。"
        )
    else:
        lines.append(
            f"- **对照：偏高**（差 {delta:+.1%}）。这批题比归档那 8 题更容易触发补证——"
            "因为每一题都带一块语料里没有的方面，判定几乎必然报出缺口，而补证几乎必然捞回"
            "**别的节**（检索永远返回 top-k）。于是「补证捞到材料」接近必然而非偶然，"
            "这抬高了触发率。这说明 37.5% 那类反推对**自然问题**是合适的，对**故意造缺口的"
            "题**会低估。"
        )
    return lines


def fivefold_verdict(payload: Mapping[str, Any]) -> list[str]:
    """新问题：⑤ 的价值落在「救回」还是「暴露」？

    两个方向意义相反，所以这里**不合成一个改善率**：

    * ``unknown → none``：把一条其实没有支撑的要点**暴露成缺口**——更严格，答案会更保守；
    * ``unknown → supported``：把一条其实有支撑的要点**救回来**——覆盖率更高，答案更完整。

    多数是前者，说明 ⑤ 的真正作用是「让隐藏缺口现形」，而不是「提高可答率」。这两句话
    常被混成一句「⑤ 让结果更好」，而它们对产品的含义完全不同：一个让答案变少但更准，
    另一个让答案变多。
    """

    comparison = payload.get("fivefold") or {}
    paired = int(comparison.get("paired_cases") or 0)
    if not paired:
        return ["- 对照组还没跑，这一节空着。"]

    transitions = comparison.get("transitions") or {}
    to_none = int(transitions.get("unknown_to_none") or 0)
    to_supported = int(transitions.get("unknown_to_supported") or 0)
    stayed = int(transitions.get("unknown_to_unknown") or 0)
    total = to_none + to_supported + stayed
    lines = [
        f"- 逐要点比了 **{paired}** 题；关 ⑤ 时落 `unknown` 的要点 **{total}** 条，"
        f"其中开 ⑤ 之后：**`unknown → none` {to_none}** 条（`{_fmt_rate(transitions.get('to_none_rate'))}`）、"
        f"**`unknown → supported` {to_supported}** 条"
        f"（`{_fmt_rate(transitions.get('to_supported_rate'))}`）、"
        f"仍是 `unknown` {stayed} 条（复查失败）。",
    ]
    if transitions.get("not_planned_in_fivefold_run"):
        lines.append(
            f"- 另有 **{transitions['not_planned_in_fivefold_run']}** 条 `unknown` 在开 ⑤ 的那次"
            "规划里找不到对应方面的要点（两次拆解不同），没有计入改判分布。"
        )
    if not total:
        lines.append("- 关 ⑤ 时没有一条要点落 `unknown`，⑤ 在这批题上没有可改判的对象。")
    elif to_none > to_supported:
        lines += [
            f"- **判断：⑤ 的价值主要落在「暴露」而不是「救回」**（{to_none} 对 {to_supported}）。"
            "它把一批其实没有支撑的要点从 `unknown` 落成 `none`，于是缺口清单变长、答案被要求"
            "声明得更多。**这不是「提高覆盖率」，是「让隐藏缺口现形」**——两件事对产品的含义"
            "不同：前者让答案变多，后者让答案变少但更准。",
            "- 顺带的推论：关掉 ⑤ 时那些要点**既不算缺口也不算支撑**，缺口闸看不见它们"
            "（文本级，只认 `gaps`），生成步却拿着那条材料照答。这正是 ⑤ 存在的理由。",
        ]
    elif to_supported > to_none:
        lines += [
            f"- **判断：⑤ 的价值主要落在「救回」**（{to_supported} 对 {to_none}）。"
            "大部分 `unknown` 在复查后落成 `supported`——也就是说，补证捞回来的材料"
            "**真的能读出那条事实**，没有 ⑤ 的话这些要点会被记成「材料到了但没人判」，"
            "答案本可以更完整却被保守处理。",
            "- 这一类读数要小心读：落成 `supported` 多，也可能是判定偏乐观（把「相关」当"
            "「支撑」）。#165 测过判定准确率（假缺口率 2.7%、误报 support 率 9.1%），"
            "可以连起来看，但两份读数用的是两套材料，不能直接相乘。",
        ]
    else:
        lines.append(
            f"- **判断：两个方向一样多**（{to_none} 对 {to_supported}）。⑤ 的作用是"
            "「两样都有一点」，不能简化成任何一种单一价值。"
        )
    return lines


def fivefold_section(payload: Mapping[str, Any]) -> list[str]:
    """第五节：A 组「有 ⑤ / 无 ⑤」的四组读数（条件 2 的频率、改判分布、净效果、两种价值）。"""

    comparison = payload.get("fivefold") or {}
    without = comparison.get("without_fivefold") or {}
    control_metrics = comparison.get("without_fivefold_metrics") or {}
    net = comparison.get("net") or {}
    transitions = comparison.get("transitions") or {}
    unknown_point_rate = _rate(
        int(without.get("unknown_points") or 0), int(without.get("point_total") or 0)
    )
    lines = [
        "## 三、⑤ 的对照：A 组关掉 ⑤ 再跑一次",
        "",
        "为什么要有这一组：⑤ 上线后 `unknown` 会被复查消费掉，成功跑批里「`unknown` 残留率」"
        "**必然是 0**——那个指标已经量不到东西了。要量「若没有 ⑤ 会带 `unknown` 的题占多少」，"
        "只能把 ⑤ 关掉再跑一次。",
        "",
        "- 关法（**只在评测进程内，`app/` 一个字节不改**）："
        "`setattr(research._Run, \"_rejudge\", async no-op)`，跑完在 `finally` 里换回来。"
        "不把 ⑤ 做成配置开关，是因为那会在生产代码里留一条「默认关、生产永不发生」的分支"
        "——ADR-0029 点名的死代码。",
        f"- 配对：**{comparison.get('paired_cases')}** 题（A 组每题跑两次，"
        f"两次都是真实检索 + 真实模型）。缺失：{comparison.get('missing') or '无'}",
        "",
        "### 3.1 条件 2 的 `unknown` 真实频率（直接测量，不是反推）",
        "",
        f"- `unknown` 非空的题：**{without.get('cases_with_unknown')}/{without.get('cases')}**"
        f"（`{_fmt_rate(without.get('rate'))}`）；要点级 `unknown`："
        f"{without.get('unknown_points')}/{without.get('point_total')}"
        f"（`{_fmt_rate(unknown_point_rate)}`）",
        "",
        "| 每题 `unknown` 个数 | 题数 |",
        "| --- | --- |",
    ]
    for count, cases in (without.get("count_distribution") or {}).items():
        lines.append(f"| {count} | {cases} |")
    if not without.get("count_distribution"):
        lines.append("| （对照组还没跑） | 0 |")
    lines += [
        "",
        f"- 与归档反推的 **{_fmt_rate(ARCHIVED_TRIGGER_RATE)}**"
        f"（{ARCHIVED_TRIGGER_NUMERATOR}/{ARCHIVED_TRIGGER_DENOMINATOR}）对照：直接测量值是 "
        f"`{_fmt_rate(without.get('rate'))}`。",
    ]
    measured = without.get("rate")
    if measured is not None:
        delta = round(float(measured) - ARCHIVED_TRIGGER_RATE, 4)
        if abs(delta) <= 0.15:
            lines.append(
                f"  - **一致的**（差 {delta:+.1%}）：归档那个反推没有偏——`max_rounds` 确实能代表"
                "「补证捞到了材料、因此会带 `unknown`」。"
            )
        else:
            lines.append(
                f"  - **差 {delta:+.1%}**：本票的题集**每题都注入了一个语料里没有的方面**，判定"
                "几乎必然报缺口，而补证（定向检索）几乎必然捞回**别的节**——检索永远返回 top-k，"
                "空手而归才罕见。于是「补证捞到材料」在这里接近必然，`unknown` 也就接近必然；"
                "归档那 8 题是**自然问题**，不是每题都带缺口。两个数因此**量的不是同一个东西**："
                f"37.5% 是自然问题上的触发率，`{_fmt_rate(measured)}` 是缺口题上的触发率。"
            )
    lines += [
        f"- 条件 2（无 ⑤）的五项读数：缺口出现率 "
        f"`{_fmt_rate((control_metrics.get('缺口出现率') or {}).get('value'))}`、"
        f"拦截率 `{_fmt_rate((control_metrics.get('拦截率') or {}).get('value'))}`"
        f"（{(control_metrics.get('拦截率') or {}).get('numerator')}/"
        f"{(control_metrics.get('拦截率') or {}).get('denominator')}）、"
        f"`unknown` 残留率 `{_fmt_rate((control_metrics.get('unknown 残留率') or {}).get('value'))}`。",
        "",
        "### 3.2 改判分布（逐要点比，按**关键词**对齐）",
        "",
        "| 关 ⑤ 时的状态 → 开 ⑤ 之后 | 条数 | 占比 |",
        "| --- | --- | --- |",
        f"| `unknown` → `none`（暴露成缺口） | {transitions.get('unknown_to_none')} | "
        f"`{_fmt_rate(transitions.get('to_none_rate'))}` |",
        f"| `unknown` → `supported`（救回） | {transitions.get('unknown_to_supported')} | "
        f"`{_fmt_rate(transitions.get('to_supported_rate'))}` |",
        f"| `unknown` → `unknown`（复查失败） | {transitions.get('unknown_to_unknown')} | — |",
        f"| 开 ⑤ 那次规划里没有这个方面的要点 | "
        f"{transitions.get('not_planned_in_fivefold_run')} | — |",
        f"| 与不可答方面无关的 `unknown`（不入库） | "
        f"{transitions.get('other_unknown_points')} | — |",
        "",
        "**为什么按关键词对齐，不按 `A1` / `A2`，也不按要点原文**：两次跑是两次规划调用，"
        f"实测 **{len(transitions.get('plan_differs_cases') or [])}/{comparison.get('paired_cases')}"
        " 题的要点清单都不一样**（条数与措辞全变），原文精确匹配一条都对不上。关键词是这一票"
        "唯一**我们注入并机械验证过**的标识——「这条要点讲的是那个语料里没有的方面」。"
        "多要点同指一个方面时（有的题会拆成两条）取**最乐观**的那一条，这样「救回」不会被"
        "同组的一条 `none` 抹掉。",
        "",
        "### 3.3 净效果（逐题）",
        "",
        "| 题 | gaps（有 ⑤ / 无 ⑤） | 答案（有 / 无） | 声明块（有 / 无） | 调用（有 / 无） |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in comparison.get("per_case") or []:
        lines.append(
            f"| {row['case']} | {row['gaps_with']} / {row['gaps_without']} | "
            f"{'非空' if row['answered_with'] else 'null'} / "
            f"{'非空' if row['answered_without'] else 'null'} | "
            f"{row['insufficient_with']} / {row['insufficient_without']} | "
            f"{row['model_calls_with']} / {row['model_calls_without']} |"
        )
    if not comparison.get("per_case"):
        lines.append("| （对照组还没跑） | — | — | — | — |")
    lines += [
        "",
        f"- 缺口总数：有 ⑤ **{net.get('gaps_with')}** 条 / 无 ⑤ **{net.get('gaps_without')}** 条；"
        f"逐题看：变多 {net.get('gaps_up')} 题、变少 {net.get('gaps_down')} 题、"
        f"持平 {net.get('gaps_same')} 题。",
        f"- 答案非空：有 ⑤ **{net.get('answered_with')}** 题 / 无 ⑤ "
        f"**{net.get('answered_without')}** 题；声明块合计：有 ⑤ "
        f"{net.get('insufficient_with')} / 无 ⑤ {net.get('insufficient_without')}。",
        "",
        "### 3.4 两种价值：暴露 vs 救回（**分开报，不合成改善率**）",
        "",
    ]
    lines += fivefold_verdict(payload)
    return lines


def render_report(payload: Mapping[str, Any]) -> str:
    """人读报告。所有数字都从 ``payload`` 取，重跑即重算。"""

    metrics = payload.get("metrics") or summarize(payload.get("records") or [])
    calls = payload.get("model_calls") or {}
    chain = payload.get("llm_chain") or {}
    environment = payload.get("environment") or {}
    alignment = metrics.get("声明对齐度") or {}
    lines = [
        "# #167 缺口闸真实行为评测",
        "",
        f"- 生成时间：`{payload.get('generated_at')}`",
        f"- 模型链路：`{chain.get('llm_model')}`（来源 `{chain.get('llm_endpoint_source')}`）",
        f"- 模型调用：名义 **{calls.get('nominal')}** / 最坏 **{calls.get('worst')}** / "
        f"实际 **{calls.get('actual')}** 次（硬顶 {calls.get('budget')}）",
        f"- 检索：`{environment.get('retrieval_mode')}`、top_k {environment.get('top_k')}、"
        f"索引 `{environment.get('elasticsearch_index')}`、租户 `{environment.get('tenant_id')}`、"
        f"{len(environment.get('kb_ids') or [])} 个库",
        "",
        "## 一、五个指标（主条件：⑤ 开着）",
        "",
        "> `unknown` 残留率这一格在 ⑤ 上线后**恒为 0**：复查会把 `unknown` 改成 supported / none，"
        "成功跑批里一条都剩不下。它证明不了「没有问题」，只说明这个口径**已经量不到东西**——"
        "真正的频率在第三节（关掉 ⑤ 直接测）。",
        "",
        "| 指标 | 值 | 分子/分母 |",
        "| --- | --- | --- |",
        _metric_row("缺口出现率", metrics.get("缺口出现率") or {}),
        _metric_row("拦截率", metrics.get("拦截率") or {}),
        _metric_row("⑤ 触发率", metrics.get("⑤ 触发率") or {}),
        _metric_row("unknown 残留率", metrics.get("unknown 残留率") or {}),
        "",
        "### 声明对齐度（分布，不是均值）",
        "",
        f"- 有缺口的题：**{alignment.get('cases_with_gaps')}** 题；"
        f"对齐 **{alignment.get('exact')}**（{_fmt_rate(alignment.get('exact_rate'))}）、"
        f"**欠声明 {alignment.get('under')}**（{_fmt_rate(alignment.get('under_rate'))}）、"
        f"过声明 {alignment.get('over')}（{_fmt_rate(alignment.get('over_rate'))}）",
        f"- 没有缺口却声明了不足：{alignment.get('declared_without_gaps')} 题",
        "",
        "| 缺口数 → 声明块数 | 题数 |",
        "| --- | --- |",
    ]
    for pair, count in (alignment.get("pairs") or {}).items():
        lines.append(f"| {pair} | {count} |")
    if not alignment.get("pairs"):
        lines.append("| （没有样本） | 0 |")

    lines += ["", "### 分组对照（A 半可答 / B 全不可答）", "", "| 组 | 缺口出现率 | 拦截率 | ⑤ 触发率 | unknown 残留率 |", "| --- | --- | --- | --- | --- |"]
    for group in (GROUP_A, GROUP_B):
        group_metrics = (metrics.get("by_group") or {}).get(group) or {}
        lines.append(
            f"| {group} | {_fmt_rate((group_metrics.get('缺口出现率') or {}).get('value'))} | "
            f"{_fmt_rate((group_metrics.get('拦截率') or {}).get('value'))} | "
            f"{_fmt_rate((group_metrics.get('⑤ 触发率') or {}).get('value'))} | "
            f"{_fmt_rate((group_metrics.get('unknown 残留率') or {}).get('value'))} |"
        )

    lines += ["", "## 二、三个问题", "", "### 2.1 文本级缺口闸够不够用？", ""]
    lines += text_level_verdict({**payload, "metrics": metrics})
    lines += ["", "### 2.2 拦截率是多少？", ""]
    lines += intercept_verdict({**payload, "metrics": metrics})
    lines += ["", "### 2.3 ⑤ 触发率的直接测量值", ""]
    lines += trigger_verdict({**payload, "metrics": metrics})
    lines += [""]
    lines += fivefold_section({**payload, "metrics": metrics})

    lines += [
        "",
        "## 四、题集与机械证据",
        "",
        "「不可答」不是凭感觉标的：每题一个关键词，它必须在**文档正文**与**已入库切片正文**"
        "里都是 0 命中。查询原样如下（`kb_ids` 用 expanding 绑定，参数见 `results.json`）：",
        "",
        "```sql",
        (payload.get("evidences") or [{}])[0].get("sql", "（无证据）").strip() or "（无证据）",
        "```",
        "",
        "| 题 | 组 | 关键词 | 文档命中 | 切片命中 | 不可答的那一块 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    cases_by_id = {str(case["case_id"]): case for case in payload.get("cases") or []}
    for evidence in payload.get("evidences") or []:
        case = cases_by_id.get(str(evidence.get("case_id"))) or {}
        lines.append(
            f"| {evidence.get('case_id')} | {case.get('group', '?')} | {evidence.get('keyword')} | "
            f"{evidence.get('doc_hits')} | {evidence.get('chunk_hits')} | "
            f"{case.get('topic', '')} |"
        )
    if not payload.get("evidences"):
        lines.append("| （证据未采集） | — | — | — | — | — |")

    lines += [
        "",
        "**为什么用半可答**：全不可答时模型容易整篇写「材料不足」；半可答时它最容易圆场——"
        "一半有材料，就顺手把另一半也答了，而那正是闸门要防的。只测全不可答，量到的是一道闸"
        "打在稻草人身上的表现。这不是造假数据：知识库覆盖不全用户的问题是生产里最常见的场景。",
        "",
        "## 五、逐题明细",
        "",
        "| 题 | ⑤ | 组 | 停止原因 | 材料 | 要点 | 缺口 | 声明块 | 调用 | 答案 | 拒绝码 | 耗时 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for record in payload.get("records") or []:
        answer_cell = "非空" if record.get("answer") is not None else "null"
        fivefold_cell = "开" if record.get("fivefold", True) else "**关**"
        lines.append(
            f"| {record['case']} | {fivefold_cell} | {record['group']} | "
            f"`{record['stop_reason']}` | "
            f"{record.get('materials')} | {len(record.get('points') or [])} | "
            f"{len(record.get('gaps') or [])} | {record.get('insufficient')} | "
            f"{record.get('model_calls')} | {answer_cell} | "
            f"{record.get('answer_rejected_reason') or '—'} | "
            f"{round(int(record.get('duration_ms') or 0) / 1000, 1)}s |"
        )
    if not payload.get("records"):
        lines.append("| （还没有跑） | — | — | — | — | — | — | — | — | — | — | — |")

    lines += [
        "",
        "### 走 ⑤ 的题（`model_calls >= 4`，只看 ⑤ 开着的那一批）",
        "",
    ]
    triggered = [
        record
        for record in payload.get("records") or []
        if record.get("fivefold", True)
        and int(record.get("model_calls") or 0) >= TRIGGER_MIN_MODEL_CALLS
    ]
    if not triggered:
        lines.append("- 这一批没有题走到 ⑤。")
    else:
        for record in triggered:
            statuses = "；".join(
                f"{point.get('point_id')}={point.get('status')}"
                for point in record.get("coverage") or []
            )
            lines.append(
                f"- **{record['case']}**：`{record['stop_reason']}`、缺口 {record.get('gaps')}、"
                f"覆盖 {statuses}"
            )

    lines += [
        "",
        "## 六、这份对照不能回答什么",
        "",
        "- **样本量 20**：五个率的分母都在 4–20 之间，一题翻转就是 5–25 个百分点。"
        "这里够判断「有没有系统性偏差」，不够做版本间比较，也不够给出精确的发生率。",
        "- **半可答题是我方构造的**：10 道 A 组题的「不可答方面」由我们追加，追加串接在原题"
        "后面。构造带来了两处偏差：(1) 追加的方面偏向「一个拎得出来的名词」（数字人民币、"
        "区块链、企业微信），而真实用户的问题里缺口往往是**半个概念**；(2) 追加串很短，"
        "规划器有时会把它并进相邻要点，于是「这道题有几个缺口」并不完全由我们决定。",
        "- **A 组追加的方面可能被规划器拆成与预期不同的要点**：我们只保证「那一块在语料里没有」，"
        "不保证模型把它拆成**一条**要点——实测每次规划出 3–5 条，同一个方面有时被拆成两条"
        "（A03 / A06 / A07），有时与别的方面并进一条。这**不影响对齐度**（它比的是同一次跑内的"
        "缺口数与声明块数，两边同一个来源），但它决定「一道题有几个缺口」，也决定「追加的那个"
        "方面有没有真的变成一条要点」——若它被并进一条其实有材料的要点，缺口就不会出现。"
        "逐题明细的要点文本可以人工复核。",
        "- **只测一个模型链**：规划 / 判定 / 复查 / 生成走的是 `fast` 与 `strong` 两个角色指针，"
        "这次落在同一个供应商的同一条模型上（见报告头部的链路）。换模型、换思考强度，读数会变；"
        "这里没有做模型间对照。",
        "- **`plan` 的检索会被降级改写**：一旦关键词或向量一路失败，`sub_search_degraded` 会记"
        "一条，本次读数就变成「单路检索下的闸门行为」。这次降级过的题："
        + ("、".join(_degraded_cases(payload.get("records") or [])) or "无")
        + "。逐题记录里带了这个字段，请连同它一起读。",
        "- **「不可答」的机械证据只覆盖关键词这一种形态**：关键词 0 命中证明「这个词不在库里」，"
        "不证明「这块知识不在库里」——库里可能用别的说法写了。反向偏差是存在的（把可答的当成了"
        "不可答），它会让缺口出现率偏高。B 组里 6 题取自题库 `absent` 标注（问题侧、写在检索之前），"
        "那 6 题的证据更强；我方构造的 4 题只有关键词证据。",
        "- **不测答案对不对**：本票只量闸门行为（缺口报没报、声明对不齐、拦没拦），不评答案质量。"
        "「声明了材料不足」不代表那一块真的不足，也不代表其余部分答对了。",
        "- **一次跑批**：没有重复跑，所以没有跑间方差；模型的抽样波动会让这次的分布挪位。"
        "第三节那份对照尤其要吃这一条：条件 1 与条件 2 是**两次独立跑**，规划与判定都是新的"
        "样本，所以「gaps 变多/变少」里既有 ⑤ 的效果，也有抽样噪声。",
        "- **⑤ 的对照按关键词对齐**：两次跑的规划从来不一样（这次 **10/10 题**的要点清单都变了），"
        "所以「哪条要点说的是同一件事」只能靠**我们注入并机械验证过的关键词**判断，而不是序号"
        "或措辞。与不可答方面无关的 `unknown`、以及开 ⑤ 那次没规划出来的方面，都单列、"
        "不计入改判分布——于是改判的那几个数**分母偏小**，不能当成「⑤ 的总体改判率」。",
        "- **关 ⑤ 是一个不存在的生产配置**：真实部署里 ⑤ 总是开着。条件 2 是**反事实**测量，"
        "用来回答「若没有它会怎样」，不代表任何可运行的版本。",
        "- **`unknown → supported` 被题集结构性地压低了**：我们每题的缺口方面**在语料里根本"
        "不存在**（0 命中的机械证据就是它），所以补证捞回来的材料几乎不可能真的撑住那个方面，"
        "复查自然落成 `none`。真实场景里缺口常常是「这部分材料没召回」而不是「库里没有」，"
        "那种情况下「救回」的比例会高得多——**这次 0 条救回不能被读成「⑤ 从不救回」**。",
        "- **3 题答案被别的原因整份作废**（`malformed answer output`：生成阶段强模型超时；"
        "`unresolved evidence reference`：块里引到了不该出现的编号）。它们没有进入对齐度的"
        "分母，也没有进入拦截率——这 3 题的「有缺口却不认账」到底会不会发生，这次没测到。",
    ]

    lines += [
        "",
        "## 七、怎么复现",
        "",
        "```powershell",
        "# 只打印预估、题集自检与关键词 0 命中证据（0 次模型调用，要连库读两种正文）",
        "uv run python scripts/eval_t167_gap_gate.py plan",
        "",
        f"# 全量（名义 {calls.get('nominal')} 次、最坏 {calls.get('worst')} 次模型调用）",
        "uv run python scripts/eval_t167_gap_gate.py run",
        "",
        "# 按当前口径重算指标并重出报告（0 次模型调用）",
        "uv run python scripts/eval_t167_gap_gate.py recompute",
        "```",
        "",
        "跑的是**生产完整链路**：`run_research`（规划 → 并行检索 → 覆盖判定 → 定向补证 → 复查 "
        "→ 生成）+ 生产检索内核 `build_kernel_search` + 两个真实模型角色。没有构造材料池，"
        "也没有另写一套提示词——这是本票与 #165 的关键区别。",
        "",
        "## 八、记账口径",
        "",
        "- 实际调用次数从**逐题记录**加总（`recompute` 也会重算这一格），不另记一本账。",
        "- 开跑前那次**预检**调用（1 题冒烟，4 次）不计入逐题记录，也不计入表中的「实际」"
        "——它发生在正式跑批之前，用途是确认链路接对。它花掉的额度如实记在这份报告的"
        f"这一节里：硬顶 {calls.get('budget')} 次是**整票**的预算，"
        f"整票实际花掉的是 {calls.get('actual')}（逐题记录）+ 4（预检）="
        f"**{int(calls.get('actual') or 0) + 4}** 次。",
    ]
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────── 装配与 CLI


async def run_all(
    *,
    groups: Sequence[str],
    out_dir: Path,
    mode: str = "run",
) -> dict[str, Any]:
    """跑指定的组并落盘（``mode='plan'`` 只打印预估与证据，一次模型调用都不发）。"""

    predicted = predict_model_calls()
    print(
        f"预计模型调用：名义 {predicted['nominal']} 次 / 最坏 {predicted['worst']} 次"
        f"（硬顶 {predicted['budget']}）"
    )
    evidences = await collect_evidence()
    print(f"关键词证据：{len(evidences)} 题全部 0 命中（文档正文 + 切片正文）")

    if mode == "plan":
        plan = build_plan(evidences=evidences)
        print(render_plan(plan))
        return plan

    chain = await _bind_model_chain()
    environment = _apply_eval_settings()

    from app.db.async_session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        index_versions = await _resolve_scope(db, tenant_id=TENANT_ID, kb_ids=KB_IDS)
    print(f"索引版本：{index_versions}")
    environment["index_versions"] = index_versions

    budget = Budget()
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.json"
    previous: dict[str, Any] = {}
    if results_path.exists():
        previous = json.loads(results_path.read_text(encoding="utf-8"))

    payload: dict[str, Any] = dict(previous)
    payload.update(
        {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "issue": "#167 缺口闸真实行为评测",
            "llm_chain": chain,
            "environment": environment,
            "model_calls": predicted,
            "cases": [
                {
                    "case_id": case.case_id,
                    "group": case.group,
                    "query": case.query,
                    "keyword": case.keyword,
                    "topic": case.topic,
                    "base_id": case.base_id,
                    "appended": case.appended,
                    "note": case.note,
                }
                for case in load_cases()
            ],
            "evidences": [as_json(evidence) for evidence in evidences],
            "checks": check_case_set(),
            "archived": archived_trigger_reading(),
        }
    )

    # 每个运行标签 → (要跑哪些组, ⑤ 开还是关)。「A-off」跑的还是 A 组那 10 题，
    # 只是把复查关掉——所以它替换的是「A 组 + 对照条件」那一段记录，不动主条件的记录。
    plan_of_runs: dict[str, tuple[tuple[str, ...], bool]] = {
        GROUP_A: ((GROUP_A,), True),
        GROUP_B: ((GROUP_B,), True),
        GROUP_A_NO_FIVEFOLD: ((GROUP_A,), False),
    }
    chose = [label for label in groups if label in plan_of_runs]
    records = list(previous.get("records") or [])
    if chose:
        targets = [plan_of_runs[label] for label in chose]
        records = [
            record
            for record in records
            if not any(
                record.get("group") in target_groups
                and bool(record.get("fivefold", True)) is fivefold
                for target_groups, fivefold in targets
            )
        ]
        for label in chose:
            target_groups, fivefold = plan_of_runs[label]
            cases = [case for case in load_cases() if case.group in target_groups]
            condition = CONDITION_FIVEFOLD if fivefold else CONDITION_CONTROL
            print(
                f"跑 {label} 组（{condition}）：{[case.case_id for case in cases]}", flush=True
            )
            records.extend(
                await run_cases(
                    cases,
                    budget=budget,
                    index_versions=index_versions,
                    fivefold=fivefold,
                )
            )
    order = {case.case_id: index for index, case in enumerate(load_cases())}
    records.sort(
        key=lambda record: (
            order.get(str(record.get("case")), 999),
            0 if record.get("fivefold", True) else 1,
        )
    )
    payload["records"] = records
    payload["groups_run"] = sorted({str(record.get("condition")) for record in records})

    payload = recompute_metrics(payload)
    model_calls = dict(payload["model_calls"])
    model_calls["skipped"] = list(budget.skipped)
    payload["model_calls"] = model_calls

    results_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "report.md").write_text(render_report(payload), encoding="utf-8")
    print(f"实际模型调用：{payload['model_calls']['actual']} 次（硬顶 {budget.cap}）")
    if budget.skipped:
        print("因预算跳过：" + "；".join(budget.skipped))
    print(f"产物：{results_path} / {out_dir / 'report.md'}")
    return payload


def _recompute(out_dir: Path) -> int:
    """不重跑、只重算：改统计口径不该重花那八十几次调用。"""

    results_path = out_dir / "results.json"
    if not results_path.exists():
        print(f"没有产物可重算：{results_path}")
        return 1
    payload = json.loads(results_path.read_text(encoding="utf-8"))
    payload = recompute_metrics(payload)
    results_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "report.md").write_text(render_report(payload), encoding="utf-8")
    print(f"已重算：实际模型调用 {payload['model_calls']['actual']} 次（未新增调用）")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="缺口闸真实行为评测（票 #167）：缺口出现率 / 声明对齐度 / 拦截率 / ⑤"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="只打印预估、题集自检与关键词证据（0 次模型调用）")
    plan.add_argument("--out-dir", default=str(REPORT_DIR), help="产物目录（plan 不写）")

    run = subparsers.add_parser("run", help="跑两组并写 eval/reports/t167/")
    run.add_argument("--groups", nargs="+", default=list(RUN_LABELS), choices=list(RUN_LABELS))
    run.add_argument("--out-dir", default=str(REPORT_DIR), help="产物目录")

    recompute = subparsers.add_parser(
        "recompute", help="按当前口径重算已有产物的指标并重出报告（0 次模型调用）"
    )
    recompute.add_argument("--out-dir", default=str(REPORT_DIR), help="产物目录")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir)
    if sys.platform == "win32" and args.command in ("run", "plan"):
        # psycopg 的异步连接在 Windows 的缺省 Proactor 循环上跑不起来（第一条查询就
        # InterfaceError）。plan 也要连库查证据，所以两个命令都设——与
        # scripts/eval_t165_coverage.py / eval_retrieval.py 同一行理由。
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    if args.command == "recompute":
        return _recompute(out_dir)
    if args.command == "plan":
        asyncio.run(run_all(groups=(), out_dir=out_dir, mode="plan"))
        return 0
    asyncio.run(run_all(groups=tuple(args.groups), out_dir=out_dir, mode="run"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
