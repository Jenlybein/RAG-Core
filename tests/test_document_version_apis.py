"""#175 · 建版本 + 列版本：文档版本管理的第一条用户可见路径。

本文件守的是**接口边界**（第 ② 层，ASGI）：TestClient 发真实请求，只看统一响应体与
会话里留下的行；投递缝与关键词索引缝换成记账器，因此不需要 Broker、不需要 ES。
仓储的写语句另有一组用例在内存 SQLite 上按真实 SQL 跑（第 ⑧ 层的做法，先例
``test_document_version_delete.py``）。

验收项与用例的对应关系（票面顺序）：

* 提交新内容 → 新稿次 + 旧稿归档 + 指针前移 + 文档置处理中，重建在提交**之后**才投递；
* 提交与当前稿相同 → 「内容未变」（20031），版本行数不变，**什么都不写**；
* 提交与历史某稿相同 → 建**新**稿次，不复活旧行；
* 两个并发提交不会得到同一稿号，撞唯一约束映射成「请重试」（20029）而不是 500；
* 文档处理中时拒绝写版本（复用 20025，不新造 ``DOCUMENT_BUSY``）；
* 列版本：按稿号倒序、默认不带正文、正文需显式要求。
"""

from __future__ import annotations

from app.core.exceptions import ErrorCode


def test_the_four_new_version_error_codes_are_the_agreed_numbers() -> None:
    """错误码是**线上词汇**（响应体里就是这个整数），所以期望值写成字面量。

    20032 本票用不到（「已经是当前稿」属于 #176 的回滚 / 激活语义），但词汇位先占好：
    之后再补一个码时，「20032 是什么」不应该有第二种答案。
    """

    assert int(ErrorCode.DOCUMENT_VERSION_CONFLICT) == 20029
    assert int(ErrorCode.DOCUMENT_VERSION_NOT_FOUND) == 20030
    assert int(ErrorCode.DOCUMENT_VERSION_UNCHANGED) == 20031
    assert int(ErrorCode.DOCUMENT_VERSION_ALREADY_ACTIVE) == 20032


def test_the_version_codes_do_not_reuse_the_existing_document_codes() -> None:
    """「文档处理中」复用既有的 20025，**不**新造 ``DOCUMENT_BUSY``。

    一个含义一个码：再造一个「忙」的码，调用方就得靠「两个码都要处理」才不误判，
    而漏掉一个的症状是「偶发 20029 之外的未知码」。
    """

    assert int(ErrorCode.DOCUMENT_IS_PROCESSING) == 20025
    assert not hasattr(ErrorCode, "DOCUMENT_BUSY")
