"""文档原始文件的落盘与删除。

落盘路径只由服务端拼：``{DOCUMENT_STORAGE_PATH}/{tenant_id}/{document_id}{ext}``。
**原始文件名只写进 ``source_filename`` 列，绝不拼进路径**（防目录穿越）；它也不
参与解析器之外的任何决策。扩展名统一小写后再进白名单与路径。

**库里存的是相对路径，不是绝对路径，也不带平台分隔符。** 上传发生在 API 进程
（可能是 Windows 宿主机），解析发生在 Worker 进程（Linux 容器），两者共享同一个
存储根卷。若把 ``data\\uploads\\...`` 这种宿主平台的绝对/相对路径写进库，容器里
按自己的 CWD 解析必然找不到文件（实测报 ``source file is missing``）。所以
``source_file_path`` 一律是 POSIX 风格的相对路径（``tenant_demo/<id>.pdf``），
读时再用 :func:`resolve_document_storage_path` 拼回当前进程的存储根。
"""

from pathlib import Path, PurePosixPath

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.models.knowledge_base import FormatKind

# 扩展名（小写）→ documents.source_type；与 T1 解析器注册的名字一致。
SOURCE_TYPE_BY_EXTENSION: dict[str, FormatKind] = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
    ".docx": "docx",
    ".pdf": "pdf",
    ".html": "html",
    ".htm": "html",
}

# 上传白名单（大小写不敏感）：不在其中一律按「解析不了」的 20023 拒绝。
# 键集就是白名单本身——扩展名被收，就必定有一个来源类型；清单各写一份，迟早会
# 出现「能传但不知道算哪种格式」。
ALLOWED_EXTENSIONS = frozenset(SOURCE_TYPE_BY_EXTENSION)


def extension_of(filename: str) -> str:
    """取小写扩展名（含点）；没有扩展名返回空串。"""

    return Path(filename).suffix.lower()


def source_type_of(filename: str) -> FormatKind:
    """按扩展名取 ``source_type``；不认识的扩展名按「解析不了」拒绝（20023）。"""

    source_type = SOURCE_TYPE_BY_EXTENSION.get(extension_of(filename))
    if source_type is None:
        raise AppError(ErrorCode.DOCUMENT_PARSE_FAILED)
    return source_type


def store_document_file(
    *, tenant_id: str, document_id: str, filename: str, data: bytes
) -> tuple[str, str]:
    """校验白名单与大小后落盘，返回 ``(source_file_path, source_filename)``。

    ``source_file_path`` 是**相对存储根的 POSIX 路径**（跨进程/跨平台可移植）。
    ``source_filename`` 是调用方给的原始文件名（只进列，不进路径）。
    """

    suffix = extension_of(filename)
    # 复用 source_type_of 的判定，不在这里再写一次白名单比对：白名单就是
    # SOURCE_TYPE_BY_EXTENSION 的键集，两处各判一次只会有两种口径。
    source_type_of(filename)
    if len(data) > settings.DOCUMENT_MAX_SIZE_MB * 1024 * 1024:
        raise AppError(ErrorCode.FILE_TOO_LARGE)

    relative = PurePosixPath(tenant_id) / f"{document_id}{suffix}"
    target = resolve_document_storage_path(str(relative))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return str(relative), filename


def resolve_document_storage_path(stored_path: str) -> Path:
    """把库里的 ``source_file_path`` 还原成当前进程可用的绝对路径。

    相对路径按 ``DOCUMENT_STORAGE_PATH`` 拼回；历史遗留的绝对路径原样使用（不再
    新增这种写法——它跨机器不可移植）。
    """

    candidate = Path(stored_path)
    if candidate.is_absolute():
        return candidate
    return Path(settings.DOCUMENT_STORAGE_PATH) / stored_path.replace("\\", "/")


def remove_document_file(path: str | None) -> None:
    """删除落盘文件；路径为空或文件已不存在时是 no-op（T5 删除文档时复用）。"""

    if not path:
        return
    resolve_document_storage_path(path).unlink(missing_ok=True)
