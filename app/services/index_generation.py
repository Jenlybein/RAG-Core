"""索引「代」（第 17 步现场九）：把索引产物从**库级**下放到**文档级**的纯函数层。

## 两根轴，各说各的

| 轴 | 库里的列 | 回答什么 |
| --- | --- | --- |
| **稿次** | ``documents.current_version`` / ``chunks.doc_version`` | 这篇文档的**正文**改过几次、现在是第几稿 |
| **代** | ``documents.current_index_generation`` / ``chunks.generation`` | 这篇文档的**索引产物**算过几次、现在用第几代 |

改造之前索引产物是「整个库一份」（``knowledge_bases.active_index_version`` +
``index_build_versions``），于是出现一件很别扭的事：起构建时要取全库稿号的最大值当
「统一稿号」套到每一篇头上，而稿次落后那几篇取正文时必然对不上（20030）。那不是笔误，
是**库级判据引用了篇级数据**——两根轴不正交的必然结果。下放到篇级之后，两根轴各自闭环：
改正文只动稿次，换切法只动代，谁都不引用对方的数。

## 本模块只放**纯函数**，且只放**运行时也要用**的东西

判据写两遍就会分叉（ADR-0031 决策五的校验和那一条的翻版）。所以：

* 指纹：迁移回填要用它、运行时「这篇要不要更新」也要用它 → 放在这里，**只有这一份**；
* 分代：只在迁移里做一次（回填历史），用的是 SQL（判据是「版本名是否等于库的生效版本名」）。
  **所以这里没有对应的 Python 实现**——留着它就是第二份口径，而且没有任何运行时消费者。
  （第一版还真留了一个，评审抓出来后才删掉：一份没有消费者的判据不是「备用」，是定时炸弹。）

迁移因此不能 ``alembic upgrade --sql`` 离线渲染：逐篇算指纹需要真库。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from app.models.chunk import DEFAULT_GENERATION
from app.models.knowledge_base import build_params_of

#: 指纹的十六进制长度。sha256 的全长正好是 64，所以这里的截断**当前不起作用**；保留它是
#: 一条契约：``documents.settings_hash`` 是 ``String(64)``，将来换更长的摘要时由这一行决定
#: 「存进去的那一段有多长」，而不是让数据库静默截断（静默截断会让两个不同指纹撞成同一个值，
#: 症状是「改完设置点更新，一半文档不生效」）。
FINGERPRINT_HEX_LENGTH = 64

#: 参与指纹的**规范键集**：从 :func:`app.models.knowledge_base.build_params_of` 的缺省输出
#: 取键，而不是在这里再抄一遍。
#:
#: 这是「只比该比的」那一条的落地（第 16 步 #207）：名称、描述、允许格式不进产物，把它们
#: 算进指纹会让每个库永远显示「待更新」。同一份键集也解释了为什么版本快照里多出来的键
#: （老版本写过、后来删掉的那种）必须被忽略——不忽略就是「设置没改，却说全库待更新」。
CANONICAL_PARAM_KEYS: tuple[str, ...] = tuple(build_params_of(None))

#: 已经被淘汰的历史代（迁移时把旧版本名下的切片标成它）。
#:
#: 它比任何当前代都小，所以「回收」的判据必须是**集合式**的（保留该篇存在的最高 N 个代），
#: 不能写成 ``generation < 当前代 - N + 1`` 这种算术式：当前代是第 1 代、只留 2 份时，那个
#: 算式给出的阈值是 0，于是第 0 代永远收不到（算术式只在「每一代都恰好存在」时才成立）。
SUPERSEDED_GENERATION = 0

#: 迁移回填时给「当前生效产物」打的那个号。取自模型层的列缺省值——**只有一个字面量**，
#: 迁移、模型、运行时都指向它（分头写「1」迟早有一处漂移，而漂移的症状是新旧产物被算成
#: 不同代，界面从此永远显示待更新）。
FIRST_GENERATION = DEFAULT_GENERATION


def settings_fingerprint(params: Mapping[str, Any] | None) -> str | None:
    """画切块参数的指纹：**同样的设置得到同样的串**，不同设置得到不同串。

    ## 只取规范键集

    版本快照里可能多出一些键（老版本写过、后来删掉的），也可能缺键（更老的版本行）。
    只取 :data:`CANONICAL_PARAM_KEYS`，多出来的忽略；**缺任何一个键就返回 ``None``**
    ——「这份快照说不清它用的是哪套设置」，按「未知」处理（调用方一律判落后），
    而不是拿剩下的键算出一个看起来很像真的指纹。

    ## 为什么是「内容指纹」而不是「设置在库上的版本号」

    版本号（1、2、3……）能回答「谁比谁新」，但答不了更常见的那个问题：把设置改回原样之后
    还要不要重算？版本号答「要」（号只增不减），指纹答「不要」。这个仓的切块参数只有三项、
    取值范围有限，运维把设置改回去是常事——每改回去一次就要求全库重算，等于用一个记录方式
    制造了一批无意义的重嵌入（真金白银的 embedding）。

    ## 为什么用排序后的 JSON 而不是 ``repr``

    ``repr`` 受键顺序影响：同样的设置从两条路径构造出来键顺序可能不同，指纹就会不同，
    症状是「设置没改，界面却说 40 篇待更新」，而且只在**某些**路径上出现。
    """

    if params is None:
        return None
    canonical = {key: params.get(key) for key in CANONICAL_PARAM_KEYS}
    if any(value is None for value in canonical.values()):
        return None
    serialized = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:FINGERPRINT_HEX_LENGTH]


def is_stale(settings_hash: str | None, current_fingerprint: str | None) -> bool:
    """这篇文档的产物是不是落后于库当前设置。

    任一侧是 ``None`` 都算落后：``settings_hash`` 为空是「这批产物的参数来路不明」，
    ``current_fingerprint`` 为空是「库现在的设置说不清」。宁可多更新一次，也不要让一个
    说不清来历的产物被当成最新的——「少提示一次」与「永远不再提示」不是一回事。
    """

    return (
        settings_hash is None
        or current_fingerprint is None
        or settings_hash != current_fingerprint
    )
