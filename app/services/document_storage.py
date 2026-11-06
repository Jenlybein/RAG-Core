"""文档原始文件的落盘与删除。

落盘路径只由服务端拼：``{DOCUMENT_STORAGE_PATH}/{tenant_id}/{document_id}{ext}``。
**原始文件名只写进 ``source_filename`` 列，绝不拼进路径**（防目录穿越）；它也不
参与解析器之外的任何决策。扩展名统一小写后再进白名单与路径。
"""

from pathlib import Path

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode

# 上传白名单（大小写不敏感）：扩展名不在其中一律按「解析不了」的 20023 拒绝。
ALLOWED_EXTENSIONS = {".md", ".markdown", ".txt", ".docx", ".pdf"}

# 扩展名（小写）→ documents.source_type；与 T1 解析器注册的名字一致。
SOURCE_TYPE_BY_EXTENSION = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
    ".docx": "docx",
    ".pdf": "pdf",
}


def extension_of(filename: str) -> str:
    """取小写扩展名（含点）；没有扩展名返回空串。"""

    return Path(filename).suffix.lower()


def store_document_file(
    *, tenant_id: str, document_id: str, filename: str, data: bytes
) -> tuple[str, str]:
    """校验白名单与大小后落盘，返回 ``(source_file_path, source_filename)``。

    ``source_filename`` 是调用方给的原始文件名（只进列，不进路径）。
    """

    suffix = extension_of(filename)
    if suffix not in ALLOWED_EXTENSIONS:
        raise AppError(ErrorCode.DOCUMENT_PARSE_FAILED)
    if len(data) > settings.DOCUMENT_MAX_SIZE_MB * 1024 * 1024:
        raise AppError(ErrorCode.FILE_TOO_LARGE)

    target = Path(settings.DOCUMENT_STORAGE_PATH) / tenant_id / f"{document_id}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return str(target), filename


def remove_document_file(path: str | None) -> None:
    """删除落盘文件；路径为空或文件已不存在时是 no-op（T5 删除文档时复用）。"""

    if not path:
        return
    Path(path).unlink(missing_ok=True)
