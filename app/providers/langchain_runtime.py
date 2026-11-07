"""框架级运行时设置：所有 LangChain 客户端共用、与具体能力无关的开关。

目前只有一件：**把「是否外发追踪」从配置同步到框架真正读的地方**。

框架的追踪客户端只读环境变量、不读我们的 Settings，所以 `.env` 里写
``LANGCHAIN_TRACING_V2=false`` 只是文档，除非有人真的把它写进进程环境。而
「数据不出网」这件事要在**任何客户端被构造之前**就成立，因此两个构造点
（大模型与嵌入）都要调用它，包括「没配 key、走空实现」的那条路径——审计要看的是
「这个进程里的框架永远不外发」，而不是「有 key 的时候才不外发」。

总是**覆盖**而不是 ``setdefault``：框架看到的值必须等于配置解析出来的值，否则会出现
「配置说 false、进程环境里还残留 true」两种说法并存。默认值就是 false，所以审计只看
一个地方——这个函数写进进程环境的那串字符。
"""

from __future__ import annotations

import os

from app.core.config import settings

# LANGCHAIN_* 是旧名、LANGSMITH_* 是新名，不同版本读不同的那一个，两个都同步才关得干净。
_TRACING_SWITCHES = ("LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING")


def apply_framework_telemetry_switches() -> None:
    """把「是否外发追踪」从配置同步到框架真正读的环境变量上。"""

    flag = "true" if settings.LANGCHAIN_TRACING_V2 else "false"
    for name in _TRACING_SWITCHES:
        os.environ[name] = flag


__all__ = ["apply_framework_telemetry_switches"]
