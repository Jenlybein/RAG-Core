"""文档状态：整数常量，1 成功 / 2 失败 / 3 处理中。

用整数不用字符串：省空间、免拼写错误、前端可直接 status === 3。
代码里一律写 DocumentStatus.PROCESSING，不要写裸的 3。
"""


class DocumentStatus:
    """文档处理状态常量。"""

    SUCCESS = 1
    FAILED = 2
    PROCESSING = 3
