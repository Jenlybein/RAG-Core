"""反馈接口的请求 / 响应模型（#105）。

请求四件事：哪一次检索（``log_id``）、它对应的那根线（``trace_id``）、几分（``score``）、
一句解释（``comment``，可空）。``tenant_id`` **不进请求体**：它由服务端从 API Key 解出
（``get_current_key``），与检索接口同一条规矩——硬传一个租户标识会被 ``extra="forbid"``
当场拒掉（``20002``），而不是被静默忽略。

**``score`` 的范围不写在这里**。写成一个 ``Field(ge=1, le=5)`` 看着更省事，但那样越界会
先撞上参数校验失败（``20002 request validation error``），而契约要的是
``20022 feedback invalid score``——「你填了个 6」和「你少传了一个字段」对调用方是两件事。
所以这里只校验类型，范围由 ``app.services.retrieval_feedback`` 判定。

``comment`` 的长度上限与列宽一致（512）：超了回 ``20002``，而不是在写库时被数据库截断
——静默截断会让调用方以为自己写的评论完整存下来了。
"""

from pydantic import BaseModel, ConfigDict, Field


class FeedbackRequest(BaseModel):
    """对某一次检索打分。"""

    model_config = ConfigDict(extra="forbid")

    log_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    #: 1~5。范围校验在服务层（越界要回 ``20022``，见模块文档）。
    score: int
    #: 可空：只想打分不想解释时就不传。
    comment: str | None = Field(default=None, max_length=512)


class FeedbackData(BaseModel):
    """提交结果：回显**最终**分数（同一 ``log_id`` 重复提交是覆盖，不是新增）。"""

    log_id: str
    score: int
    comment: str | None = None
