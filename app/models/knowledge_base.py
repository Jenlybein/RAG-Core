"""表 1：knowledge_bases —— 文档的归属容器，也是检索的范围单位。

切块配置（策略 / 窗口 / 合并阈值）挂在这里而不是文档上：同一个库里混用两种
切法会让片长分布不一致，检索时「片长」会变成与内容无关的排序因素。
"""

from datetime import datetime
from typing import Final, Literal

from sqlalchemy import CheckConstraint, DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, now

SplitStrategy = Literal["structure", "size"]

# 切块策略的合法取值。放在模型层而不是各处重复字面量：路由校验、索引期分支、
# 迁移里的 CHECK 约束都引用同一个来源。标注 Literal 是为了让 Pydantic 字段的
# 默认值不被 mypy 判成「str 赋给 Literal」。
SPLIT_STRUCTURE: Final[SplitStrategy] = "structure"
SPLIT_SIZE: Final[SplitStrategy] = "size"
SPLIT_STRATEGIES: Final[tuple[SplitStrategy, ...]] = (SPLIT_STRUCTURE, SPLIT_SIZE)

# 建库默认值。800 与既有实测基线一致，改成别的值会让历史评测不可比。
DEFAULT_CHUNK_SIZE = 800

# 数值旋钮的范围。放模型层是为了让路由（Pydantic）与索引期兜底共用同一组边界，
# 避免两处各写一份然后慢慢漂移。
MIN_CHUNK_SIZE = 200
MAX_CHUNK_SIZE = 2000


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (
        CheckConstraint(
            "split_strategy IN ('size', 'structure')",
            name="ck_knowledge_bases_split_strategy",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 切块策略是**知识库级**属性，建库时定一次；本步不支持改。
    split_strategy: Mapped[str] = mapped_column(
        String(32), default=SPLIT_STRUCTURE, server_default=SPLIT_STRUCTURE
    )
    # chunk_size 存在 settings 里，读取统一走 settings_of()。
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


def settings_of(kb: KnowledgeBase | None) -> dict[str, int]:
    """从 settings 里取切块数值旋钮，缺失或越界时回落到默认值。

    越界值回落而不是抛错：正常路径上这些值由 Pydantic 挡住，能读到越界值只可能
    是手工改库或历史数据。带着默认值继续索引，好过让文档永远进不了库。
    """

    raw = (kb.settings if kb is not None else None) or {}
    chunk_size = raw.get("chunk_size", DEFAULT_CHUNK_SIZE)
    if not isinstance(chunk_size, int) or not MIN_CHUNK_SIZE <= chunk_size <= MAX_CHUNK_SIZE:
        chunk_size = DEFAULT_CHUNK_SIZE
    return {"chunk_size": chunk_size}
