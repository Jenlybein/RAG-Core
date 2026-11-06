"""配置收口：所有环境变量只在这里读一次。

骨架期之后，第 3 步（文档处理链路）在这里一次补齐 11 项：Celery/Redis、
文档入库、embedding 批次与 OCR 运行环境。.env 模板里还有更多阶段变量，
pydantic-settings 默认忽略未声明的键（extra="ignore"），不会因多配置报错。

``PDF_INSPECTOR_MODEL_CACHE`` 由 pdf-inspector 库自己读取，因此不在这里声明；
它只在 Dockerfile / CI 里作为环境变量设置。
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """从 .env 加载的运行时配置。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_ENV: str = "local"
    DATABASE_URL: str = "postgresql+psycopg://postgres:123456@localhost:5432/rag_db"
    VECTOR_STORE: str = "pgvector"

    MODEL_BASE_URL: str = ""
    MODEL_API_KEY: str = ""
    EMBEDDING_MODEL: str = "qwen3.7-text-embedding"
    EMBEDDING_DIMENSIONS: int = 1536
    # 百炼 embedding 单批上限按模型不同：text-embedding-v3/v4 = 10、
    # v1/v2 = 25、qwen3.7 = 20；默认取最保守的 10。
    EMBEDDING_BATCH_SIZE: int = 10

    CHUNK_SIZE: int = 800
    CHUNK_OVERLAP: int = 100
    TOP_K: int = 5

    # 文档入库：原始文件落盘目录、上传大小上限、单个表格切片的最大行数。
    DOCUMENT_STORAGE_PATH: str = "./data/uploads"
    DOCUMENT_MAX_SIZE_MB: int = 20
    TABLE_MAX_ROWS_PER_CHUNK: int = 10

    # 异步（第 3 步）：Celery 走 Redis，broker 与 result backend 分库。
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/1"

    # OCR（第 3 步）：off / auto / force；离线模式与模型目录主要给容器与 CI 用。
    # PDF_INSPECTOR_MODEL_CACHE 由 pdf-inspector 库自己读取，不在这里声明，
    # 只在 Dockerfile / CI 里设置。
    OCR_MODE: str = "auto"
    OCR_OFFLINE: bool = False
    OCR_MODEL_DIRECTORY: str = ""
    PDFIUM_LIB_PATH: str = ""
    ORT_DYLIB_PATH: str = ""

    # 结构化日志（T2）：只新增这两项，滚动阈值/份数是 setup_logging 的参数。
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "logs"

    # 占位 Key（第 1 步）：数据面两个入口共用。认这个固定 token，
    # tenant_id 取默认租户、不做真实校验；第 5 步换成 api_keys 表。
    DEV_API_KEY: str = "dev-key"
    DEV_TENANT_ID: str = "tenant_demo"
    # 离线测试 / CI 不启 MCP 会话管理器（避免每个 TestClient 都启停 session manager）。
    # 生产默认开启；仅 pytest 通过环境变量关掉。
    ENABLE_MCP: bool = True


settings = Settings()
