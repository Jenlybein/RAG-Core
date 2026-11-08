"""给旧文档插入 h3/h4 标题，增加纵深，内容一个字不改。

为什么要保持「每个 h2 下至少 2 个 h3」：切块器算 heading_path 时取本节叶子的
公共前缀，分叉时回落到 h2。所以这样改造后路径仍是 2 段，已有题目的金标不用动；
反过来，如果某个 h2 下只留 1 个 h3，路径会变成 3 段，金标必须同步改。

用法：uv run python scripts/add_heading_depth.py [--dry-run]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# (文件, 段落锚点, 需要插在锚点之前的标题行)
INSERTS: list[tuple[str, str, str]] = [
    # ── troubleshoot_deep：四个案例都是「现象 → 排查 → 处置 → 教训」结构 ──
    (
        "troubleshoot_deep.md",
        "现象是某个区域的门店在凌晨集中离线",
        "### 现象与初查\n\n#### 表面现象\n\n",
    ),
    (
        "troubleshoot_deep.md",
        "初步排查走的是常规路径",
        "#### 初步排查\n\n",
    ),
    (
        "troubleshoot_deep.md",
        "在设备侧抓包后发现",
        "### 排查与定位\n\n#### 抓包发现\n\n",
    ),
    (
        "troubleshoot_deep.md",
        "处置分两步。止血是把受影响设备的证书链裁剪",
        "### 处置与改进\n\n#### 处置步骤\n\n",
    ),
    (
        "troubleshoot_deep.md",
        "复盘时的改进项有三条",
        "#### 改进项\n\n",
    ),
    (
        "troubleshoot_deep.md",
        "这个案例的教训是：**没有错误码不等于没有问题**",
        "### 教训\n\n",
    ),
    (
        "troubleshoot_deep.md",
        "现象是华东区域约两千多台终端在夜间批量掉线",
        "### 现象与初查\n\n#### 表面现象\n\n",
    ),
    (
        "troubleshoot_deep.md",
        "排查第一步看时间分布，同一秒集中掉线",
        "### 排查与定位\n\n#### 第一步：时间分布\n\n",
    ),
    (
        "troubleshoot_deep.md",
        "第二步看设备侧指标",
        "#### 第二步：设备侧指标\n\n",
    ),
    (
        "troubleshoot_deep.md",
        "第三步找是什么在消耗会话",
        "#### 第三步：找到消耗源\n\n",
    ),
    (
        "troubleshoot_deep.md",
        "处置分两步。止血是临时调高会话表配额",
        "### 处置与改进\n\n#### 处置步骤\n\n",
    ),
    (
        "troubleshoot_deep.md",
        "改进项里有两条是机制性的",
        "#### 改进项\n\n",
    ),
    (
        "troubleshoot_deep.md",
        "这个案例的教训是：**容量规划要按最坏情况设计",
        "### 教训\n\n",
    ),
    (
        "troubleshoot_deep.md",
        "现象是某门店反馈「设备显示在线但业务系统传不上数据」",
        "### 现象与初查\n\n#### 表面现象\n\n",
    ),
    (
        "troubleshoot_deep.md",
        "这类「指标正常但业务异常」的问题最难查",
        "### 反向追踪\n\n#### 换思路\n\n",
    ),
    (
        "troubleshoot_deep.md",
        "抓包结果显示数据包发出去了",
        "#### 抓包结果\n\n",
    ),
    (
        "troubleshoot_deep.md",
        "处置是给监控回传做限速",
        "### 处置与改进\n\n#### 处置步骤\n\n",
    ),
    (
        "troubleshoot_deep.md",
        "改进项包括：部署验收时要扫描一遍上行流量",
        "#### 改进项\n\n",
    ),
    (
        "troubleshoot_deep.md",
        "这个案例的教训是：**「在线」不等于「可用」**",
        "### 教训\n\n",
    ),
    (
        "troubleshoot_deep.md",
        "现象是某一批设备在断电恢复后集中出现签名校验失败",
        "### 现象与初查\n\n#### 表面现象\n\n",
    ),
    (
        "troubleshoot_deep.md",
        "排查先对比正常批次与异常批次的差异",
        "### 排查与定位\n\n#### 差异对比\n\n",
    ),
    (
        "troubleshoot_deep.md",
        "签名校验依赖时间戳",
        "#### 隐性依赖\n\n",
    ),
    (
        "troubleshoot_deep.md",
        "打破死锁的方式是给注册流程加一个「时间预同步」步骤",
        "### 处置与改进\n\n#### 处置步骤\n\n",
    ),
    (
        "troubleshoot_deep.md",
        "改进项：出厂配置里应当预置至少两个公共 NTP 地址",
        "#### 改进项\n\n",
    ),
    (
        "troubleshoot_deep.md",
        "这个案例的教训是：**隐性依赖比显性依赖更危险**",
        "### 教训\n\n",
    ),
    # ── integration_api：每节拆成「是什么 → 怎么做 → 踩过什么坑」 ──
    (
        "integration_api.md",
        "平台对外提供两类接口：管理类接口与业务类接口",
        "### 鉴权模型\n\n#### 两类接口\n\n",
    ),
    (
        "integration_api.md",
        "凭据由两部分组成：应用 ID 与应用密钥",
        "#### 凭据构成\n\n",
    ),
    (
        "integration_api.md",
        "签名算法基于 HMAC-SHA256",
        "### 签名计算\n\n#### 待签字符串\n\n",
    ),
    (
        "integration_api.md",
        "时间戳采用 Unix 秒级时间戳",
        "#### 时间戳与随机串\n\n",
    ),
    (
        "integration_api.md",
        "密钥轮换每 6 个月一次",
        "### 密钥轮换\n\n#### 轮换周期\n\n",
    ),
    (
        "integration_api.md",
        "平台在异步任务完成、状态变更、告警触发这三类场景下会向调用方推送回调",
        "### 回调机制\n\n#### 投递方式\n\n",
    ),
    (
        "integration_api.md",
        "回调地址需要在控制台登记并完成校验",
        "#### 地址登记\n\n",
    ),
    (
        "integration_api.md",
        "调用方必须在 5 秒内返回 2xx 状态码",
        "### 超时与重试\n\n#### 响应要求\n\n",
    ),
    (
        "integration_api.md",
        "失败重试采用指数退避",
        "#### 重试序列\n\n",
    ),
    (
        "integration_api.md",
        "回调体里的字段分为三类",
        "### 字段与时序\n\n#### 字段分类\n\n",
    ),
    (
        "integration_api.md",
        "回调的时序不保证严格有序",
        "#### 时序问题\n\n",
    ),
    (
        "integration_api.md",
        "限流分三个维度：按应用凭据的分钟级请求数",
        "### 三个维度\n\n#### 维度划分\n\n",
    ),
    (
        "integration_api.md",
        "分钟级请求数按滑动窗口计算",
        "#### 滑动窗口\n\n",
    ),
    (
        "integration_api.md",
        "看限流错误时要注意区分两种情形",
        "### 排查与调优\n\n#### 两种情形\n\n",
    ),
    (
        "integration_api.md",
        "退避参数建议采用「指数退避 + 抖动」",
        "#### 退避参数\n\n",
    ),
    (
        "integration_api.md",
        "错误码按四段设计",
        "### 分层结构\n\n#### 四段编码\n\n",
    ),
    (
        "integration_api.md",
        "签名类错误是最容易误判的一组",
        "### 分类排查\n\n#### 签名类\n\n",
    ),
    (
        "integration_api.md",
        "回调类错误要注意区分「我方发出失败」与「对方处理失败」",
        "#### 回调类\n\n",
    ),
    (
        "integration_api.md",
        "存储与介质类错误在接口场景下较少见",
        "#### 存储类\n\n",
    ),
    (
        "integration_api.md",
        "错误响应体包含四个字段",
        "### 错误处理约定\n\n#### 响应字段\n\n",
    ),
    (
        "integration_api.md",
        "重试策略要按错误码分类，不能一刀切",
        "#### 重试分类\n\n",
    ),
]


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    corpus = REPO_ROOT / "eval" / "corpus"
    touched: dict[str, str] = {}
    misses: list[str] = []

    for filename, anchor, heading in INSERTS:
        path = corpus / filename
        text = touched.get(filename) or path.read_text(encoding="utf-8")
        if anchor not in text:
            misses.append(f"{filename}: 找不到锚点 {anchor[:24]!r}")
            continue
        # 幂等判断看锚点行「紧邻的上一非空行是不是 h3/h4」。
        # 两个坑：① 同一份文档的四个案例复用同一套小标题，用全文包含判断会把
        # 第二个案例误判成已插过；② 第一个锚点紧贴在 h2 下方，把「上方有标题」
        # 当成已插入会漏掉每节第一个锚点。只认 h3/h4 才既幂等又不漏。
        index = text.index(anchor)
        line_start = text.rfind("\n", 0, index) + 1
        head = text[:line_start].rstrip()
        prev_line = head.rsplit("\n", 1)[-1] if head else ""
        if re.match(r"^#{3,6}\s", prev_line):
            continue
        text = text[:line_start] + heading + text[line_start:]
        touched[filename] = text

    if misses:
        print("锚点未命中：")
        for miss in misses:
            print("  -", miss)
        return 1

    for filename, text in touched.items():
        print(f"{filename}: {len(text)} 字")
        if not dry_run:
            (corpus / filename).write_text(text, encoding="utf-8")
    print(f"{len(INSERTS)} 处标题插入{'（dry-run，未写文件）' if dry_run else '完成'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
