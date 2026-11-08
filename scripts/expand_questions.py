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
from app.utils.markdown_splitter import MarkdownStructuredSplitter  # noqa: E402
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
    # ══════════════════════════════════════════════════════════════════
    # 第二批：语料扩到 15 篇后补的题型。三件事一起做——
    # ① 补 billing_policy（此前 3.4K 字文档零覆盖）；
    # ② 给七篇不同**类型**的新文档出题（价格表 / SLA / FAQ / 复盘 / 发布说明 /
    #    入职指引 / 会议纪要），切块策略对这些文体的敏感度高于普通说明文；
    # ③ 补 `false_premise`（借 CRAG 的题型）与更多跨文档多跳（借 MultiHop-RAG）。
    # ══════════════════════════════════════════════════════════════════
    # ── billing_policy：补齐此前零覆盖的一整篇文档 ──
    {
        "type": "table_lookup",
        "query": "每月账单是哪天出账的",
        "relevant": [("billing_policy", f"{B} > 计费周期")],
        "note": "次月 3 日。同节还有出账后 5 个工作日内扣款。",
    },
    {
        "type": "table_lookup",
        "query": "发票在付款完成后几天内开具",
        "relevant": [("billing_policy", f"{B} > 发票开具时限")],
        "note": "5 个工作日。与「出账后 5 个工作日扣款」是同一数字、不同含义。",
    },
    {
        "type": "negative_scope",
        "query": "抵扣券可以叠加使用吗",
        "relevant": [("billing_policy", f"{B} > 抵扣券使用")],
        "note": "不找零、不兑现、不叠加，单笔只能用一张。",
    },
    {
        "type": "synonym",
        "query": "退回来的钱能取现吗",
        "relevant": [("billing_policy", f"{B} > 退费申请流程")],
        "note": "「取现」对「提现」，退费只进账户余额，提现要单独申请。",
    },
    {
        "type": "colloquial",
        "query": "欠钱了会马上给停掉吗",
        "relevant": [("billing_policy", f"{B} > 欠费与停机")],
        "note": "账单到期后 7 天标欠费、欠费后 3 天停机、停机保留 15 天，三个数字要分清。",
    },
    {
        "type": "noise_robust",
        "query": "退费和退款到底有什么区别",
        "relevant": [("billing_policy", f"{B} > 退费与退款的区别")],
        "note": "退费进余额、退款回原渠道；两个词在语料里高频混用，专设一节辨析。",
        "trap": [("support_refund", f"{R} > 退款到账时间")],
    },
    {
        "type": "counterfactual",
        "query": "升配和降配分别什么时候生效",
        "relevant": [("billing_policy", f"{B} > 套餐变更")],
        "note": "升配即时生效补差价，降配下个周期生效且当期不退——同一节里两个相反规则。",
    },
    {
        "type": "cross_section",
        "query": "觉得账单金额算错了，怎么申诉、多久内要提",
        "relevant": [
            ("billing_policy", f"{B} > 计费争议"),
            ("billing_policy", f"{B} > 账单出账时间"),
        ],
        "note": "争议时限与出账时间分属两节，缺一节就答不全。",
    },
    # ── price_list：表格密集，考「从并列的数字里挑对那一档」 ──
    {
        "type": "table_lookup",
        "query": "机架款 XR-2100 的指导价是多少",
        "relevant": [("price_list", "价格与折扣对照表 > 整机指导价")],
        "note": "9600 元。三款指导价在同一张表里，且都有套装价干扰。",
    },
    {
        "type": "table_lookup",
        "query": "万兆光模块多少钱一个",
        "relevant": [("price_list", "价格与折扣对照表 > 配件价格")],
        "note": "1180 元。同表还有千兆光模块 320 元，是两个不同配件。",
    },
    {
        "type": "noise_robust",
        "query": "一次订 60 台能打几折",
        "relevant": [("price_list", "价格与折扣对照表 > 阶梯折扣")],
        "note": "9 折。阶梯表五档相邻，60 台落在 50-199 档，容易串到相邻档。",
    },
    {
        "type": "negative_scope",
        "query": "阶梯折扣能和促销价一起用吗",
        "relevant": [("price_list", "价格与折扣对照表 > 阶梯折扣")],
        "note": "互斥，系统自动取更优的一种。",
    },
    # ── sla_terms：条款型，数值与免责范围 ──
    {
        "type": "table_lookup",
        "query": "企业版的月度可用性承诺是多少",
        "relevant": [("sla_terms", "服务等级协议条款 > 可用性承诺")],
        "note": "99.9%。四档并列，且赔偿比例与档位一一对应。",
    },
    {
        "type": "noise_robust",
        "query": "计划内维护算不算不可用时长",
        "relevant": [("sla_terms", "服务等级协议条款 > 常见争议")],
        "note": "判据是「有没有提前 48 小时通知」，不是维护本身。这条只写在争议一节。",
        "trap": [("sla_terms", "服务等级协议条款 > 可用性承诺")],
    },
    {
        "type": "negative_scope",
        "query": "哪些情况不计入不可用时长",
        "relevant": [("sla_terms", "服务等级协议条款 > 免责情形")],
        "note": "六类免责情形，与「可用性承诺」一节的排除项互补。",
    },
    # ── faq_short：问答对文体，短句密集 ──
    {
        "type": "colloquial",
        "query": "密码忘了进不去管理页面怎么办",
        "relevant": [("faq_short", "高频问答速查 > 设备与网络")],
        "note": "FAQ 每节五问，问题都短，考「短问句在长文档里怎么被召回」。",
    },
    {
        "type": "synonym",
        "query": "货到了再付钱行不行",
        "relevant": [("faq_short", "高频问答速查 > 下单与发货")],
        "note": "「货到再付钱」对「货到付款」，且答案是「对公可申请、个人不支持」。",
    },
    # ── incident_report：时间线叙事，因果链 ──
    {
        "type": "table_lookup",
        "query": "3 月 14 日那次故障峰值掉了多少台设备",
        "relevant": [("incident_report", "故障复盘报告 > 影响范围")],
        "note": "2300 台、占 18%。与摘要里的「约 2300 台」呼应，需区分峰值与总数。",
    },
    {
        "type": "cross_section",
        "query": "会话配额隔离什么时候完成、谁负责",
        "relevant": [
            ("incident_report", "故障复盘报告 > 改进项与负责人"),
            ("incident_report", "故障复盘报告 > 根因分析"),
        ],
        "note": "改进项在表格里、动机在根因一节，两节合起来才是完整答案。",
    },
    {
        "type": "counterfactual",
        "query": "上次同类事件为什么没能防住这次故障",
        "relevant": [("incident_report", "故障复盘报告 > 根因分析")],
        "note": "根因一节点明「只做了临时扩容、没有形成机制」，是本次复发的直接教训。",
        "trap": [("incident_report", "故障复盘报告 > 事件摘要")],
    },
    # ── release_notes：版本对比，反事实的最佳素材 ──
    {
        "type": "counterfactual",
        "query": "双机热备的快速切换是哪个版本开始有的",
        "relevant": [("release_notes", "版本发布说明 > v5.1.0（上一版）")],
        "note": "v5.1.0。当前版是 v5.2.0，容易答成「当前版本」。",
        "trap": [("release_notes", "版本发布说明 > v5.2.0（当前版本）")],
    },
    {
        "type": "negative_scope",
        "query": "标准款支持双机热备吗",
        "relevant": [("release_notes", "版本发布说明 > v5.1.0（上一版）")],
        "note": "明确写「标准款在 5.1.0 之后仍然不支持」，与「整机规格」一节互证。",
        "trap": [("support_models", f"{M} > 整机规格（增强款）")],
    },
    {
        "type": "noise_robust",
        "query": "默认会话老化时间在 5.2.0 改成多少了",
        "relevant": [("release_notes", "版本发布说明 > v5.2.0（当前版本）")],
        "note": "240 秒。product_spec 里写的是默认 300 秒，两处数字不同——考「哪个是最新的」。",
        "trap": [("product_spec", f"{P} > 性能指标（二）并发会话")],
    },
    {
        "type": "counterfactual",
        "query": "从 v4.x 能直接升到 v5.x 吗",
        "relevant": [("release_notes", "版本发布说明 > 升级路径与兼容性")],
        "note": "不能，必须先升 v5.0.0 过渡版本；且跨大版本升级会清空自定义配置。",
        "trap": [("release_notes", "版本发布说明 > v4.x 与 v5.x 的差异")],
    },
    # ── onboarding：流程清单，带审批人表格 ──
    {
        "type": "table_lookup",
        "query": "平台只读账号由谁审批",
        "relevant": [("onboarding", "新员工入职与权限指引 > 账号开通清单")],
        "note": "团队负责人。六项账号的审批人各不相同，只有第 1 项无需审批。",
    },
    {
        "type": "negative_scope",
        "query": "实习期能申请可写权限吗",
        "relevant": [("onboarding", "新员工入职与权限指引 > 常见问题")],
        "note": "不能，实习期只能拿只读。这条只在 FAQ 一节里。",
    },
    {
        "type": "cross_section",
        "query": "转正考核要过哪几项",
        "relevant": [
            ("onboarding", "新员工入职与权限指引 > 转正与考核"),
            ("onboarding", "新员工入职与权限指引 > 权限申请流程"),
        ],
        "note": "考核三项在考核节，而「独立完成标准变更」需要可写权限，两者相关。",
    },
    # ── meeting_minutes：非结构化对话，结论藏在讨论里 ──
    {
        "type": "counterfactual",
        "query": "证书台账最后由哪个系统作为主维护",
        "relevant": [("meeting_minutes", "架构评审会议纪要 > 议题一：证书台账合并")],
        "note": "资产系统为主、安全组补丁留痕。节内有争议过程，结论在段末。",
    },
    {
        "type": "negative_scope",
        "query": "调试日志的留存时间延长了吗",
        "relevant": [("meeting_minutes", "架构评审会议纪要 > 议题二：日志留存策略调整")],
        "note": "没有延长，仍是 7 天；改为新增脱敏摘要入工单。",
        "trap": [("security_compliance", f"{S} > 日志留存")],
    },
    {
        "type": "table_lookup",
        "query": "会话隔离复现验证的决议编号和期限",
        "relevant": [("meeting_minutes", "架构评审会议纪要 > 决议汇总")],
        "note": "R3、4 月 23 日。四条决议编号相邻，考编号与内容的对应。",
    },
    # ── false_premise：前提本身是错的（借 CRAG 的 false_premise 题型）──
    {
        "type": "false_premise",
        "query": "E1024 会让设备反复重启，要怎么修",
        "relevant": [("support_models", f"{M} > 常见误配（一）")],
        "note": "假前提：E1024 是证书过期，不会导致重启；反复重启是电源适配器错配。",
        "trap": [("support_errors", f"{E} > 连接与证书类错误（一）")],
    },
    {
        "type": "false_premise",
        "query": "标准款怎么配置双机热备",
        "relevant": [("support_models", f"{M} > 整机规格（标准款）")],
        "note": "假前提：标准款不支持双机热备，硬件上没有同步接口。",
        "trap": [("support_models", f"{M} > 整机规格（增强款）")],
    },
    {
        "type": "false_premise",
        "query": "机架款怎么用壁挂件装在墙上",
        "relevant": [("support_models", f"{M} > 配件兼容性（一）")],
        "note": "假前提：机架款是 1U 上柜，不需要也不支持壁挂件。",
        "trap": [("support_models", f"{M} > 壁挂与桌面安装")],
    },
    {
        "type": "false_premise",
        "query": "标准款的万兆光口要配哪种光模块",
        "relevant": [("support_models", f"{M} > 整机规格（标准款）")],
        "note": "假前提：标准款只有千兆网口，没有万兆光口。",
        "trap": [("support_models", f"{M} > 整机规格（机架款）")],
    },
    # ── 跨文档多跳：证据分布在不同文档（借 MultiHop-RAG 的设定）──
    {
        "type": "cross_section",
        "query": "退款和退费分别退到哪里去",
        "relevant": [
            ("billing_policy", f"{B} > 退费与退款的区别"),
            ("support_refund", f"{R} > 退款到账时间"),
        ],
        "note": "跨两篇文档：退费进余额在计费文档，退款回原渠道在售后文档。",
    },
    {
        "type": "cross_section",
        "query": "固件升级要走变更流程吗，值班期间要盯什么",
        "relevant": [
            ("ops_runbook", f"{RUN} > 固件升级值班要求"),
            ("ops_change", f"{C} > 变更分级"),
        ],
        "note": "值班手册说「升级由变更流程驱动」，变更规范给分级；跨两篇文档。",
    },
    {
        "type": "cross_section",
        "query": "万兆光模块的价格和选型要求分别在哪里看",
        "relevant": [
            ("price_list", "价格与折扣对照表 > 配件价格"),
            ("support_models", f"{M} > 整机规格（机架款）"),
        ],
        "note": "价格在价格表、必须与端口速率匹配的约束在型号指南；跨两篇文档。",
    },
    {
        "type": "cross_section",
        "query": "会话配额隔离这件事在哪些材料里被提到",
        "relevant": [
            ("incident_report", "故障复盘报告 > 改进项与负责人"),
            ("meeting_minutes", "架构评审会议纪要 > 议题三：会话配额隔离的验证"),
        ],
        "note": "复盘报告给整改项，会议纪要给验证方式；同一件事分散在两篇文档。",
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
    # 再校验「恰好对应一片」：判据是「切片的 owner 章节 == 金标章节」，若某节被切成
    # 多片，一道题就会同时命中多片，R@1 的分母被凭空放大。tests/test_eval_corpus.py
    # 里也有同样的断言，这里提前到写入前，免得写完才发现。
    splitter = MarkdownStructuredSplitter(chunk_size=800, chunk_overlap=100, table_max_rows=10)
    pieces_per_section: dict[tuple[str, str], int] = {}
    for doc in _load_corpus():
        for piece in splitter.split(doc.text):
            key = (doc.stem, str(piece.metadata.get("heading_path") or ""))
            pieces_per_section[key] = pieces_per_section.get(key, 0) + 1

    problems: list[str] = []
    for item in pending:
        for document, heading_path in list(item["relevant"]) + list(item.get("trap") or []):
            if document not in sections_by_doc:
                problems.append(f"未知文档 {document}")
            elif heading_path not in sections_by_doc[document]:
                problems.append(f"章节不存在：{document} / {heading_path}")
            else:
                count = pieces_per_section.get((document, heading_path), 0)
                if count != 1:
                    problems.append(f"章节被切成 {count} 片（应为 1）：{document} / {heading_path}")
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
