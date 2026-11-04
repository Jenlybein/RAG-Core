"""FastAPI 应用入口。

骨架期只挂 3 个接口，多一个都不挂：
  POST /api/v1/knowledge-bases/create
  POST /api/v1/documents/upload
  POST /api/v1/rag/retrieve

/docs 与 /openapi.json 由 FastAPI 自动提供。
"""

from fastapi import FastAPI

from app.api.router import api_router

app = FastAPI(title="RAG-Core", version="0.1.0")
app.include_router(api_router)


@app.get("/health", include_in_schema=False)
async def health() -> dict:
    """仅用于探活，不算业务接口，不出现在 /docs 里。"""

    return {"status": "ok"}
