# 部署脚本与配置参考

本页收录部署时用到的脚本片段与配置样例，用于验证切块器对不同代码块的切分行为。

## 索引重建脚本

下面这段把全库文档重新入队，包含失败重试与并发上限控制。

```python
import time
from collections.abc import Iterable

import requests

BASE_URL = "http://127.0.0.1:8000/api/v1"
API_KEY = "dev-key"
MAX_CONCURRENCY = 4
RETRY_LIMIT = 3
RETRY_BACKOFF = [1, 3, 7]


def list_documents(kb_id: str) -> list[dict]:
    """列出某个知识库下的全部文档，翻页直到取空。"""
    documents: list[dict] = []
    page = 1
    while True:
        response = requests.get(
            f"{BASE_URL}/documents",
            params={"kb_id": kb_id, "page": page, "page_size": 50},
            headers={"Authorization": f"Bearer {API_KEY}"},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()["data"]
        items = payload["documents"]
        if not items:
            return documents
        documents.extend(items)
        page += 1


def reindex_batch(documents: Iterable[dict]) -> dict[str, int]:
    """逐篇重建索引，失败按退避表重试；统计成功与最终失败。"""
    stats = {"ok": 0, "skip": 0, "failed": 0, "retried": 0}
    for document in documents:
        if document["status"] == "success":
            stats["skip"] += 1
            continue
        for attempt, delay in enumerate(RETRY_BACKOFF, start=1):
            response = requests.post(
                f"{BASE_URL}/documents/{document['document_id']}/reindex",
                params={"force": "true", "reparse": "false"},
                headers={"Authorization": f"Bearer {API_KEY}"},
                timeout=60,
            )
            if response.status_code == 200 and response.json()["code"] == 0:
                stats["ok"] += 1
                break
            stats["retried"] += 1
            time.sleep(delay)
            if attempt == RETRY_LIMIT:
                stats["failed"] += 1
    return stats


if __name__ == "__main__":
    for kb_id in ("kb-eval", "kb-eval-ops"):
        docs = list_documents(kb_id)
        print(kb_id, len(docs), reindex_batch(docs))
```

## 检索服务的配置样例

检索相关旋钮集中在下面这份配置里，改任何一项都要重建索引。

```json
{
  "retrieval": {
    "top_k": 5,
    "overfetch_factor": 3,
    "rrf_k": 60,
    "candidate_limit": 100,
    "vector_weight": 1.0,
    "keyword_weight": 1.0,
    "degrade_to_vector_on_keyword_failure": true
  },
  "rerank": {
    "enabled": true,
    "model": "qwen3-rerank",
    "candidate_size": 20,
    "content_truncate_chars": 1024,
    "timeout_seconds": 15,
    "failure_mode": "fallback_to_fusion"
  },
  "chunking": {
    "default_chunk_size": 800,
    "default_chunk_overlap": 100,
    "table_max_rows_per_chunk": 10,
    "overlap_must_be_less_than_size": true
  }
}
```

## 容器的编排片段

下面是 Worker 与依赖服务的最小编排，注意存储卷必须在 API 与 Worker 两侧共享。

```
services:
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_PASSWORD: 123456
      POSTGRES_DB: rag_db
    volumes:
      - pg_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d rag_db"]
      interval: 5s
      retries: 10

  worker:
    build:
      context: ..
      dockerfile: deploy/Dockerfile
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      DATABASE_URL: postgresql+psycopg://postgres:123456@postgres:5432/rag_db
      DOCUMENT_STORAGE_PATH: /app/data/uploads
    volumes:
      - ./data/uploads:/app/data/uploads
    command: celery -A app.celery_app:celery_app worker --concurrency=2

volumes:
  pg_data:
```

## 常见故障的对照

排查时先看状态列，再看任务日志。

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| 状态一直是处理中 | Worker 没起 | 起 Worker 容器，看 `celery@<id> ready` |
| 状态失败且错误码 20023 | 扩展名不在白名单 | 换格式或换库 |
| 状态失败且错误码 20024 | 整篇抽不出文字 | 检查是否扫描件、OCR 是否可用 |
| 检索返回空且原因 no_indexed_chunks | 库是空的 | 先上传文档再检索 |
