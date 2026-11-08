"""页号识别：从「第 N 页」这样的标题里读出物理页号。

PDF 解析器为每页产出一个 ``## 第 N 页`` 标题。**它不是父级标题，而是与页内正文
标题同级的兄弟**——所以页号不会自然出现在 ``heading_path`` 里，必须由切块器
把它当成「跨节的属性」一路带下去。

这一点是实测撞出来的：先按「正则扫 heading_path」实现，结果三页文档切出来的片
页号全是空，因为「第 2 页」那一行在栈里被同级的「## 上架步骤」顶掉了。
"""

from __future__ import annotations

import re

_PAGE_TITLE_RE = re.compile(r"^第\s*(\d+)\s*页\s*$")


def page_from_heading(title: str) -> int | None:
    """标题若是纯「第 N 页」则返回页号，否则返回 None。

    只认**整行就是页标记**的标题。正文里出现「见第 3 页」这种引用不应改变
    当前页号，否则引用句会把后续整节都标到错误页上。
    """

    match = _PAGE_TITLE_RE.match((title or "").strip())
    return int(match.group(1)) if match else None
