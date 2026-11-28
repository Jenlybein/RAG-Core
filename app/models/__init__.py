"""ORM 模型统一导出。

alembic 的 env.py 要 import 这个模块，确保每张表都注册到 Base.metadata，
否则 --autogenerate 看不到它们。

前四张是业务表（知识库 / 文档 / 切片 / 归属索引）；第 7 步的七张身份与控制面表
（租户 / 套餐 / 账号 / 密钥配置 / API Key / 会话 / 审计）同样在这里登记，一张都不能漏。
第 8 步（#93）补上运行时配置覆盖表 ``runtime_settings``；#94 再补多模型配置表
``model_configs``；第 9 步（#105）再补反馈表 ``retrieval_feedback``；第 11 步（#141）
的索引构建版本表 ``index_build_versions`` 已在第 17 步整层删除（库级生效版本退场，
可见性改由篇级当前代表达，见 ADR-0025 第 2 次修订）；第 12 步（#172）再补文档版本表
``document_versions``（一稿一行）；第 14 步（#199）把归属索引改名 ``usage_events``
并补按天汇总表 ``usage_daily``（一行一次的账 + 13 个月的回溯）。
"""

from app.models.account import Account
from app.models.api_key import ApiKey
from app.models.audit_log import AuditLog
from app.models.chunk import Chunk
from app.models.console_session import ConsoleSession
from app.models.document import Document
from app.models.document_version import DocumentVersion
from app.models.key_policy import KeyPolicy
from app.models.knowledge_base import KnowledgeBase
from app.models.model_config import ModelConfig
from app.models.plan import Plan
from app.models.retrieval_feedback import RetrievalFeedback
from app.models.runtime_setting import RuntimeSetting
from app.models.usage_daily import UsageDaily
from app.models.usage_event import UsageEvent
from app.models.tenant import Tenant

__all__ = [
    "Account",
    "ApiKey",
    "AuditLog",
    "Chunk",
    "ConsoleSession",
    "Document",
    "DocumentVersion",
    "KeyPolicy",
    "KnowledgeBase",
    "ModelConfig",
    "Plan",
    "RetrievalFeedback",
    "RuntimeSetting",
    "Tenant",
    "UsageDaily",
    "UsageEvent",
]
