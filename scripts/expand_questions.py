"""把新题追加进 eval/questions.json，并校验每个金标章节真实存在。

参考开源基准的题型框架补题，主要借用 RGB（Chen et al., "Benchmarking LLMs in RAG"）
的四个子任务：噪声鲁棒、负例拒答、信息整合、反事实鲁棒；再补上表格数值与否定式提问
两类实际业务里高频、但公开基准少见的问法。

用法：uv run python scripts/expand_questions.py [--dry-run]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
QUESTIONS_PATH = REPO_ROOT / "eval" / "questions.json"
CORPUS_DIR = REPO_ROOT / "eval" / "corpus"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eval_chunking import _load_corpus, _sections  # noqa: E402

E = "售后故障与错误码速查"  # support_errors
M = "终端型号与配件指南"  # support_models
R = "退款与售后受理规则"  # support_refund
P = "产品规格说明书"  # product_spec
B = "计费与账务政策"  # billing_policy
S = "安全合规与审计说明"  # security_compliance
RUN = "值班处置手册"  # ops_runbook
C = "发布与变更管理规范"  # ops_change

NEW_QUESTIONS: list[dict[str, object]] = [
    # ── noise_robust：同族章节连着排，正文高度相似，只有编号与结论不同 ──
    {
        "type": "noise_robust",
        "query": "E1026 是什么错误，怎么处理",
        "relevant": [("support_errors", f"{E} > 连接与证书类错误（三）")],
        "note": "同族八节正文 90% 相同，靠 certificate name mismatch 这串字分辨。",
        "trap": [("support_errors", f"{E} > 连接与证书类错误（二）")],
    },
    {
        "type": "noise_robust",
        "query": "E1027 报 unable to get local issuer certificate 是怎么回事",
        "relevant": [("support_errors", f"{E} > 连接与证书类错误（四）")],
        "note": "缺中间证书。与「证书过期」语义接近，容易串到（一）。",
        "trap": [("support_errors", f"{E} > 连接与证书类错误（一）")],
    },
    {
        "type": "noise_robust",
        "query": "E1040 证书链太长超出固件解析上限怎么解决",
        "relevant": [("support_errors", f"{E} > 连接与证书类错误（六）")],
        "note": "「链太长」与「链不完整」是相反方向，考细分。",
        "trap": [("support_errors", f"{E} > 连接与证书类错误（四）")],
    },
    {
        "type": "noise_robust",
        "query": "E1041 签名算法不支持",
        "relevant": [("support_errors", f"{E} > 连接与证书类错误（七）")],
        "note": "处置是先升级固件，不是续期——与同族其它节的最简答案不同。",
        "trap": [("support_errors", f"{E} > 连接与证书类错误（五）")],
    },
    {
        "type": "noise_robust",
        "query": "E1048 证书串号与资产台账不一致",
        "relevant": [("support_errors", f"{E} > 连接与证书类错误（八）")],
        "note": "末位章节，与首节距离最远，考长文档内的精确命中。",
        "trap": [("support_errors", f"{E} > 连接与证书类错误（一）")],
    },
    {
        "type": "noise_robust",
        "query": "E2049 同一台设备并发会话数超限",
        "relevant": [("support_errors", f"{E} > 配额与限流类错误（二）")],
        "note": "限流族六节结构一致只有编号与结论不同。",
        "trap": [("support_errors", f"{E} > 配额与限流类错误（一）")],
    },
    {
        "type": "noise_robust",
        "query": "E2051 当日累计调用量用尽每日上限",
        "relevant": [("support_errors", f"{E} > 配额与限流类错误（四）")],
        "note": "「每日上限」与「分钟级限流」结论不同（查重复投递 vs 加退避）。",
        "trap": [("support_errors", f"{E} > 配额与限流类错误（一）")],
    },
    {
        "type": "noise_robust",
        "query": "E2053 多租户共享池被别的租户吃满",
        "relevant": [("support_errors", f"{E} > 配额与限流类错误（六）")],
        "note": "结论是申请独立配额，与同族「加退避」不同。",
        "trap": [("support_errors", f"{E} > 配额与限流类错误（五）")],
    },
    # ── counterfactual：同代多档 + 新旧规则叠加，问「哪一个 / 怎么算」 ──
    {
        "type": "counterfactual",
        "query": "上一代的主力机型是哪个型号",
        "relevant": [("support_models", f"{M} > 整机规格（标准款）")],
        "note": "三款章节都以「面向…」开头，只有标准款自称「上一代主力机型」。",
        "trap": [("support_models", f"{M} > 整机规格（增强款）")],
    },
    {
        "type": "counterfactual",
        "query": "增强款与标准款外观一致吗，内部差在哪",
        "relevant": [("support_models", f"{M} > 整机规格（增强款）")],
        "note": "「外观一致但内部不同」只在增强款一节写明。",
        "trap": [("support_models", f"{M} > 整机规格（标准款）")],
    },
    {
        "type": "counterfactual",
        "query": "机架款的万兆光口出厂带光模块吗",
        "relevant": [("support_models", f"{M} > 整机规格（机架款）")],
        "note": "答案是「另配、不附带」，考否定细节而非型号参数本身。",
        "trap": [("product_spec", f"{P} > 端口定义")],
    },
    {
        "type": "counterfactual",
        "query": "维修过的设备保修期是从头算还是沿用原来的",
        "relevant": [("support_refund", f"{R} > 保修范围")],
        "note": "同一节里三条规则并列：整机新算、换货重新算、维修沿用原期。",
        "trap": [("support_refund", f"{R} > 换货流程")],
    },
    # ── table_lookup：数值/时长，靠精确数字定位 ──
    {
        "type": "table_lookup",
        "query": "标准款整机转发吞吐大概多少",
        "relevant": [("product_spec", f"{P} > 性能指标（一）吞吐")],
        "note": "900 Mbps。同节还有开启 VPN / QoS 的衰减比例。",
    },
    {
        "type": "table_lookup",
        "query": "增强款的会话表容量是多少条",
        "relevant": [("product_spec", f"{P} > 性能指标（二）并发会话")],
        "note": "10 万条。三款容量在同一句里并列，考能否定位到增强款那一档。",
    },
    {
        "type": "table_lookup",
        "query": "会话老化时间默认多少秒",
        "relevant": [("product_spec", f"{P} > 性能指标（二）并发会话")],
        "note": "300 秒，且 TCP 与 UDP 分别计时。",
    },
    {
        "type": "table_lookup",
        "query": "标准款整机尺寸和重量是多少",
        "relevant": [("product_spec", f"{P} > 尺寸与重量（标准款）")],
        "note": "160×110×30 毫米、420 克；与机架款一节同构。",
    },
    {
        "type": "table_lookup",
        "query": "审计日志要留存多久",
        "relevant": [("security_compliance", f"{S} > 日志留存")],
        "note": "12 个月。同节并列访问日志 6 个月、调试日志 7 天，考第几档。",
    },
    {
        "type": "table_lookup",
        "query": "变更审批记录保存多长时间",
        "relevant": [("ops_change", f"{C} > 变更审批")],
        "note": "12 个月，与审计日志的 12 个月数字相同但归属不同文档。",
    },
    {
        "type": "table_lookup",
        "query": "P2 告警的响应时限是多少",
        "relevant": [("ops_runbook", f"{RUN} > 告警分级与响应时限")],
        "note": "30 分钟。P1 是 15 分钟，两级并列。",
    },
    # ── negative_scope：问「哪些不…」，答案藏在排除句里 ──
    {
        "type": "negative_scope",
        "query": "哪些配件绝对不能跨型号混用",
        "relevant": [("support_models", f"{M} > 配件兼容性（一）")],
        "note": "三条规则里只有电源适配器不能混用，网线与壁挂件反而通用。",
    },
    {
        "type": "negative_scope",
        "query": "备份链路不承载哪类业务",
        "relevant": [("product_spec", f"{P} > 蜂窝与备份链路")],
        "note": "答案是「视频类大流量业务」，同节只说承载管理与关键业务。",
    },
    {
        "type": "negative_scope",
        "query": "保修不覆盖哪些情况",
        "relevant": [("support_refund", f"{R} > 保修范围")],
        "note": "进水、跌落、私自拆机、电压误接四项排除。",
    },
    {
        "type": "negative_scope",
        "query": "哪一级变更不需要主管审批",
        "relevant": [("ops_change", f"{C} > 变更分级")],
        "note": "三级由值班确认、四级自助；一二级才要审批。",
    },
    # ── synonym：换词问，关键词不在文档里 ──
    {
        "type": "synonym",
        "query": "钱什么时候能退回来",
        "relevant": [("support_refund", f"{R} > 退款到账时间")],
        "note": "「退回来」对「到账」，且要区分到账从执行日算而非申请日。",
    },
    {
        "type": "synonym",
        "query": "机器坏了想寄回去修，要走什么流程",
        "relevant": [("support_refund", f"{R} > 维修流程")],
        "note": "「寄回去修」对「寄修」，前置动作是导出配置并解绑。",
    },
    {
        "type": "synonym",
        "query": "押金怎么拿回来",
        "relevant": [("support_refund", f"{R} > 押金退还")],
        "note": "押金退还不走退款审核队列，由资产部门处理。",
    },
    {
        "type": "synonym",
        "query": "旧机器能抵钱换新的吗",
        "relevant": [("support_refund", f"{R} > 以旧换新")],
        "note": "「抵钱」对「估价抵扣」，且不产生现金退款。",
    },
    # ── colloquial：口语化，不含文档术语 ──
    {
        "type": "colloquial",
        "query": "磁盘快满了怎么救急",
        "relevant": [("ops_runbook", f"{RUN} > 磁盘写满应急处置")],
        "note": "「救急」对「应急处置」，且要点是先清导出文件与滚动日志。",
    },
    {
        "type": "colloquial",
        "query": "上线要不要两个人一起看",
        "relevant": [("ops_change", f"{C} > 变更分级")],
        "note": "答案是「一级变更需双人复核」，需要把口语映射到变更分级。",
    },
    {
        "type": "colloquial",
        "query": "队列堆住了怎么办",
        "relevant": [("ops_runbook", f"{RUN} > 消息队列积压")],
        "note": "先分容量问题还是消费者问题，且不能清空队列。",
    },
    {
        "type": "colloquial",
        "query": "缓存突然不生效了数据库快扛不住",
        "relevant": [("ops_runbook", f"{RUN} > 缓存击穿与降级")],
        "note": "「不生效 + 数据库压力」指向缓存击穿，要分热点键过期与实例故障。",
    },
    # ── cross_section：答案跨两节，缺一节就答不全 ──
    {
        "type": "cross_section",
        "query": "平台密钥多久轮换一次，轮换时要注意什么",
        "relevant": [
            ("security_compliance", f"{S} > 密钥轮换"),
            ("ops_runbook", f"{RUN} > 证书轮换操作步骤"),
        ],
        "note": "周期在安全文档、操作与灰度比例在值班手册，两节都要。",
    },
    {
        "type": "cross_section",
        "query": "退款一直没到账该怎么查",
        "relevant": [
            ("support_refund", f"{R} > 退款到账时间"),
            ("support_refund", f"{R} > 退款到账异常排查"),
        ],
        "note": "先说承诺时效，再说四步排查，同一文档两节。",
    },
    {
        "type": "cross_section",
        "query": "变更窗口是什么时候，窗口结束前要做什么",
        "relevant": [
            ("ops_change", f"{C} > 变更窗口"),
            ("ops_change", f"{C} > 变更后验证"),
        ],
        "note": "窗口时间与验证三层分别在两节。",
    },
    {
        "type": "cross_section",
        "query": "设备成片离线怎么排查，什么时候要升级",
        "relevant": [
            ("ops_runbook", f"{RUN} > 设备离线批量排查"),
            ("ops_runbook", f"{RUN} > 升级与通报路径"),
        ],
        "note": "排查顺序与升级阈值分属两节。",
    },
    # ── absent：答案确实不存在，正确行为是不返回 ──
    {
        "type": "absent",
        "query": "支持刷脸支付吗",
        "relevant": [],
        "note": "语料没有支付方式相关章节，答案不存在。",
    },
    {
        "type": "absent",
        "query": "有没有手机 App 可以管理设备",
        "relevant": [],
        "note": "只有管理后台与工单入口，没有任何 App 相关内容。",
    },
    {
        "type": "absent",
        "query": "能开香港公司的发票吗",
        "relevant": [],
        "note": "发票章节只讲普票与专票，跨境章节讲形式发票但不涉及香港开票。",
    },
    # ── rare_token：极冷门字符串，只有一字不差才能命中 ──
    {
        "type": "rare_token",
        "query": "sha256:3b7e1f9d24c8a6e0b5f7c1d9e3a80642 是哪批固件的核对基准",
        "relevant": [("security_compliance", f"{S} > 固件签名核对（二）")],
        "note": "与（一）的 sha256:9f2a… 只差前缀，考长串精确匹配。",
        "trap": [("security_compliance", f"{S} > 固件签名核对（一）")],
    },
    {
        "type": "rare_token",
        "query": "PRJ-9QX8B3 的验收标准是什么",
        "relevant": [("security_compliance", f"{S} > 内部专项计划（二）")],
        "note": "与 PRJ-4K7T2M 编号形状一致，靠字母数字区分。",
        "trap": [("security_compliance", f"{S} > 内部专项计划（一）")],
    },
    # ── model_number：型号后缀只差一点 ──
    {
        "type": "model_number",
        "query": "XR-2100 最大能接多少终端",
        "relevant": [("support_models", f"{M} > 整机规格（机架款）")],
        "note": "512 台。与 XR-2000 / XR-2000 Pro 的 64 / 256 并列。",
        "trap": [("support_models", f"{M} > 整机规格（增强款）")],
    },
    {
        "type": "model_number",
        "query": "XR-2000 的内存多大",
        "relevant": [("support_models", f"{M} > 整机规格（标准款）")],
        "note": "2 GB。增强款 4 GB、机架款 16 GB。",
        "trap": [("support_models", f"{M} > 整机规格（增强款）")],
    },
    {
        "type": "model_number",
        "query": "哪一款支持 Wi-Fi 6",
        "relevant": [("support_models", f"{M} > 整机规格（增强款）")],
        "note": "标准款只有 Wi-Fi 5；考「哪个型号具备」而不是「参数是多少」。",
        "trap": [("support_models", f"{M} > 整机规格（标准款）")],
    },
]


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    existing = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    questions: list[dict[str, object]] = existing["questions"]
    known_ids = {item["id"] for item in questions}

    # 幂等保护：按 query 去重。这脚本是「一次性扩充工具」，但误跑第二次不该
    # 把同一批题再加一遍——题目重复会让宏平均被同一道题多算几次。
    known_queries = {str(item["query"]) for item in questions}
    pending = [item for item in NEW_QUESTIONS if str(item["query"]) not in known_queries]
    already = len(NEW_QUESTIONS) - len(pending)
    if already:
        print(f"跳过 {already} 道已存在的题（按 query 去重）")
    if not pending:
        print(f"没有新题可加，当前总数 {len(questions)}")
        return 0

    # 校验金标章节全部真实存在，避免标注写错导致「永远命不中」
    sections_by_doc = {
        doc.stem: {s.heading_path for s in _sections(doc.text)} for doc in _load_corpus()
    }
    problems: list[str] = []
    for item in pending:
        for document, heading_path in list(item["relevant"]) + list(item.get("trap") or []):
            if document not in sections_by_doc:
                problems.append(f"未知文档 {document}")
            elif heading_path not in sections_by_doc[document]:
                problems.append(f"章节不存在：{document} / {heading_path}")
    if problems:
        print("金标校验失败：")
        for problem in sorted(set(problems)):
            print("  -", problem)
        return 1

    next_index = len(questions) + 1
    added: list[dict[str, object]] = []
    for offset, item in enumerate(pending):
        question_id = f"q{next_index + offset:02d}"
        assert question_id not in known_ids
        added.append(
            {
                "id": question_id,
                "type": item["type"],
                "query": item["query"],
                "relevant": [
                    {"document": document, "heading_path": heading_path}
                    for document, heading_path in item["relevant"]
                ],
                **(
                    {
                        "trap": [
                            {"document": document, "heading_path": heading_path}
                            for document, heading_path in item["trap"]
                        ]
                    }
                    if item.get("trap")
                    else {}
                ),
                "note": item["note"],
            }
        )

    print(f"新增 {len(added)} 道，总数 {len(questions)} → {len(questions) + len(added)}")
    counts: dict[str, int] = {}
    for item in questions + added:
        counts[str(item["type"])] = counts.get(str(item["type"]), 0) + 1
    for key in sorted(counts):
        print(f"  {key:16s} {counts[key]}")

    if dry_run:
        print("（dry-run，未写文件）")
        return 0

    existing["questions"] = questions + added
    QUESTIONS_PATH.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"已写入 {QUESTIONS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
