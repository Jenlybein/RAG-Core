"""导入预算：``import app.main`` 不许把重型可选依赖拖进每个进程。

## 为什么值得单独一条用例

这笔开销是**隐形的**：它不报错、不影响正确性，只是让每个进程（pytest 收集、每个运维
脚本、每次手工 ``python -c``）都先等几秒。本项目实测过两次这样的账：

* `fastmcp` 冷导入约 2.9 s，而离线套件与「不挂 MCP 的部署」根本用不到它；
* `langchain_openai` 约 2.9 s（经它还会拖进整个 `openai` 客户端栈），而它只在**真的
  要用嵌入**时才需要。

两者原本都在模块顶层 import，于是 ``import app.main`` 要 10~13 s；改成惰性之后是 3.5 s。
这类回归没有任何测试会自然抓到——所以这里用一个子进程直接量「导入了什么」。

## 判据

用子进程跑一次 ``import app.main``（``ENABLE_MCP=false``），检查 ``sys.modules`` 里**没有**
那两个包。用子进程而不是当前进程：当前进程早就 import 过它们了（其它用例会用），
测不出「顶层 import」这件事。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 顶层 import 会把它们拖进每一个进程，而它们只服务少数路径。
_LAZY_ONLY = ("fastmcp", "langchain_openai")

_PROBE = """
import json, os, sys
os.environ["ENABLE_MCP"] = "false"
os.environ["KEYWORD_SEARCH_PROVIDER"] = "none"
sys.path.insert(0, {root!r})
import app.main  # noqa: F401
print(json.dumps(sorted(sys.modules)))
"""


def test_importing_the_app_does_not_drag_in_optional_heavyweights() -> None:
    """``import app.main`` 之后，重型可选依赖不该已经在 ``sys.modules`` 里。

    失败的含义：有人又把 ``from fastmcp... `` / ``from langchain_openai ...`` 挪回了模块
    顶层。功能不会坏，但每个进程白等几秒——修法是把那个 import 放回使用它的函数里，
    并保留一个可 patch 的模块级薄函数（见 ``app.providers.llm._init_chat_model`` 的注释：
    惰性化不能顺手把测试缝一起弄没，那会变成「用例绿着但什么都没测」）。
    """

    probe = _PROBE.format(root=str(REPO_ROOT))
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]

    loaded = set(json.loads(completed.stdout.strip().splitlines()[-1]))
    dragged = sorted(
        name for name in _LAZY_ONLY if any(m == name or m.startswith(name + ".") for m in loaded)
    )
    assert not dragged, (
        f"import app.main 又把这些包拖进来了：{dragged}。"
        "每个进程会因此多等几秒——请把对应的 import 放回使用它的函数里"
    )
