"""表 1：knowledge_bases —— 文档的归属容器，也是检索的范围单位。

切块配置（策略 / 白名单 / 窗口）挂在这里而不是文档上：同一个库里混用两种
切法会让片长分布不一致，检索时「片长」会变成与内容无关的排序因素。

策略同时是**准入规则**：它决定这个库收哪些格式。structure 的全部能力都建立在
Markdown 标题语法上，收 pdf/docx 只会让切块器退化成窗口硬切、白拿不到章节路径，
所以宁可在上传时拒绝，也不要入库之后产出一批「看着像结构切、其实不是」的切片。
"""

from datetime import datetime
from typing import Final, Literal, Sequence

from sqlalchemy import CheckConstraint, DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, now

SplitStrategy = Literal["structure", "size"]
FormatKind = Literal["markdown", "text", "docx", "pdf", "html"]

# 切块策略的合法取值。放在模型层而不是各处重复字面量：路由校验、索引期分支、
# 迁移里的 CHECK 约束都引用同一个来源。标注 Literal 是为了让 Pydantic 字段的
# 默认值不被 mypy 判成「str 赋给 Literal」。
SPLIT_STRUCTURE: Final[SplitStrategy] = "structure"
SPLIT_SIZE: Final[SplitStrategy] = "size"
SPLIT_STRATEGIES: Final[tuple[SplitStrategy, ...]] = (SPLIT_STRUCTURE, SPLIT_SIZE)

FORMAT_MARKDOWN: Final[FormatKind] = "markdown"

# 与解析器产出的 source_type 一一对应（markdown / text / docx / pdf / html）。
# 一一对应是刻意的：上传时拿 source_type 直接比对白名单，不需要中间映射表，
# 少一层就少一处会漂移的地方。
ALL_FORMATS: Final[tuple[FormatKind, ...]] = ("markdown", "text", "docx", "pdf", "html")

# 策略 → 白名单默认值。structure 只收 markdown；size 不依赖结构，所以全收。
# 值类型写成 tuple[str, ...] 而不是 tuple[FormatKind, ...]：调用方（校验与读取）
# 一律按 list[str] 处理，避免让字面量类型漏进接口签名。
DEFAULT_FORMATS_BY_STRATEGY: Final[dict[str, tuple[str, ...]]] = {
    SPLIT_STRUCTURE: (FORMAT_MARKDOWN,),
    SPLIT_SIZE: ALL_FORMATS,
}

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
    # 切块策略是**知识库级**属性，建库时必须显式选择、没有默认值：它同时决定
    # 这个库收哪些格式（见 allowed_formats）。本步不支持改。
    split_strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    # 允许上传的格式白名单。与策略的一致性由 resolve_formats 保证，**不在数据库
    # 加 CHECK**：JSONB 的包含判断写进约束后报错不友好，也表达不出
    # 「structure 必须恰好是 [markdown]」这种形状。
    allowed_formats: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    # chunk_size 存在 settings 里，读取统一走 settings_of()。
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


def resolve_formats(strategy: str, formats: Sequence[str] | None) -> list[str]:
    """按策略补全并校验白名单；非法组合抛 ``ValueError``。

    建库时的唯一判定点：把请求补成最终值，并拒绝矛盾组合。写成纯函数，是为了在
    没有数据库、没有 HTTP 的情况下也能把全部分支测完。

    上传侧**不再调用它**：读库时由 :func:`formats_of` 按策略强制（`structure` 一律
    返回 `[markdown]`，不看列里存了什么）。两道防线的分工是——建库挡住写入，
    读取挡住「列被手工改坏」；只靠哪一道都会留一个洞。

    六种非法情形都拒绝，而不是挑一个凑合：策略名不认识、格式名拼错、空白名单、
    重复项、structure 配了非 markdown、以及「不传」以外的任何歧义。
    """

    if strategy not in SPLIT_STRATEGIES:
        raise ValueError(f"未知的切块策略：{strategy}")
    if formats is None:
        return list(DEFAULT_FORMATS_BY_STRATEGY[strategy])

    unknown = [item for item in formats if item not in ALL_FORMATS]
    if unknown:
        raise ValueError(f"不支持的格式：{'、'.join(unknown)}")
    if len(set(formats)) != len(formats):
        raise ValueError("白名单里有重复的格式")
    if not formats:
        raise ValueError("白名单不能为空")
    if strategy == SPLIT_STRUCTURE and set(formats) != {FORMAT_MARKDOWN}:
        raise ValueError("structure 策略只接受 markdown：它的能力都建立在标题语法上")

    return list(formats)


def formats_of(kb: KnowledgeBase | None) -> list[str]:
    """取该库允许的上传格式。

    **策略是唯一决策者**：`structure` 永远返回 `[markdown]`，不看列里存了什么。
    这一条是准入的第二道防线——`resolve_formats` 只在建库时把关，而列可以被手工改
    成 `["pdf"]`。若这里照着列原样返回，那个库就会悄悄收下 pdf 并退化成窗口硬切，
    正是本次要消除的现象。

    `size` 侧仍按列取值（它允许收窄），脏值按策略默认兜底，与 ``settings_of`` 同一
    口径：白名单被改坏时带着默认值继续，好过让整篇文档永远进不了库。
    """

    strategy = (kb.split_strategy if kb is not None else None) or SPLIT_STRUCTURE
    if strategy == SPLIT_STRUCTURE:
        return [FORMAT_MARKDOWN]
    raw = (kb.allowed_formats if kb is not None else None) or []
    formats: list[str] = [item for item in raw if item in ALL_FORMATS]
    if not formats:
        formats = list(DEFAULT_FORMATS_BY_STRATEGY[strategy])
    return formats


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
