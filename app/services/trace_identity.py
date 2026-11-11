"""``trace_id`` 的派生与「交付单元有哪些片」的收口（#101）。

**这里只有纯函数**：不碰数据库、不碰观测 SDK、不读配置。理由是可测性也是契约——
「同一个 ``log_id`` 永远得到同一个 ``trace_id``」这条性质必须能在不启动任何旁路的情况下
被证明，而第 9 步之后接观测栈（#102）的那一层只是**拿走这里算出来的值**去注入
（``trace_context``），不重新生成一个 id。

派生方式：``sha256(log_id)`` 取前 32 位十六进制（128 位）。选它的理由：

* **确定性**：给定种子结果唯一，于是响应里那根线与库里那一行不需要第二张对照表；
* **不依赖外部服务**：没有 SDK、没有网络、没有随机源，部署切到 ``jsonl`` / ``null``
  时 id 照样有值；
* **形状就是契约**：32 位十六进制是 128 位，碰撞概率远低于「同一个租户一辈子能发起的
  检索次数」，拿来当 trace 的标识足够。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Final

#: 契约规定 ``trace_id`` 是 32 位十六进制（ADR-0010 第五次修订）。
TRACE_ID_LENGTH: Final[int] = 32


def trace_id_for(log_id: str) -> str:
    """以 ``log_id`` 为种子派生 ``trace_id``；同一个种子恒等。

    ``log_id`` 必须是本次检索的行 id（``new_retrieval_log_id()``）——它是种子的唯一来源，
    换种子就等于换契约。空串在这里不做特殊处理：它只可能来自「根本没有生成过 id」的
    调用，是一个应当在代码里被修掉的 bug，而不是运行时要兜的形状。
    """

    digest = hashlib.sha256(log_id.encode("utf-8")).hexdigest()
    return digest[:TRACE_ID_LENGTH]


#: 租户指纹的长度：16 位十六进制 = 64 位。碰撞概率对「租户数」这个量级绰绰有余。
TENANT_FINGERPRINT_LENGTH: Final[int] = 16


def tenant_fingerprint(tenant_id: str) -> str:
    """把 ``tenant_id`` 变成**观测栈里的稳定指纹**，而不是明文。

    为什么要指纹（而不是明文、也不是不加）：

    - **不加**：Langfuse 侧就永远只能看平台总量，没法按租户切分——而管理端监控最想回答
      的恰恰是「哪个租户在拖慢 / 在大量降级」。历史数据补不回来，所以这件事开始推之前
      就得定下来。
    - **明文**：观测栈是自托管的，明文其实也能接受；但观测数据常常要导出给别人看
      （排障、给外部做容量评估），明文租户标识会让「这份数据能发给谁」多一道合规问题。
    - **指纹**：同一个租户恒等，于是能在观测栈里按它分组、筛选、看趋势；而它本身不含
      租户名与 id，导出去也不泄露客户身份。控制台侧有一张映射表（`tenants` 表）可以把
      指纹翻回租户名。

    与 ``trace_id_for`` 一样是纯函数：不读配置、不碰库，因此能在离线套件里逐字节断言。
    """

    digest = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()
    return digest[:TENANT_FINGERPRINT_LENGTH]


def delivered_chunk_ids(results: Iterable[Mapping[str, object]]) -> list[str]:
    """这次交给调用方的**交付单元**里涉及哪些切片 id（代表片在前，溯源片随后）。

    折叠之后一条结果代表一节（ADR-0013）：``chunk_id`` 是代表片，``source_chunk_ids``
    是把它顶上来的那几片。两者都要记，否则「为什么是这一节」事后无法回指；但**只记 id**：
    正文在切片表里有一份，分数明细属于评测与 trace。

    去重保序：同一片既可能以代表片出现、又在别的单元里当溯源片（表格行折回整表时就会
    这样），重复记一次不给任何信息，只会让这一列随折叠方式膨胀。
    """

    seen: dict[str, None] = {}
    for result in results:
        representative = result.get("chunk_id")
        if isinstance(representative, str) and representative:
            seen.setdefault(representative, None)
        sources = result.get("source_chunk_ids")
        if isinstance(sources, list):
            for source in sources:
                if isinstance(source, str) and source:
                    seen.setdefault(source, None)
    return list(seen)
