"""唯一解析入口：把一份 ``Document`` 变成 ``ParsedDocument``。

**项目里只有这里有「文档 → 文本」的转换。** 上传路由、Celery 任务、重建 / 删除
脚本都必须调用 ``prepare_document_content``，绝不在自己那里按文件类型 if/elif
分流；下游只看到一条 Markdown 风格的字符串，完全不感知原始格式。

三分支：
A. 库里已有正文且不需要重解析 → 直接用 ``document.content``（不碰磁盘）；
B. 有源文件路径 → 读字节，交给注册表按文件名选解析器；
C. 两者都没有 → ``DOCUMENT_PARSE_FAILED``。

同步实现：解析是纯 CPU 活（读字节、解码 / 转 HTML / 抽文本），异步只在外层——
T4 的 Celery 任务包住整条流水线。
"""

from app.core.exceptions import AppError, ErrorCode
from app.models.document import Document
from app.providers.parsers import get_parser_registry
from app.providers.parsers.base import ParsedDocument


def prepare_document_content(document: Document, *, reparse: bool = False) -> ParsedDocument:
    """返回这份文档唯一可用的解析结果，供切块器消费。"""
    if document.content and not reparse:
        return ParsedDocument(
            content=document.content,
            source_type=document.source_type or "text",
            metadata={"from": "db"},
        )

    if document.source_file_path:
        data = _read_source_file(document.source_file_path)
        parsed = get_parser_registry().parse(data, filename=document.source_filename or "")
        if not parsed.content.strip():
            raise AppError(ErrorCode.DOCUMENT_CONTENT_EMPTY, "解析之后没有可提取文字")
        return parsed

    raise AppError(
        ErrorCode.DOCUMENT_PARSE_FAILED,
        "document content and file are both empty",
    )


def _read_source_file(path: str) -> bytes:
    # 库里存的是相对存储根的 POSIX 路径（上传进程与 Worker 进程共享同一个根卷），
    # 这里统一还原成当前进程的绝对路径；直接用 str 拼会跨平台失效。
    from app.services.document_storage import resolve_document_storage_path

    resolved = resolve_document_storage_path(path)
    try:
        return resolved.read_bytes()
    except FileNotFoundError:
        raise AppError(ErrorCode.DOCUMENT_PARSE_FAILED, "source file is missing") from None
