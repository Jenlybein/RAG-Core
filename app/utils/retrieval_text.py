"""检索文本拼装：索引期那份「送进索引的文本」。

文档名 + 章节路径 + 正文，形如「文档：X 章节：Y 正文：Z」（见 GLOSSARY 的
「检索文本」）。它与**切片原文**不是一回事：切片原文（``chunks.content``）原样存库、
原样返回给调用方；检索文本只在索引期算出来，向量与（后续的）关键词索引共用同一份，
两路对比才有意义。

它是纯函数：不碰数据库、不 import 框架。空的段连同标签一起省略，所以没有章节路径时
绝不会留下「章节：」；三段全空时返回空串（空切片不该拿到一条只由标签组成的向量）。
每段首尾的空白会被去掉，段与段之间用一个空格连接。
"""

import re

_DOCUMENT_LABEL = "文档："
_SECTION_LABEL = "章节："
_CONTEXT_LABEL = "节说明："
_MODEL_LABEL = "型号："
_CONTENT_LABEL = "正文："

# 型号形态：2–4 个大写字母 + 短横 + 3–5 位数字，后面可以跟一个后缀词。
# 覆盖 XR-2000、XR-2000 Pro、SFP-10G、AX-3000 Max 这类写法。
_MODEL_RE = re.compile(
    r"\b[A-Z]{2,4}-\d{1,5}(?:[A-Z]|\s+(?:Pro|Plus|Max|Lite|Mini|SE))?\b"
)


def model_tokens(content: str) -> list[str]:
    """抽出正文里出现过的型号，长型号排在前面。

    为什么需要它：``XR-2000`` 是 ``XR-2000 Pro`` 的子串，两条只剩型号不同的切片
    在向量空间里几乎重合，问题里带着完整型号时反而可能命中较短那条。把型号从正文
    里**抽出来单独成段**（``型号：XR-2000 Pro``），就给了它一个独立、可对齐的位置，
    而不是埋在长句子里。
    """

    tokens = {token.strip() for token in _MODEL_RE.findall(content)}
    # 长型号优先：XR-2000 Pro 命中时，不要再把 XR-2000 也列一遍（子串会被顺带匹配）。
    return sorted(tokens, key=len, reverse=True)


def build_retrieval_text(
    title: str,
    heading_path: str,
    content: str,
    section_context: str | None = None,
    *,
    model_tags: bool = False,
) -> str:
    """拼出检索文本：``文档：X 章节：Y 节说明：S 型号：M 正文：Z``，空段不产生空标签。

    ``section_context`` 是可选的节级说明（见 ``app/services/section_context.py``）：
    它给「这一节在讲什么」一句人话，让「人话提问」也能落到正确的章节。

    ``model_tags`` 打开时把正文里的型号抽出来单独成段，解决 ``XR-2000`` 与
    ``XR-2000 Pro`` 只差一个后缀时的混淆。

    都为空时检索文本与补上这些能力之前逐字一致——它们是可选增强，不是格式变更。
    """

    parts = [
        f"{label}{value.strip()}"
        for label, value in (
            (_DOCUMENT_LABEL, title),
            (_SECTION_LABEL, heading_path),
            (_CONTEXT_LABEL, section_context or ""),
            (_MODEL_LABEL, "、".join(model_tokens(content)) if model_tags else ""),
            (_CONTENT_LABEL, content),
        )
        if value.strip()
    ]
    return " ".join(parts)
