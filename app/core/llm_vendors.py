"""大模型供应商名单：**这家仓库支持哪几家**的唯一真相。

**为什么单独一个中立模块**（而不是把名单写进 ``config.py`` 或工厂里）：

``LLM_PROVIDER`` 的取值需要两个使用方，而它们的依赖方向正好相反：

* ``app/core/config.py`` 要在**启动期**校验取值（拼错 → 进程起不来）；
* ``app/providers/llm/__init__.py`` 要在**装配期**按名字选出地址与 key。

让 config 去 import 工厂是不行的：工厂 import 了 LangChain 与 ``settings`` 自己，
config 又是 ``settings`` 的定义处——那是循环依赖，而且会把「框架」拖进「配置」的
导入链（每个 import config 的模块都跟着加载 LangChain）。让工厂去 config 里读一份
名单，等于「两份真相」：加一家只改一处，另一处静默不同步。

所以名单放在这里：一个**不 import 任何东西**的模块，两边都 import 它。装配知识
（框架 provider 名 / 默认地址 / key 落在哪个配置项）仍留在工厂里——那是装配的
细节，不属于配置词汇表。两边的对应关系由
``tests/test_llm_provider.py::test_vendor_names_have_exactly_one_source_of_truth``
钉住：工厂的装配表必须与这份名单逐个相等，漂移会当场变红。
"""

from __future__ import annotations

# 顺序即报错文案里的顺序：运维看到的允许值列表与这几行逐字一致。
LLM_VENDORS: tuple[str, ...] = (
    "deepseek",
    "qwen",
    "ark",
    "openai",
    "anthropic",
    "gemini",
)

__all__ = ["LLM_VENDORS"]
