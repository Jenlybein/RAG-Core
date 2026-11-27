"""索引「代」（第 17 步现场九）：把索引产物从**库级**下放到**文档级**的纯函数层。

## 两根轴，各说各的

| 轴 | 库里的列 | 回答什么 |
| --- | --- | --- |
| **稿次** | ``documents.current_version`` / ``chunks.doc_version`` | 这篇文档的**正文**改过几次、现在是第几稿 |
| **代** | ``documents.current_index_generation`` / ``chunks.generation`` | 这篇文档的**索引产物**算过几次、现在用第几代 |

改造之前索引产物是「整个库一份」（``knowledge_bases.active_index_version`` +
``index_build_versions``），于是出现一件很别扭的事：起构建时要取全库稿号的最大值当
「统一稿号」套到每一篇头上，而稿次落后那几篇取正文时必然对不上（20030）。那不是笔误，
是**库级判据引用了篇级数据**——两根轴不正交的必然结果。

下放到篇级之后，两根轴各自闭环：改正文只动稿次，换切法只动代，谁都不引用对方的数。

## 本模块只放**纯函数**

给它行、给回值，不碰会话、不碰库。理由是这个仓已经吃过一次教训：判据写两遍就会分叉
（ADR-0031 决策五的校验和）。所以：

* 迁移回填用它算指纹（:func:`settings_fingerprint`）；
* 运行时的「这篇要不要更新」用它算指纹；
* 两者是**同一个函数**，不存在「SQL 一份、Python 一份」。

迁移里因此不能 ``alembic upgrade --sql`` 离线渲染：逐行算指纹需要真库。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

#: 指纹的十六进制长度。与 ``documents.settings_hash`` 的 ``String(64)`` 对齐（留一倍余量：
#: 换哈希算法时不必改列宽，而改列宽在老库上是一次 DDL）。
FINGERPRINT_HEX_LENGTH = 64

#: 「这一片属于第 1 代」——迁移回填时给「当前生效产物」打的那个号。
FIRST_GENERATION = 0 + 1

#: 已经被淘汰的历史代（迁移时把旧版本的切片标成它）。它小于任何当前代，
#: 于是「保留最近 N 代」的回收条件 ``generation < 当前代 - N + 1`` 会自动把它算进去。
SUPERSEDED_GENERATION = 0


def settings_fingerprint(params: Mapping[str, Any] | None) -> str | None:
    """画切块参数的指纹：**同样的设置得到同样的串**，不同设置得到不同串。

    ## 为什么是「内容指纹」而不是「设置在库上的版本号」

    版本号（1、2、3……）也能回答「谁比谁新」，但它答不了另一个更常见的问题：

    > 我把设置改回原样了，那些文档还要不要重算？

    版本号会答「要」（号只增不减），指纹会答「不要」（内容一样，指纹一样）。而这个仓的
    切块参数只有十来个字段、取值范围有限，运维把设置改回去是常事——每改回去一次就要求
    全库重算一遍，等于用一个记录方式制造了一批无意义的重嵌入（真金白银的 embedding）。

    ## 为什么用排序后的 JSON 而不是 ``repr``

    ``repr`` 受键顺序影响：同样的设置从两条路径构造出来，键顺序可能不同，指纹就会不同，
    症状是「设置没改，界面却说 40 篇待更新」，而且它只在**某些**路径上出现。排序 + 紧凑
    分隔符把这件事钉成确定的。

    ``None``（还没记过）原样返回 ``None``，**不做成空串**：调用方据此判「未知」而不是
    「和当前设置不一样」——两者在界面上都可能显示成待更新，但在日志里必须分得开。
    """

    if params is None:
        return None
    canonical = json.dumps(dict(params), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:FINGERPRINT_HEX_LENGTH]


def generation_of(index_version: str | None, active_index_version: str | None) -> int:
    """迁移回填用：这一批切片属于「当前代」还是「已被淘汰的旧代」。

    判据是「它的库级版本名是否等于该库当时生效的那个版本名」——改造前那一版就是线上在
    用的产物，所以它是第 :data:`FIRST_GENERATION` 代；其余历史版本的切片标
    :data:`SUPERSEDED_GENERATION`，交给回收流程清掉（改造前根本没有回收，旧版本的切片
    一直躺在库里）。

    两个参数都可能为 ``None``（库里没生效版本 / 切片没有版本名），这时按**旧代**处理：
    「不确定它是不是当前产物」不能当成「它就是当前产物」，否则一次回填会把来路不明的切片
    认成线上内容。
    """

    if active_index_version is None or index_version is None:
        return SUPERSEDED_GENERATION
    return FIRST_GENERATION if index_version == active_index_version else SUPERSEDED_GENERATION


def is_stale(settings_hash: str | None, current_fingerprint: str | None) -> bool:
    """这篇文档的产物是不是落后于库当前设置。

    ``None`` 的一侧一律算落后（宁可多更新一次，也不要让一个「来路不明」的产物被当成最新
    的）——这条与「需重建」那条提示同一条纪律：宁可多提示一次，也不要少提示。
    """

    return (
        settings_hash is None or current_fingerprint is None or settings_hash != current_fingerprint
    )
