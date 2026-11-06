"""离线测试：关掉 MCP 会话管理器，避免每个 TestClient 都启停 lifespan。

必须在任何测试文件 import app.main 之前生效，所以放在 conftest 里。
生产 / 手动起服务时 ENABLE_MCP 默认 True，/mcp/ 正常挂载。
"""

import os

os.environ.setdefault("ENABLE_MCP", "false")
