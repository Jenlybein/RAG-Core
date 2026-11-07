"""离线测试的环境隔离：关掉 MCP 会话管理器、关键词引擎固定成空实现。

必须在任何测试文件 import ``app.main`` 之前生效，所以放在 conftest 里
（``app.core.config.settings`` 在 import 时就把环境变量读成对象了）。
生产 / 手动起服务时 ENABLE_MCP 默认 True，/mcp/ 正常挂载。

**为什么 KEYWORD_SEARCH_PROVIDER 是强制覆盖而不是 setdefault**：离线套件的前提是
「无 ES、无网络、无 .env」。可这条前提以前是靠「本机恰好没有 .env」成立的——开发者
在 shell 里导出过 .env 模板里的 ``KEYWORD_SEARCH_PROVIDER=elasticsearch``（或者干脆
docker compose 起过容器），套件就会真的去连 ES：结果取决于这台机器今天有什么，
而不是取决于代码。``setdefault`` 拦不住这种情况（变量已经存在，它什么都不做），
所以这里无条件写死 ``none``：**环境里有 ES 配置也一样全绿**。
"""

import os

os.environ.setdefault("ENABLE_MCP", "false")
# 强制覆盖：见上。这一行让「无 ES」成为套件自己的事实，而不是机器的巧合。
os.environ["KEYWORD_SEARCH_PROVIDER"] = "none"
