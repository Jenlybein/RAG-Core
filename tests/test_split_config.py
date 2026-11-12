"""切块配置：建库时选策略与窗口，并且真的改变索引期的切法。

三个接缝各测一件事：HTTP 接口（能否传进、回显、非法值被拒）、建库服务
（配置是否真的落进知识库对象）、切块器工厂（两种策略产出的切片形态确实不同）。
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.models.knowledge_base import (
    ALL_FORMATS,
    DEFAULT_CHUNK_SIZE,
    FORMAT_MARKDOWN,
    SPLIT_SIZE,
    SPLIT_STRUCTURE,
    resolve_formats,
    settings_of,
)
from app.utils.markdown_splitter import MarkdownStructuredSplitter
from app.utils.splitter_factory import build_splitter
from app.utils.text_splitter import WindowTextSplitter
from tests import fake_db
from tests.fake_db import ZERO_COUNT

CREATE_PATH = "/api/v1/knowledge-bases/create"
LIST_PATH = "/api/v1/knowledge-bases"
AUTH_HEADERS = {"Authorization": f"Bearer {settings.DEV_API_KEY}"}

MARKDOWN_DOC = (
    "# 部署指南\n\n引导段落。\n\n"
    "## 现场勘查\n\n正文甲。\n\n"
    "### 机房位置\n\n正文乙。\n\n"
    "### 上行链路\n\n正文丙。\n"
)


class _FakeSession:
    """只记录 add/flush/commit 的会话；让真实的建库服务跑完，但不碰数据库。"""

    def __init__(self) -> None:
        self.info: dict[str, Any] = {}
        self.added: list[Any] = []
        self.commits = 0

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        # 真实 flush 会填上 Column default 的时间戳；这里照做，否则响应序列化失败。
        for obj in self.added:
            if getattr(obj, "created_at", None) is None:
                obj.created_at = datetime(2024, 1, 2, 3, 4, 5)

    def execute(self, _statement: Any) -> Any:
        # 建库前的静态配额（库数）会数一次行：替身回 0，行为与空库一致。
        return ZERO_COUNT

    def commit(self) -> None:
        self.commits += 1


@pytest.fixture()
def client() -> Any:
    """用假会话替代 get_db：鉴权与参数校验走真实代码路径。"""

    main = importlib.import_module("app.main")
    session = importlib.import_module("app.db.session")
    fake = _FakeSession()

    main.app.dependency_overrides[session.get_db] = lambda: fake


    main.app.dependency_overrides[session.get_transaction] = fake_db.no_transaction
    try:
        with TestClient(main.app) as test_client:
            test_client.fake_session = fake  # type: ignore[attr-defined]
            yield test_client
    finally:
        main.app.dependency_overrides.clear()


def _create(client: Any, **payload: Any) -> Any:
    return client.post(CREATE_PATH, json={"name": "demo", **payload}, headers=AUTH_HEADERS)


def test_strategy_is_required(client: Any) -> None:
    """建库必须选策略：缺了它就没有「这个库怎么切」这个答案。"""

    response = _create(client)

    assert response.json()["code"] != 0, "不传策略不该被静默套上默认值"


def test_structure_default_whitelist_is_markdown_only(client: Any) -> None:
    """structure 不传白名单时默认只收 markdown：它的能力全建立在标题语法上。"""

    response = _create(client, split_strategy=SPLIT_STRUCTURE)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["split_strategy"] == SPLIT_STRUCTURE
    assert data["chunk_size"] == DEFAULT_CHUNK_SIZE == 512
    assert data["allowed_formats"] == [FORMAT_MARKDOWN]


def test_size_default_whitelist_is_every_format(client: Any) -> None:
    """size 不传白名单时五种格式全开：它不依赖任何结构。"""

    response = _create(client, split_strategy=SPLIT_SIZE)

    assert response.status_code == 200
    assert response.json()["data"]["allowed_formats"] == list(ALL_FORMATS)


def test_size_strategy_and_window_are_echoed(client: Any) -> None:
    """显式传「按 size 切」与窗口时，响应回显的是实际生效值。"""

    response = _create(client, split_strategy="size", chunk_size=400)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["split_strategy"] == SPLIT_SIZE
    assert data["chunk_size"] == 400


def test_size_can_narrow_its_whitelist(client: Any) -> None:
    """size 库可以把白名单收窄到只收某几种格式。"""

    response = _create(client, split_strategy=SPLIT_SIZE, allowed_formats=["pdf"])

    assert response.status_code == 200
    assert response.json()["data"]["allowed_formats"] == ["pdf"]


def test_structure_rejects_non_markdown_whitelist(client: Any) -> None:
    """structure 配 pdf 是矛盾组合，建库当场拒绝而不是建出一个必然出错的值。"""

    response = _create(client, split_strategy=SPLIT_STRUCTURE, allowed_formats=["markdown", "pdf"])

    assert response.json()["code"] != 0


@pytest.mark.parametrize(
    "payload",
    [
        {"split_strategy": "structured"},  # 旧命名，不是合法取值
        {"split_strategy": "char"},
        {"split_strategy": SPLIT_STRUCTURE, "chunk_size": 100},
        {"split_strategy": SPLIT_STRUCTURE, "chunk_size": 5000},
        # 格式名拼错：白名单是枚举，不许自由文本
        {"split_strategy": SPLIT_STRUCTURE, "allowed_formats": ["markdown", "word"]},
        # 空白名单等于这个库什么都不收，与「建库」的目的矛盾
        {"split_strategy": SPLIT_STRUCTURE, "allowed_formats": []},
    ],
)
def test_invalid_split_config_is_rejected(client: Any, payload: dict[str, Any]) -> None:
    """非法策略、越界数值、拼错的格式名都被拒绝，不静默回落到默认。"""

    response = _create(client, **payload)

    assert response.json()["code"] != 0


def test_config_is_persisted_on_the_knowledge_base_row(client: Any) -> None:
    """配置真的落进了知识库行：策略与白名单是独立列，数值进 settings。"""

    _create(client, split_strategy="size", chunk_size=400)

    stored = client.fake_session.added[0]  # type: ignore[attr-defined]
    assert stored.split_strategy == "size"
    assert stored.allowed_formats == list(ALL_FORMATS)
    assert stored.settings == {"chunk_size": 400}


def test_list_shows_split_strategy_and_window(client: Any, monkeypatch) -> None:
    """列库要能看到每个库用哪种策略、窗口多大、收哪些格式。"""

    repo = importlib.import_module("app.repositories.knowledge_base_repository")

    def fake_list(db, tenant_id):
        return [
            SimpleNamespace(
                id="kb-1",
                name="结构库",
                tenant_id=tenant_id,
                split_strategy="structure",
                allowed_formats=["markdown"],
                settings={"chunk_size": 800},
            ),
            SimpleNamespace(
                id="kb-2",
                name="字符库",
                tenant_id=tenant_id,
                split_strategy="size",
                allowed_formats=list(ALL_FORMATS),
                settings={"chunk_size": 400},
            ),
        ]

    monkeypatch.setattr(repo, "list_by_tenant", fake_list)

    response = client.get(LIST_PATH, headers=AUTH_HEADERS)

    items = response.json()["data"]["knowledge_bases"]
    assert [
        (item["split_strategy"], item["chunk_size"], item["allowed_formats"]) for item in items
    ] == [
        ("structure", 800, ["markdown"]),
        ("size", 400, list(ALL_FORMATS)),
    ]


def test_size_strategy_really_ignores_headings() -> None:
    """「按 size 切」是纯字符硬切：标题不再是边界，也不产出章节路径。"""

    size_splitter = build_splitter(SPLIT_SIZE, chunk_size=120)
    structure_splitter = build_splitter(SPLIT_STRUCTURE, chunk_size=120)

    assert isinstance(size_splitter, WindowTextSplitter)

    size_pieces = size_splitter.split(MARKDOWN_DOC)
    structure_pieces = structure_splitter.split(MARKDOWN_DOC)

    # 结构化切法为每个 h2/h3 各出一片，路径里有层级；字符切法根本没有章节路径。
    assert any(" > " in str(piece.metadata.get("heading_path")) for piece in structure_pieces)
    assert all(piece.metadata.get("heading_path") in (None, "") for piece in size_pieces)


def test_size_strategy_does_not_start_a_piece_per_heading() -> None:
    """标题在「按 size 切」下不是边界：它只是正文里的普通一行。"""

    splitter = build_splitter(SPLIT_SIZE, chunk_size=2000)

    pieces = splitter.split(MARKDOWN_DOC)

    assert len(pieces) == 1, "整篇不足窗口，按 size 切只该出一片"
    # 三个标题全在同一片里，没有被当成切点。
    for heading in ("## 现场勘查", "### 机房位置", "### 上行链路"):
        assert heading in pieces[0].text


def test_unknown_strategy_falls_back_to_structure() -> None:
    """手工改库写进非法值时回落而不是抛错：切法不理想好过文档永远进不了库。"""

    splitter = build_splitter("nonsense")

    assert isinstance(splitter, MarkdownStructuredSplitter)


def test_settings_out_of_range_falls_back_to_defaults() -> None:
    """settings 里的越界数值回落默认，不让脏数据毁掉整条索引链路。"""

    kb = SimpleNamespace(settings={"chunk_size": 99999})

    values = settings_of(kb)

    assert values == {"chunk_size": DEFAULT_CHUNK_SIZE}


def test_legacy_merge_threshold_key_is_ignored() -> None:
    """旧数据里残留的 merge_threshold 被忽略：不报错、也不影响切块配置。

    这个键属于被撤掉的「构建期合并」（逻辑上不可能触发，见切块器注释）。
    存量库里还留着它，读配置时不能因为它而失败。
    """

    kb = SimpleNamespace(settings={"chunk_size": 800, "merge_threshold": 150})

    assert settings_of(kb) == {"chunk_size": 800}


def test_missing_kb_settings_yields_defaults() -> None:
    """库不存在时取配置回落默认，把「库不存在」留给后续流程去报。"""

    assert settings_of(None) == {"chunk_size": DEFAULT_CHUNK_SIZE}


def test_indexing_service_builds_splitter_from_the_kb_config(monkeypatch) -> None:
    """索引期按文档所属知识库的配置选切块器，而不是全局配置。"""

    service_module = importlib.import_module("app.services.indexing_service")
    repo = importlib.import_module("app.repositories.knowledge_base_repository")

    async def _split_config(_db, _kb_id, *, tenant_id: str):
        return (SPLIT_SIZE, 400)

    # 票 #132 起索引流水线走异步会话，库级切块配置读的是异步孪生。
    monkeypatch.setattr(repo, "async_get_split_config", _split_config)

    service = service_module.IndexingService(
        splitter=None,
        embeddings=SimpleNamespace(),
        stores=[],
    )
    doc = SimpleNamespace(kb_id="kb-1", tenant_id="tenant-demo")

    splitter, strategy = asyncio.run(service._resolve_splitter(None, doc))

    assert strategy == SPLIT_SIZE
    assert isinstance(splitter, WindowTextSplitter)
    assert splitter.chunk_size == 400


def test_injected_splitter_wins_over_kb_config(monkeypatch) -> None:
    """注入了切块器时以注入为准：测试靠它把切块与接线解耦。"""

    service_module = importlib.import_module("app.services.indexing_service")
    injected = MarkdownStructuredSplitter(chunk_size=300, chunk_overlap=50)

    service = service_module.IndexingService(
        splitter=injected,
        embeddings=SimpleNamespace(),
        stores=[],
    )
    doc = SimpleNamespace(kb_id="kb-1", tenant_id="tenant-demo")

    splitter, strategy = asyncio.run(service._resolve_splitter(None, doc))

    assert splitter is injected
    assert strategy == SPLIT_STRUCTURE


def test_resolve_formats_fills_defaults_per_strategy() -> None:
    """不传白名单时按策略补全：structure 只 markdown，size 五种全开。"""

    assert resolve_formats(SPLIT_STRUCTURE, None) == [FORMAT_MARKDOWN]
    assert resolve_formats(SPLIT_SIZE, None) == list(ALL_FORMATS)


@pytest.mark.parametrize(
    "strategy, formats",
    [
        (SPLIT_SIZE, ["word"]),  # 拼错的名字
        (SPLIT_SIZE, []),  # 空白名单等于什么都不收
        (SPLIT_SIZE, ["markdown", "markdown"]),  # 重复项
        (SPLIT_STRUCTURE, ["markdown", "pdf"]),  # 矛盾组合
        (SPLIT_STRUCTURE, []),  # structure 也不能什么都不收
    ],
)
def test_resolve_formats_rejects_bad_whitelists(strategy: str, formats: list[str]) -> None:
    """白名单的四类非法值都在写库之前被拒。"""

    with pytest.raises(ValueError):
        resolve_formats(strategy, formats)


def test_resolve_formats_keeps_declared_order() -> None:
    """合法白名单按传入顺序保留：顺序即优先级，不该被重排。"""

    assert resolve_formats(SPLIT_SIZE, ["pdf", "docx"]) == ["pdf", "docx"]


def test_resolve_formats_rejects_unknown_strategy() -> None:
    """未知策略名不能被当成某个已知策略处理。"""

    with pytest.raises(ValueError):
        resolve_formats("nonsense", None)
