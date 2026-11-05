"""工程底座冒烟测试：守住「装齐工具链」与「离线可导入」两条底线。

测试接缝是导入边界：只观察 import 是否成功、应用是否挂了路由，
不发送任何 HTTP 请求、不创建数据库会话，因此 CI 无需 PostgreSQL。
"""

import importlib

from fastapi import FastAPI


def test_structlog_is_installed() -> None:
    """运行时依赖声明缺 structlog 时，后续日志票会直接从 import 处崩掉。"""

    module = importlib.import_module("structlog")

    assert module is not None
    assert hasattr(module, "get_logger")


def test_app_imports_without_database_connection() -> None:
    """导入 app.main 不应连 PostgreSQL；应用必须挂上路由而不是空壳。"""

    main = importlib.import_module("app.main")

    assert isinstance(main.app, FastAPI)
    paths = {route.path for route in main.app.routes}
    assert "/health" in paths
    assert "/api/v1/knowledge-bases/create" in paths
    assert "/api/v1/documents/upload" in paths
    assert "/api/v1/rag/retrieve" in paths
