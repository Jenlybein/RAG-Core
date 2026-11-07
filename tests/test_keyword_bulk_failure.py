"""ES bulk「部分失败」必须是一个**可重试**的业务错误，且文案不带响应体内容。

bulk 是部分成功的接口：只要有一条文档没写进去，`response["errors"]` 就是真。
索引阶段因此必须失败——本文件守的是这个失败**长什么样**：

* 它必须是 ``AppError(API_REQUEST_ERROR)``，也就是 ``tasks/indexing`` 的
  ``RETRYABLE_ERROR_CODES`` 里的成员。抛裸 ``RuntimeError`` 会被
  ``_is_retryable`` 判成「不可重试」，于是 ES 抖一下就把文档钉成失败态；
* 文案固定，不带响应体里的任何内部细节（mapping 报错、字段名、文档 id……）：
  Worker 的失败留痕只存固定文案，异常里的东西会经日志与重试记录扩散出去。

离线：注入假客户端，不连 ES（真实引擎的冒烟在 ``tests/test_keyword_search_es.py``）。
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.exceptions import AppError, ErrorCode
from app.providers.keyword_search.elasticsearch import ElasticsearchKeywordSearchProvider
from app.tasks.indexing import _is_retryable

TENANT_ID = "tenant_demo"
KB_ID = "kb-a"

# 响应体里可能夹带的一切内部细节：换一个值也不该出现在异常文案里。
BULK_DETAIL = "mapper_parsing_exception: failed to parse field [retrieval_text]"
EXPECTED_MESSAGE = "elasticsearch bulk indexing reported errors"


class _FakeIndices:
    async def exists(self, **kwargs) -> bool:
        return True


class _FailingBulkClient:
    """bulk 回一份「有 errors」的响应；其余方法用不到。"""

    def __init__(self, response: dict) -> None:
        self.indices = _FakeIndices()
        self._response = response

    async def bulk(self, **kwargs) -> dict:
        return self._response


def _provider(client: _FailingBulkClient) -> ElasticsearchKeywordSearchProvider:
    return ElasticsearchKeywordSearchProvider(
        url="http://es.example:9200", index_name="rag_chunks", client=client
    )


def _chunk() -> dict:
    return {
        "id": "chunk-1",
        "tenant_id": TENANT_ID,
        "kb_id": KB_ID,
        "document_id": "doc-1",
        "content": "切片原文",
        "retrieval_text": "文档：售后手册 正文：切片原文",
        "index_version": "v1",
        "meta": {"title": "售后手册"},
    }


def test_bulk_partial_failure_is_a_retryable_business_error() -> None:
    """bulk 报错 → ``API_REQUEST_ERROR``，且 ``_is_retryable`` 认得它。"""

    client = _FailingBulkClient(
        {
            "errors": True,
            "items": [
                {
                    "index": {
                        "_id": "chunk-1",
                        "status": 400,
                        "error": {"type": "mapper_parsing_exception", "reason": BULK_DETAIL},
                    }
                }
            ],
        }
    )

    with pytest.raises(AppError) as excinfo:
        asyncio.run(_provider(client).add_chunks([_chunk()]))

    assert excinfo.value.code is ErrorCode.API_REQUEST_ERROR
    assert _is_retryable(excinfo.value) is True, "ES 抖一下要能重试，不能把文档钉成失败态"


def test_bulk_failure_message_carries_no_response_detail() -> None:
    """文案固定：响应体里的原因、字段名、文档 id 一律不出现。"""

    client = _FailingBulkClient(
        {
            "errors": True,
            "items": [
                {
                    "index": {
                        "_id": "chunk-1",
                        "status": 400,
                        "error": {"type": "mapper_parsing_exception", "reason": BULK_DETAIL},
                    }
                }
            ],
        }
    )

    with pytest.raises(AppError) as excinfo:
        asyncio.run(_provider(client).add_chunks([_chunk()]))

    assert excinfo.value.msg == EXPECTED_MESSAGE, "固定文案，不随响应体变化"
    for leak in (BULK_DETAIL, "mapper_parsing_exception", "chunk-1", "retrieval_text"):
        assert leak not in str(excinfo.value), f"异常文案里不许带内部细节：{leak}"
    # 链上去的原始异常也不该被当成文案的一部分：这里根本没有 chain。
    assert excinfo.value.__cause__ is None
