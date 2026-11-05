"""配置收口：所有环境变量只在这里读一次。

骨架期只取本阶段需要的 11 个变量；阶段 3 起才有 Redis / Celery / ES /
Langfuse 的配置项，届时往 Settings 里追加字段即可。

.env 里有 40 多个变量，pydantic-settings 默认会忽略未声明的键
（extra="ignore"），所以阶段 2~11 的配置不会让本步报错。
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

    CHUNK_SIZE: int = 800
    CHUNK_OVERLAP: int = 100
    TOP_K: int = 5

    # 结构化日志（T2）：只新增这两项，滚动阈值/份数是 setup_logging 的参数。
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "logs"


settings = Settings()
