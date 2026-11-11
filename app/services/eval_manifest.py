"""评测语料清单（``eval/corpus/manifest.json``）的**唯一**读取点。

**为什么它住在 ``app/`` 而不是 ``scripts/``**：旁证跑批由 Worker 执行
（``app/tasks/side_evidence.py`` → ``app/services/side_evidence_run.py``），它需要知道
「评测要碰哪几个知识库」。这份清单原先是 ``scripts/eval_retrieval.py`` 的私有函数
``_load_manifest`` 提供的，于是 ``app/`` 反过来 import 了脚本的私有函数——依赖方向是反的：

- 脚本依赖应用是正常的（它们本来就在装配应用的服务）；
- 应用依赖脚本，会让 Worker 的可用性取决于 ``scripts/`` 在不在 ``sys.path`` 上、
  以及进程启动时的 cwd（``scripts/`` 没有 ``__init__.py``）。

所以清单读取上移到这一层，``scripts/eval_retrieval.py`` 反过来用它，并保留
``CorpusLayout`` / ``_load_manifest`` 两个旧名字（``eval_runner`` 等仍按旧名调用）。

**只读、不连库**：这里不建知识库、不查切片，只把 JSON 解成结构。真正的语料装载
（切块、写入）仍在脚本里——那是评测环境的事，不该进生产进程。
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from typing import Final

#: ``app/services/eval_manifest.py`` → ``services`` → ``app`` → 仓库根
#: （与 ``app/utils/tokenizer.py`` 找 ``.tokenizer/`` 同一种定位法）。
REPO_ROOT: Final[pathlib.Path] = pathlib.Path(__file__).resolve().parents[2]

#: 语料清单：每个知识库的 id、名字、描述与文档 stem 列表，外加一个「空库」。
MANIFEST_PATH: Final[pathlib.Path] = REPO_ROOT / "eval" / "corpus" / "manifest.json"


@dataclass(frozen=True)
class CorpusLayout:
    """清单里的一个知识库：只描述「有哪些文档」，不含正文。"""

    kb_id: str
    name: str
    description: str
    documents: tuple[str, ...]


def load_manifest(path: pathlib.Path | None = None) -> tuple[list[CorpusLayout], str]:
    """读清单，返回（有语料的库列表，留给「空结果」用例的空库 id）。

    空库单独返回而不是混进列表：它**故意没有文档**，把它当普通库去装载，检索会以
    「knowledge base not found」整单拒绝（旁证实跑踩过）。
    """

    payload = json.loads((path or MANIFEST_PATH).read_text(encoding="utf-8"))
    layouts = [
        CorpusLayout(
            kb_id=kb["kb_id"],
            name=kb["name"],
            description=kb.get("description", ""),
            documents=tuple(kb["documents"]),
        )
        for kb in payload["kbs"]
    ]
    return layouts, payload["empty_kb"]["kb_id"]


__all__ = ["MANIFEST_PATH", "REPO_ROOT", "CorpusLayout", "load_manifest"]
