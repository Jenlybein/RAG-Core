"""把受 h3 成为切片边界影响的 15 道题的旧金标钉到子节级。

每条都按「答案正文落在哪个子节」判定，不按关键词匹配。判定依据写在
RELABELS 里，便于人工复核。

用法：uv run python scripts/relabel_h3.py [--dry-run]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
QUESTIONS_PATH = REPO_ROOT / "eval" / "questions.json"

CASE_1 = "深度排障案例集 > 案例一：证书链断裂导致的批量离线"
CASE_2 = "深度排障案例集 > 案例二：会话表打满引发的连锁掉线"
CASE_3 = "深度排障案例集 > 案例三：上行拥塞造成的伪故障"
CASE_4 = "深度排障案例集 > 案例四：时间偏移引发的签名失效"
AUTH = "接口对接与鉴权说明 > 鉴权方式与签名算法"
CALLBACK = "接口对接与鉴权说明 > 回调通知与重试策略"
API_REF = "对外接口参考"
CFG_REF = "配置项参考手册"

# 题号 -> (新 heading_path, 判定依据)
RELABELS: dict[str, tuple[str, str]] = {
    "q110": (
        "部署实施指南 > 设备上架与供电接入 > 上架形态",
        "过热问题集中在「型号与位置匹配」与「机架安装」两个 h4 里，二者都在上架形态下",
    ),
    "q112": (
        f"{CASE_1} > 现象与初查",
        "「凌晨集中离线、次日营业前自动恢复」是该案例的现象描述，不是排查过程",
    ),
    "q113": (
        f"{CASE_2} > 处置与改进",
        "「先做什么再做什么」对应处置分两步：止血调高配额、根因打散集中任务",
    ),
    "q114": (
        f"{CASE_3} > 现象与初查 > 表面现象",
        "「设备显示在线但传不上数据」原句就是该案例的表面现象；该 h3 下只有一个 h4，故路径四段",
    ),
    "q115": (
        f"{CASE_4} > 排查与定位",
        "「断电恢复 + 签名校验失败」的因果是差异对比得出的：设备时间是出厂默认值",
    ),
    "q116": (
        f"{AUTH} > 签名计算",
        "时间戳偏差窗口写在「待签字符串」下的时间戳小节里，属签名计算",
    ),
    "q117": (
        f"{CALLBACK} > 超时与重试",
        "重试次数与退避间隔在重试序列小节，属超时与重试",
    ),
    "q118": (
        "接口对接与鉴权说明 > 错误码分层与排查路径 > 错误处理约定",
        "「哪些错误码重试没有意义」是重试分类的内容，在错误处理约定下",
    ),
    "q119": (
        f"{AUTH} > 密钥轮换 > 轮换周期",
        "轮换周期、宽限期、未切完的后果都在轮换周期小节；该 h3 下唯一 h4，故路径四段",
    ),
    "q129": (
        f"{API_REF} > 知识库接口 > 创建知识库",
        "「创建后还能不能改」写在创建知识库的请求说明里",
    ),
    "q132": (
        f"{API_REF} > 通用约定 > 错误返回格式",
        "业务错误一律 HTTP 200 的约定写在错误返回格式小节",
    ),
    "q133": (
        f"{CFG_REF} > 检索服务配置 > 融合参数",
        "RRF 的 k 固定 60、不开放配置，写在融合参数小节",
    ),
    "q134": (
        f"{CFG_REF} > 异步任务配置 > Celery",
        "「OCR 吃满索引并发」写在 Celery 的并发与队列小节，不在 OCR 小节",
    ),
    "q135": (
        f"{CFG_REF} > 观测配置 > 日志",
        "调试日志留存 7 天写在日志的级别与格式小节",
    ),
    "q137": (
        f"{CFG_REF} > 检索服务配置 > 重排",
        "「重排超时不返回错误而是降级」写在重排的超时后的行为小节",
    ),
}


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    payload = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    changed = 0

    for question in payload["questions"]:
        target = RELABELS.get(question["id"])
        if target is None:
            continue
        heading_path, reason = target
        old = question["relevant"][0]["heading_path"]
        if old == heading_path:
            continue
        question["relevant"][0]["heading_path"] = heading_path
        note = question.get("note", "")
        marker = "h3 边界生效后重标"
        if marker not in note:
            question["note"] = f"{note}（{marker}：{reason}）" if note else f"{marker}：{reason}"
        changed += 1
        print(f"{question['id']}: {old}  ->  {heading_path}")

    if dry_run:
        print(f"\n{dry_run and changed} 条将改动（dry-run，未写文件）")
        return 0

    QUESTIONS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\n{changed} 条已重标")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
