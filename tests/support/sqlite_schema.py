"""SQLite 夹具用的最小建表语句（第 17 步现场九·第二步）。

## 为什么需要它

索引流水线写完切片时要记下「这一代产物是用哪份切块参数算出来的」，于是它会按主键读一次
``knowledge_bases``。而好几个用例的 SQLite 夹具只建了 ``documents`` / ``chunks`` ——
缺这张表时症状是 ``no such table: knowledge_bases``，与业务无关，纯粹是夹具没跟上。

## 为什么是手写 DDL 而不是 ``KnowledgeBase.__table__.create()``

模型上的 ``allowed_formats`` / ``settings`` 是 **JSONB**，而 SQLite 的类型编译器不认它
（``can't render element of type JSONB``）。用例要的是一个「有那张表、能被 SELECT」的形状，
不是精确的列类型——所以手写一份 SQLite 能吃的 DDL，比给模型加方言分支干净。

**列要与模型同名**：ORM 的 SELECT 会把每一列都点出来，缺一列同样是 "no such column"。
"""

from __future__ import annotations

#: ``knowledge_bases`` 的 SQLite 版（列名与 ``app/models/knowledge_base.py`` 一致）。
KNOWLEDGE_BASES_DDL = """
CREATE TABLE knowledge_bases (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    split_strategy VARCHAR(32) NOT NULL DEFAULT 'structure',
    allowed_formats JSON,
    settings JSON,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
)
"""
