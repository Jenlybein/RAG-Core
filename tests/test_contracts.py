"""T0 契约底座：切块结果 / 解析结果 / 配置项 / embedding 批次。

全部离线：只构造对象、调纯函数，不连 PostgreSQL、不发真实模型请求。
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.providers.embedding.openai_compatible import OpenAICompatibleEmbedding
from app.providers.parsers.base import ParsedDocument
from app.utils.text_splitter import SplitPiece, WindowTextSplitter
from app.utils.tokenizer import token_count


def test_window_splitter_returns_pieces_with_empty_metadata() -> None:
    """`size` 策略的契约：只出片、不带结构化信息、遵守 token 窗口。

    窗口单位是 token 而不是字符，所以「不超过窗口」要按 token 判——按字符判会让
    中文片悄悄变长 1.5–1.9 倍。
    """

    splitter = WindowTextSplitter(chunk_size=20, chunk_overlap=5)
    body = "第一句话在这里。第二句话在这里。第三句话在这里。第四句话在这里。"
    pieces = splitter.split(body)

    assert all(isinstance(piece, SplitPiece) for piece in pieces)
    assert all(piece.metadata == {} for piece in pieces), "size 库不产任何章节信息"
    assert all(token_count(piece.text) <= 20 for piece in pieces), "片不得超过窗口"
    assert all(piece.text.strip() for piece in pieces), "不该产出空白片"
    assert splitter.split("") == []


def test_parsed_document_metadata_is_independent_per_instance() -> None:
    first = ParsedDocument(content="a", source_type="text")
    second = ParsedDocument(content="b", source_type="markdown")

    assert first.metadata == {}
    assert second.metadata == {}
    first.metadata["parser"] = "x"
    assert second.metadata == {}, "metadata 不能是共享的可变默认值"


def test_settings_expose_t0_config_defaults() -> None:
    settings = Settings(_env_file=None)

    expected = {
        "CELERY_BROKER_URL": "redis://localhost:6379/0",
        "CELERY_RESULT_BACKEND": "redis://localhost:6379/1",
        "DOCUMENT_STORAGE_PATH": "./data/uploads",
        "DOCUMENT_MAX_SIZE_MB": 20,
        "TABLE_MAX_ROWS_PER_CHUNK": 10,
        "EMBEDDING_BATCH_SIZE": 10,
        "OCR_MODE": "auto",
        "OCR_OFFLINE": False,
        "OCR_MODEL_DIRECTORY": "",
        "PDFIUM_LIB_PATH": "",
        "ORT_DYLIB_PATH": "",
    }

    for name, default in expected.items():
        assert getattr(settings, name) == default


def test_embedding_provider_rejects_non_positive_batch_size() -> None:
    with pytest.raises(ValueError):
        OpenAICompatibleEmbedding(
            base_url="http://localhost:1/v1",
            api_key="test-key",
            model="test-model",
            dimensions=3,
            batch_size=0,
        )
